import os
import json
import asyncio
import hashlib
import datetime
import re
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright
from openai import AsyncOpenAI

openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", "YOUR_LOCAL_API_KEY"))

# -------------------------------------------------------------
# [사후 검증] 네이버 SEO 사후 검증 함수 (가이드미달 완전 박멸 버전)
# -------------------------------------------------------------
def validate_naver_title(title):
    """네이버 쇼핑 상품명 가이드라인 만족 여부 검증"""
    if not title:
        return False
    
    # 1. 글자 수 가이드 (20자 ~ 45자)
    if not (20 <= len(title) <= 45):
        return False
        
    # ❌ [가이드 미달 주범 제거] 
    # 문맥상 발생하는 단어 중복(ex: 파타야 시내 + 시암 파타야CC)을 
    # 에러로 잡던 무지성 단어 중복 체크 로직을 완전히 삭제합니다.
    
    # 2. 금지 특수문자 및 진짜 악성 키워드만 필터링
    # 원본명에 섞여 들어오는 등급명('세이브', '특가', '스탠다드')은 필터에서 완전히 제외합니다.
    forbidden = ["★", "▼", "▲", "◆", "■", "♥", "대박", "신상품"]
    if any(f_word in title for f_word in forbidden):
        return False
    return True

# -------------------------------------------------------------
# [프롬프트] 5옵션 생성 프롬프트기 (대괄호 쪼개짐 버그 방어 버전)
# -------------------------------------------------------------
def make_batch_prompt(data):
    airport = data.get('departure_airport', "없음")
    
    # 지시문 내 대괄호([]) 기호 오염을 막기 위해 순수 텍스트(ex: 대구출발)로 정제 후 문장 가이드를 줍니다.
    if airport != "없음" and airport:
        clean_airport = airport.replace("[", "").replace("]", "").strip()
        departure_context = f"""- 지정 출발지: {clean_airport}
(★필수 규칙: 생성하는 5개 상품명은 반드시 문장 맨 앞에 대괄호를 붙인 '[{clean_airport}]' 문구로 시작해야 합니다. 예를 들어 '[{clean_airport}] 방콕 패키지...' 형태로 완벽하게 결합하여 출력하되, 절대로 '{clean_airport} [출발]'과 같이 대괄호 위치를 쪼개거나 분리하여 띄어 쓰지 마십시오.)"""
    else:
        departure_context = "- 지정 출발지: 없음\n(★주의: 상품명 맨 앞에 '기본출발', '전국출발' 같은 어떠한 출발 관련 문구도 절대 넣지 말고, 곧바로 '지역명'부터 시작할 것)"

    return f"""당신은 네이버 쇼핑 검색 최적화(SEO) 기준에 맞춰 여행 상품명을 정제하고 재창조하는 마케팅 자동화 전문가입니다.
제공된 정형 데이터를 바탕으로 가이드라인을 완벽히 준수하는 서로 다른 스타일의 새로운 상품명 5개를 생성하세요.

[입력 데이터]
- 기준 상품명: {data.get('full_title', '제목없음')}
- 여행 지역: {data.get('region', '지역명 미상')}
- 기간: {data.get('duration', '기간 미상')}
{departure_context}
- 핵심 설명: {data.get('description', '')}
- 추출 키워드: {data.get('hashtags', '')}

[네이버 쇼핑 상품명 가이드라인]
1. 글자 수: 공백 포함 최소 25자 ~ 최대 42자 사이로 매끄럽게 구성하세요.
2. 금지어 규칙: 원본명에 있는 '신상품', '세이브', '특가', '대박' 같은 수식어나 특수문자(★, # 등)는 절대 새로 만드는 상품명에 포함하지 마십시오.
3. 출발지 배치 조건: 
   - 지정 출발지가 존재할 경우: 반드시 상품명 맨 앞에 글자 분리 없이 결합된 대괄호 형태로 시작합니다. (예시: [{clean_airport if airport != "없음" else ""}] )
   - 지정 출발지가 '없음'일 경우: 무조건 곧바로 지역명/브랜드명으로 상품명을 시작합니다.
4. 포맷: 문장이 아닌 명사형 키워드의 깔끔한 띄어쓰기 조합으로 구성한다.

반드시 아래 JSON 포맷으로만 응답하세요. 다른 설명은 생략합니다.
{{
  "option_1": "상품명_1",
  "option_2": "상품명_2",
  "option_3": "상품명_3",
  "option_4": "상품명_4",
  "option_5": "상품명_5"
}}"""

# -------------------------------------------------------------
# [초고속 핸들러] 실시간 비동기 병렬 호출 제어 엔진 (하드 가드 포함)
# -------------------------------------------------------------
async def fetch_live_llm_title(p, semaphore, runtime_cache_check, llm_results):
    p_id = p["ID"]
    orig_title = p["원본상품명"]
    
    if orig_title in runtime_cache_check:
        return

    async with semaphore:
        try:
            runtime_cache_check[orig_title] = p_id
            response = await openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that outputs JSON."},
                    {"role": "user", "content": make_batch_prompt(p)}
                ],
                response_format={"type": "json_object"},
                temperature=0.2
            )
            res_json = json.loads(response.choices[0].message.content)
            options = [res_json.get(f"option_{i}", "").strip() for i in range(1, 6)]
            
            # 💡 [★ 하드웨어 스크립트 가드 수리] 대구 [출발], [대구출발] 쪼개짐 현상 완전 방어 자동 보정기
            airport = p.get('departure_airport', "없음")
            if airport != "없음" and airport:
                clean_airport = airport.replace("[", "").replace("]", "").strip() # ex) 대구출발
                city_name = clean_airport.replace("출발", "").strip() # ex) 대구
                
                fixed_options = []
                for opt in options:
                    # AI가 이미 완벽하게 [대구출발]로 시작했다면 통과
                    if opt.startswith(airport):
                        fixed_options.append(opt)
                    else:
                        # 대구 [출발], [대구] 출발, 대구출발 등 AI가 망쳐놓은 모든 출발지 텍스트 패턴 패턴 강제 추출 및 제거
                        bad_patterns = [
                            airport, clean_airport, f"{city_name} [출발]", f"[{city_name}] 출발", 
                            f"[{city_name} 출발]", f"[{city_name}출발]", city_name, "출발"
                        ]
                        
                        clean_opt = opt
                        for pattern in bad_patterns:
                            clean_opt = clean_opt.replace(pattern, "")
                        
                        # 특수문자나 찌꺼기 공백 다듬기
                        clean_opt = re.sub(r'^[ \t\s\[\]\-\/]+', '', clean_opt).strip()
                        
                        # 깔끔하게 정상 포맷인 [대구출발] 을 맨 앞에 강제 결합
                        fixed_options.append(f"{airport} {clean_opt}")
                options = fixed_options
            
            # 새롭게 정의된 완화 필터로 사후 검증 진행
            llm_results[p_id] = options
        except Exception as e:
            print(f"❌ 단일 상품 LLM 생성 오류 패스 ({orig_title}): {e}")
            llm_results[p_id] = ["[Error]"] * 5

# -------------------------------------------------------------
# [1단계 데이터 수집] 수집 로직
# -------------------------------------------------------------
async def process_single_product_raw(item, target_region, target_airport, current_url):
    try:
        main_info = await item.query_selector(":scope > .inr.right")
        img_check = await item.query_selector(":scope > .inr.img")
        if not main_info or not img_check: return None

        title_el = await main_info.query_selector(".item_title")
        full_title = (await title_el.inner_text()).strip() if title_el else "제목 없음"

        if target_airport == "없음" or not target_airport:
            if "[청주출발]" in full_title or "depCityCd=CJJ" in current_url: target_airport = "[청주출발]"
            elif "[제주출발]" in full_title or "depCityCd=CJU" in current_url: target_airport = "[제주출발]"
            elif "[부산출발]" in full_title or "depCityCd=PUS" in current_url: target_airport = "[부산출발]"
            elif "[대구출발]" in full_title or "depCityCd=TAE" in current_url: target_airport = "[대구출발]"

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
            if potential_url and "bg_alpha" not in potential_url: img_url = potential_url.strip()

        if img_url and img_url.startswith("//"): img_url = "https:" + img_url

        return {
            "ID": product_id, "원본상품명": full_title, "가격": int(price) if price else 0, "URL": current_url, "이미지URL": img_url, "지정지역": target_region, "출발공항": target_airport,
            "full_title": full_title, "region": target_region, "departure_airport": target_airport, "duration": duration, "description": product_desc, "hashtags": ", ".join(all_hashtags)
        }
    except Exception as e:
        print(f"⚠️ 개별 상품 추출 중 오류 패스: {e}"); return None

# -------------------------------------------------------------
# 메인 실행 함수
# -------------------------------------------------------------
async def run_crawler():
    print("🌐 구글 API 인증 및 스프레드시트 연결 중...")
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    json_raw = os.environ.get("GOOGLE_JSON_RAW")
    
    try:
        if json_raw: creds = Credentials.from_service_account_info(json.loads(json_raw), scopes=scopes)
        else: creds = Credentials.from_service_account_file('secrets.json', scopes=scopes)
        gc = gspread.authorize(creds)
    except Exception as auth_error:
        print(f"❌ 구글 API 인증 실패: {auth_error}"); return

    source_spreadsheet_id = os.environ.get("SOURCE_SPREADSHEET_ID")
    if not source_spreadsheet_id:
        print("❌ 에러: 환경 변수 'SOURCE_SPREADSHEET_ID'가 설정되어 있지 않습니다."); return
    
    try:
        source_doc = gc.open_by_key(source_spreadsheet_id)
        source_sheet = source_doc.worksheet("상품리스트2")
    except Exception as e:
        print(f"❌ 소스 스프레드시트 로드 실패: {e}"); return

    all_rows = source_sheet.get_all_values()
    data_rows = all_rows[1:]
    
    target_tasks = []
    for row in data_rows:
        if len(row) >= 1:
            url_clean = row[0].strip()  
            if url_clean.startswith("http"):
                target_tasks.append({
                    "url": url_clean,
                    "sheet_region": row[1].strip() if len(row) > 1 and row[1].strip() else "지역명 미상",
                    "sheet_airport": row[2].strip() if len(row) > 2 and row[2].strip() else "없음"
                })
                
    print(f"✅ 총 {len(target_tasks)}개의 유효 타겟 상품 라인을 확보했습니다.")

    existing_titles_dict = {}
    target_spreadsheet_id = os.environ.get("TARGET_SPREADSHEET_ID", source_spreadsheet_id)
    try:
        target_doc = gc.open_by_key(target_spreadsheet_id)
        github_sheet = target_doc.worksheet("github2")
        for r in github_sheet.get_all_records():
            pid = str(r.get("ID", "")).strip()
            if pid:
                t_opts = [str(r.get(f"네이버_상품명_{i}", "")).strip() for i in range(1, 6)]
                if not any(t_opts) or any("[⚠️가이드미달]" in opt or " [출발]" in opt for opt in t_opts): 
                    continue
                existing_titles_dict[pid] = t_opts
        print(f"✅ 정상 수집된 기존 상품 {len(existing_titles_dict)}개를 캐싱했습니다.")
    except Exception:
        print("⚠️ 기존 github2 캐시가 없거나 비어있습니다. 전수 조사로 진행합니다.")

    raw_products = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 1024},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        for task in target_tasks:
            current_url = task["url"]
            target_region = task["sheet_region"]
            target_airport = task["sheet_airport"]
            
            try:
                print(f"🔄 {target_region} (출발: {target_airport}) 페이지 로딩 중...")
                await page.goto(current_url, wait_until="domcontentloaded", timeout=40000)
                
                try: await page.wait_for_selector(".option_wrap.result .count em", timeout=12000)
                except Exception: pass

                total_count = 20  
                try:
                    count_element = await page.query_selector(".option_wrap.result .count em")
                    if count_element:
                        count_text = (await count_element.inner_text()).strip()
                        if count_text.isdigit(): total_count = int(count_text)
                except Exception: pass

                needed_scrolls = (total_count - 1) // 20 if total_count > 20 else 0
                if needed_scrolls > 0:
                    for scroll_step in range(1, needed_scrolls + 1):
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await asyncio.sleep(2.0)
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight - 300)")
                        await asyncio.sleep(0.3)
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        current_items = await page.query_selector_all(".prod_list_wrap ul.type > li")
                        if len(current_items) >= total_count: break

                await asyncio.sleep(1.0)
                final_items = await page.query_selector_all(".prod_list_wrap ul.type > li")
                
                sc_tasks = [process_single_product_raw(item, target_region, target_airport, current_url) for item in final_items]
                batch_results = await asyncio.gather(*sc_tasks)
                raw_products.extend([res for res in batch_results if res is not None])
                
            except Exception as e:
                print(f"❌ {current_url} 크롤링 에러: {e}")
        await browser.close()

    if not raw_products:
        print("ℹ️ 수집된 상품이 없습니다."); return

    print(f"📦 총 {len(raw_products)}개 상품 중 신규 및 오류 대상 실시간 갱신 돌입...")
    runtime_cache_check = {}
    llm_results = {}
    
    semaphore = asyncio.Semaphore(15) 
    
    live_tasks = []
    for p in raw_products:
        if p["ID"] in existing_titles_dict:
            continue
        live_tasks.append(fetch_live_llm_title(p, semaphore, runtime_cache_check, llm_results))
        
    if live_tasks:
        await asyncio.gather(*live_tasks)
        print("✅ 실시간 LLM 병렬 연산 완료!")
    else:
        print("♻️ 처리할 신규 상품이나 공란이 없습니다. 기존 캐시를 유지합니다.")

    # 4단계: 최종 데이터 조립 및 구글 시트 반영
    final_table = []
    for p in raw_products:
        p_id = p["ID"]
        orig_title = p["원본상품명"]
        
        if p_id in llm_results: t_list = llm_results[p_id]
        elif orig_title in runtime_cache_check and runtime_cache_check[orig_title] in llm_results: t_list = llm_results[runtime_cache_check[orig_title]]
        elif p_id in existing_titles_dict:
            t_list = existing_titles_dict[p_id]
            while len(t_list) < 5: t_list.append("")
        else: t_list = ["[미생성]"] * 5

        final_table.append({
            "ID": p_id, "원본상품명": orig_title, "가격": p["가격"], "URL": p["URL"], "이미지URL": p["이미지URL"], "지정지역": p["지정지역"], "출발공항": p["출발공항"],
            "네이버_상품명_1": t_list[0], "네이버_상품명_2": t_list[1], "네이버_상품명_3": t_list[2], "네이버_상품명_4": t_list[3], "네이버_상품명_5": t_list[4]
        })

    if final_table:
        df = pd.DataFrame(final_table)
        column_order = ["ID", "원본상품명", "가격", "URL", "이미지URL", "지정지역", "출발공항", 
                        "네이버_상품명_1", "네이버_상품명_2", "네이버_상품명_3", "네이버_상품명_4", "네이버_상품명_5"]
        df = df[column_order]
        
        target_spreadsheet_id = os.environ.get("TARGET_SPREADSHEET_ID", source_spreadsheet_id)
        try:
            doc = gc.open_by_key(target_spreadsheet_id)
            sheet = doc.worksheet("github2")
            sheet.clear()
            sheet.update('A1', [df.columns.values.tolist()] + df.values.tolist())
            print(f"✅ 구글 시트 github2 반영 완료 (가이드미달 및 공항 띄어쓰기 오류 전면 박멸)")
        except Exception as e:
            print(f"❌ 시트 반영 실패: {e}")

if __name__ == "__main__":
    asyncio.run(run_crawler())
