import os
import json
import asyncio
import hashlib
import re
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright
from openai import AsyncOpenAI

openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", "YOUR_LOCAL_API_KEY"))

async def generate_naver_titles_llm(data):
    airport = data.get('departure_airport', "없음")
    
    if airport != "없음" and airport:
        departure_context = f"- 지정 출발공항: {airport}\n(★필수 규칙: 생성하는 모든 상품명의 '맨 첫 단어'는 무조건 대괄호를 포함한 '{airport}'로 시작해야 합니다. 다른 공항명은 절대 넣지 마십시오.)"
        example_1 = f"{airport} 방콕 파타야 5일 전일정 5성호텔 산호섬투어"
        example_2 = f"{airport} 다낭 5일 가족여행 가성비 패키지 추천"
        example_3 = f"{airport} 세부 4일 초특급리조트 호핑투어 힐링"
    else:
        departure_context = "- 지정 출발공항: 없음\n(★필수 규칙: 상품명 맨 앞에 '[기본출발]', '[전국출발]', '[출발지없음]' 등 어떠한 출발 관련 문구도 절대 넣지 말고, 곧바로 '지역명'부터 시작할 것)"
        example_1 = "방콕 파타야 5일 전일정 5성호텔 산호섬투어"
        example_2 = "다낭 5일 가족여행 가성비 패키지 추천"
        example_3 = "세부 4일 초특급리조트 호핑투어 힐링"

    prompt = f"""당신은 네이버 쇼핑 검색 최적화(SEO) 기준에 맞춰 여행 상품명을 정제하고 재창조하는 마케팅 자동화 전문가입니다.
제공된 정형 데이터를 바탕으로 가이드라인을 완벽히 준수하는 서로 다른 스타일의 새로운 상품명 3개를 생성하세요.

[입력 데이터]
- 기준 상품명: {data.get('full_title', '제목없음')}
- 여행 지역: {data.get('region', '지역명 미상')}
- 기간: {data.get('duration', '기간 미상')}
{departure_context}
- 핵심 설명: {data.get('description', '')}
- 추출 키워드: {data.get('hashtags', '')}

[네이버 쇼핑 상품명 가이드라인]
1. 글자 수: 공백 포함 최소 25자 ~ 최대 35자 사이로 구성한다. (40자 절대 초과 금지)
2. 중복 제거: 상품명 내부에서 동일한 단어가 2회 이상 중복 나열되는 것을 절대 금지한다.
3. 정제성: 원본 상품명에 있는 '신상품', '세이브', '특가', '대박', 특수문자(★, # 등)는 절대 새로 만드는 상품명에 포함하지 마십시오.
4. 출발지 조건 규칙 (★최우선 순위): 
   - [지정 출발공항]이 '{airport}'로 존재할 경우: 3개의 옵션 모두 무조건 맨 앞에 '{airport}' 가 와야 합니다. 부산, 대구 등 다른 공항명과 절대로 헷갈리거나 섞이지 마십시오.
   - [지정 출발공항]이 '없음'일 경우: 절대로 임의의 출발 문구를 조작해 넣지 말고 무조건 곧바로 지역명/브랜드명으로 상품명을 시작한다.
5. 포맷: 문장이 아닌 명사형 키워드의 깔끔한 띄어쓰기 조합으로 구성한다.

반드시 아래 JSON 포맷으로만 응답하세요. 다른 설명은 생략합니다.
{{
  "option_1": "{example_1}",
  "option_2": "{example_2}",
  "option_3": "{example_3}"
}}
"""
    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that outputs JSON. 지시사항 중 출발 공항 규칙을 절대적으로 준수하세요."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0,
            seed=42
        )
        
        result_json = json.loads(response.choices[0].message.content)
        return (
            result_json.get("option_1", "").strip(),
            result_json.get("option_2", "").strip(),
            result_json.get("option_3", "").strip()
        )
    except Exception as e:
        print(f"❌ LLM 상품명 생성 중 에러 발생: {e}")
        return f"[Error] {data.get('full_title', '')}", f"[Error] {data.get('region', '')}", f"[Error] {data.get('full_title', '')}"


async def process_single_product(item, target_region, target_airport, current_url, existing_titles_dict, runtime_titles_dict):
    try:
        main_info = await item.query_selector(":scope > .inr.right")
        img_check = await item.query_selector(":scope > .inr.img")
        
        if not main_info or not img_check:
            return None

        title_el = await main_info.query_selector(".item_title")
        full_title = (await title_el.inner_text()).strip() if title_el else "제목 없음"

        if target_airport == "없음" or not target_airport:
            if "[청주출발]" in full_title or "depCityCd=CJJ" in current_url:
                target_airport = "[청주출발]"
            elif "[제주출발]" in full_title or "depCityCd=CJU" in current_url:
                target_airport = "[제주출발]"
            elif "[부산출발]" in full_title or "depCityCd=PUS" in current_url:
                target_airport = "[부산출발]"
            elif "[대구출발]" in full_title or "depCityCd=TAE" in current_url:
                target_airport = "[대구출발]"

        price_el = await main_info.query_selector(".price")
        price_raw = await price_el.inner_text() if price_el else "0"
        price = "".join(filter(str.isdigit, price_raw))

        unique_str = f"{full_title}_{price}"
        product_id = hashlib.md5(unique_str.encode()).hexdigest()[:8]

        if "#" in full_title:
            parts = full_title.split("#")
            title_hashtags = sorted([p.strip() for p in parts[1:] if p.strip()])
        else:
            title_hashtags = []

        hash_span_els = await main_info.query_selector_all(".hash_group span")
        ui_hashtags = [(await h.inner_text()).replace("#", "").strip() for h in hash_span_els]
        all_hashtags = sorted(list(set(title_hashtags + ui_hashtags)))

        desc_el = await main_info.query_selector(".item_text.stit")
        product_desc = (await desc_el.inner_text()).strip() if desc_el else ""

        duration_el = await main_info.query_selector("span.icn.cal")
        duration_text = (await duration_el.inner_text()).strip() if duration_el else ""
        duration = duration_text.replace("여행기간", "").strip()

        img_url = ""
        img_el = await img_check.query_selector("img")
        if img_el:
            data_src = await img_el.get_attribute("data-src")
            src = await img_el.get_attribute("src")
            potential_url = data_src if data_src else src
            
            if potential_url and "bg_alpha" not in potential_url:
                img_url = potential_url.strip()
            else:
                all_imgs = await img_check.query_selector_all("img")
                for im in all_imgs:
                    i_src = await im.get_attribute("src")
                    i_data = await im.get_attribute("data-src")
                    target = i_data if i_data else i_src
                    if target and "bg_alpha" not in target:
                        img_url = target.strip()
                        break

        if img_url and img_url.startswith("//"): 
            img_url = "https:" + img_url

        if product_id in existing_titles_dict:
            t1, t2, t3 = existing_titles_dict[product_id]
        elif full_title in runtime_titles_dict:
            t1, t2, t3 = runtime_titles_dict[full_title]
            print(f"♻️ [비용 절감] 캐시 재사용: {full_title}")
        else:
            print(f"✨ [신규 상품] LLM 타이틀 생성: {full_title}")
            ai_input_data = {
                "full_title": full_title,
                "region": target_region,          
                "departure_airport": target_airport, 
                "duration": duration,
                "description": product_desc,
                "hashtags": ", ".join(all_hashtags)
            }
            t1, t2, t3 = await generate_naver_titles_llm(ai_input_data)
            runtime_titles_dict[full_title] = (t1, t2, t3)

        return {
            "ID": product_id,
            "원본상품명": full_title,
            "가격": int(price) if price else 0,
            "URL": current_url,
            "이미지URL": img_url,
            "지정지역": target_region,
            "출발공항": target_airport,
            "네이버_상품명_1": t1,
            "네이버_상품명_2": t2,
            "네이버_상품명_3": t3
        }
    except Exception as e:
        print(f"⚠️ 개별 상품 추출 중 오류 패스: {e}")
        return None


async def run_crawler():
    print("🌐 구글 API 인증 및 스프레드시트 연결 중...")
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    json_raw = os.environ.get("GOOGLE_JSON_RAW")
    
    try:
        if json_raw:
            service_account_info = json.loads(json_raw)
            creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
        else:
            creds = Credentials.from_service_account_file('secrets.json', scopes=scopes)
        gc = gspread.authorize(creds)
    except Exception as auth_error:
        print(f"❌ 구글 API 인증 실패: {auth_error}")
        return

    source_spreadsheet_id = os.environ.get("SOURCE_SPREADSHEET_ID")
    target_spreadsheet_id = os.environ.get("TARGET_SPREADSHEET_ID", source_spreadsheet_id)

    if not source_spreadsheet_id:
        print("❌ 에러: 환경 변수 'SOURCE_SPREADSHEET_ID'가 설정되어 있지 않습니다.")
        return

    try:
        source_doc = gc.open_by_key(source_spreadsheet_id)
        source_sheet = source_doc.worksheet("상품리스트")
        
        all_rows = source_sheet.get_all_values()
        data_rows = all_rows[1:]
        
        target_tasks = []
        for row in data_rows:
            if len(row) >= 1 and row[0].strip().startswith("http"):
                target_tasks.append({
                    "url": row[0].strip(),
                    "sheet_region": row[1].strip() if len(row) > 1 and row[1].strip() else "지역명 미상",
                    "sheet_airport": row[2].strip() if len(row) > 2 and row[2].strip() else "없음"
                })
        print(f"✅ 총 {len(target_tasks)}개의 유효 타겟 URL을 확보했습니다.")
    except Exception as e:
        print(f"❌ URL 리스트 파싱 에러: {e}")
        return

    existing_titles_dict = {}
    try:
        target_doc = gc.open_by_key(target_spreadsheet_id)
        github_sheet = target_doc.worksheet("github")
        existing_data = github_sheet.get_all_records()
        
        for r in existing_data:
            pid = str(r.get("ID", "")).strip()
            if pid:
                t1 = str(r.get("네이버_상품명_1", "")).strip()
                t2 = str(r.get("네이버_상품명_2", "")).strip()
                t3 = str(r.get("네이버_상품명_3", "")).strip()
                if t1 or t2 or t3:
                    existing_titles_dict[pid] = (t1, t2, t3)
        print(f"✅ 기존 상품 {len(existing_titles_dict)}개 캐싱 완료")
    except Exception as cache_error:
        print(f"⚠️ 기존 시트 로드 패스: {cache_error}")

    runtime_titles_dict = {}
    all_products = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 1024},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        for idx, task in enumerate(target_tasks, start=1):
            current_url = task["url"]
            target_region = task["sheet_region"]
            target_airport = task["sheet_airport"]
            
            try:
                print(f"🔄 [{idx}/{len(target_tasks)}] {target_region} (출발: {target_airport}) 수집 중...")
                await page.goto(current_url, wait_until="domcontentloaded", timeout=30000)
                
                try:
                    await page.wait_for_selector(".option_wrap.result .count em", timeout=8000)
                except Exception:
                    pass

                total_count = 20  
                try:
                    count_element = await page.query_selector(".option_wrap.result .count em")
                    if count_element:
                        count_text = (await count_element.inner_text()).strip()
                        if count_text.isdigit():
                            total_count = int(count_text)
                            print(f"   ↳ 🎯 총 상품 수: [{total_count}개]")
                except Exception as e:
                    print(f"   ⚠️ 총 상품 수 파싱 에러: {e}")

                needed_scrolls = (total_count - 1) // 20 if total_count > 20 else 0
                if needed_scrolls > 0:
                    for _ in range(needed_scrolls):
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await asyncio.sleep(1.5)
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight - 300)")
                        await asyncio.sleep(0.3)
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

                await asyncio.sleep(1.0)
                final_items = await page.query_selector_all(".prod_list_wrap ul.type > li")
                print(f"📦 최종 엘리먼트 {len(final_items)}개 추출 완료")
                
                tasks = [
                    process_single_product(item, target_region, target_airport, current_url, existing_titles_dict, runtime_titles_dict) 
                    for item in final_items
                ]
                batch_results = await asyncio.gather(*tasks)
                
                for res in batch_results:
                    if res is not None:
                        all_products.append(res)

            except Exception as e:
                print(f"❌ {current_url} 접속 에러: {e}")
                continue

        await browser.close()

    # 스프레드시트 일괄 업데이트
    if all_products:
        print(f"\n🚀 총 {len(all_products)}개 상품 스프레드시트 업데이트 시작...")
        try:
            df = pd.DataFrame(all_products)
            # 중복 상품 최종 정제 (ID 기준)
            df = df.drop_duplicates(subset=["ID"], keep="first")
            column_order = ["ID", "원본상품명", "가격", "URL", "이미지URL", "지정지역", "출발공항", "네이버_상품명_1", "네이버_상품명_2", "네이버_상품명_3"]
            df = df[column_order]
            data_to_upload = [df.columns.values.tolist()] + df.values.tolist()

            target_doc = gc.open_by_key(target_spreadsheet_id)
            try:
                sheet = target_doc.worksheet("github")
            except gspread.exceptions.WorksheetNotFound:
                sheet = target_doc.add_worksheet(title="github", rows="3000", cols="15")
                
            sheet.clear()  
            sheet.update('A1', data_to_upload)
            print(f"✅ 성공: [{target_doc.title}] github 시트에 총 {len(df)}개 적재 완료!")
        except Exception as e:
            print(f"❌ 구글 시트 적재 중 에러: {e}")

if __name__ == "__main__":
    asyncio.run(run_crawler())
