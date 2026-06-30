import os
import json
import hashlib
import asyncio
import re
from typing import List, Dict, Set

import gspread
from google.oauth2.service_account import Credentials
from openai import AsyncOpenAI

SPREADSHEET_KEY = os.environ.get("SOURCE_SPREADSHEET_ID") 
SOURCE_SHEET_NAME = "raw"                        
CACHE_FILE_PATH = "product_cache.json"          

MAX_CONCURRENT_TASKS = 10 
semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
aclient = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

def calculate_hash(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()

def clean_origin_name(text: str) -> str:
    if not text:
        return ""
    
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'_[A-Za-z0-9]+', '', text)
    text = re.sub(r'\b[A-Z]{3,}\d{3,}[A-Za-z0-9]*\b', '', text)
    text = re.sub(r'\b[A-Za-z0-9]{10,20}\b', '', text)
    text = text.replace("_", " ")
    
    trash_words = ["선착순특가", "실속여행", "신상품", "대박특가", "출발확정", "세미팩", "[SK스토아 에디션]", "[USJ 오피셜 호텔]", "[USJ와패키지를한번에]", "[VIP]"]
    for word in trash_words:
        text = text.replace(word, "")
        
    text = re.sub(r'\b\d{7,}\b', '', text)
    text = re.sub(r'[^가-힣0-9\s\-\[\]\(\)\&]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def load_google_sheet_data():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    
    google_json_raw = os.environ.get("GOOGLE_JSON_RAW")
    if google_json_raw:
        service_account_info = json.loads(google_json_raw)
        creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
        
    client = gspread.authorize(creds)
    doc = client.open_by_key(SPREADSHEET_KEY)
    sheet = doc.worksheet(SOURCE_SHEET_NAME)
    
    print(f"⏳ '{SOURCE_SHEET_NAME}' 시트에서 수식 결과 데이터를 가져오는 중...")
    all_values = sheet.get_all_values()
    
    if not all_values:
        return sheet, [], []
        
    headers = all_values[0]
    rows = all_values[1:]
    
    processed_rows = []
    for idx, row in enumerate(rows, start=2): 
        while len(row) < 7:
            row.append("")
            
        p_id = str(row[0]).strip()   
        p_name = str(row[1]).strip() 
        current_result = str(row[6]).strip() 
        
        if p_id and p_name and p_id != "id": 
            processed_rows.append({
                "row_num": idx,
                "id": p_id,
                "name": p_name,
                "current_result": current_result
            })
            
    return sheet, processed_rows, headers

def load_cache() -> Dict[str, Dict]:
    if os.path.exists(CACHE_FILE_PATH):
        with open(CACHE_FILE_PATH, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except:
                return {}
    return {}

def save_cache(cache_data: Dict[str, Dict]):
    with open(CACHE_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache_data, f, ensure_ascii=False, indent=4)

async def call_llm_with_retry(target: Dict, confirmed_pool: Set[str]) -> str:
    async with semaphore:
        origin_name = clean_origin_name(target["name"])
        
        if not origin_name:
            origin_name = target["name"]

        retry_count = 0
        extra_prompt = ""
        
        while retry_count < 3:
            try:
                response = await aclient.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {
                            "role": "system", 
                            "content": (
                                "너는 네이버 쇼핑 검색 노출 로직(SEO) 최적화 및 커머스 마케팅 피드 전문 카피라이터야. "
                                "너의 유일한 목적은 원본 상품명에서 불필요한 노이즈를 제거하고, 네이버 쇼핑 노출 스코어가 가장 높은 25자 이상 45자 이하의 매매력적인 완성형 상품명으로 재조합하는 것이다.\n\n"
                                "⚠️ [네이버 쇼핑 SEO 핵심 가이드라인]\n"
                                "1. 🎯 엄격한 글자 수 제한 (25자 ~ 45자):\n"
                                "   - 최종 추천 결과물의 길이는 공백을 포함하여 반드시 최소 25자에서 최대 45자 사이여야 한다. (★45자 절대 초과 금지, 25자 미만 절대 금지★)\n"
                                "2. 🧠 정보 중심의 유기적 재조합 (키워드 단순 나열 금지):\n"
                                "   - '미샌딩포함 스낵 제공'처럼 키워드 조각을 무작정 이어 붙인 나열형 상품명은 네이버 로직에서 어뷰징으로 차단당한다.\n"
                                "   - 반드시 문맥을 파악하여 '메리어트호텔 6일 클락 명문 골프 패키지', '홋카이도 후라노 비에이 온천호텔 4일 패키지여행'처럼 소비자가 읽기 쉽고 로직이 선호하는 부드러운 문장 구조로 재조합해라.\n"
                                "3. 🛑 상품 ID, 코드 및 영문 노이즈 전면 제거:\n"
                                "   - 상품 고유 ID, 영어 알파벳과 숫자가 뒤섞인 마스터 코드 등 시스템 노이즈는 네이버 쇼핑 검색 품질 가이드라인 위반이므로 단 한 글자도 진입시키지 마라. 100% 순수 한글, 일정 숫자, 공백으로만 채워라.\n"
                                "4. 💎 핵심 가치 정보(숫자) 보존:\n"
                                "   - '9일', '10일', '4일', '4성급', '18홀 3회' 등 여행 상품의 일자, 기간, 등급을 나타내는 숫자는 네이버 검색 필수 키워드이므로 절대로 누락하지 말고 카피 속에 자연스럽게 녹여내라.\n"
                                "5. 광고성 공해 단어 전면 배제:\n"
                                "   - '선착순특가', '실속여행', '신상품', '대박특가', '출발확정', '세미팩' 등 네이버 로직이 스팸으로 분류하는 진부한 홍보 단어는 무조건 삭제해라.\n\n"
                                "⚠️ [출력 제한 사양]\n"
                                "- 문장 끝은 끊기지 않고 신뢰감을 주는 명사구 형태(예: ~ 여행, ~ 패키지, ~ 투어)로 자연스럽게 끝맺음할 것.\n"
                                "- 쉼표(,), 느낌표(!), 물결(~), 플러스(+), 언더바(_) 등의 부호는 절대 사용 금지.\n"
                                "- 부가적인 설명, 서론, 후론은 일체 배제하고 오직 가공 완료된 상품명 '딱 한 줄'만 출력할 것.\n\n"
                                f"{extra_prompt}"
                            )
                        },
                        {"role": "user", "content": f"원본 상품명: {origin_name}"}
                    ],
                    max_tokens=80,
                    temperature=0.3
                )
                
                suggested_name = response.choices[0].message.content.strip()
                
                suggested_name = re.sub(r'[A-Za-z_]', '', suggested_name).strip()
                suggested_name = re.sub(r'\s+', ' ', suggested_name)
                
                if suggested_name not in confirmed_pool and 25 <= len(suggested_name) <= 45:
                    confirmed_pool.add(suggested_name)
                    return suggested_name
                
                retry_count += 1
                extra_prompt = f"\n⚠️ 글자 수가 25자~45자 범위를 벗어났거나 단순 단어 나열 형태입니다. 네이버 쇼핑 노출 규격(25~45자)에 맞게 완성형 문장으로 다시 작성하세요."
                
            except Exception as e:
                print(f"❌ API 에러 발생 ({origin_name}): {e}")
                await asyncio.sleep(1)
                retry_count += 1
                
        fallback_name = re.sub(r'[A-Za-z_]', '', origin_name).strip()
        if len(fallback_name) > 45:
            fallback_name = fallback_name[:42] + " 여행"
        elif len(fallback_name) < 25:
            fallback_name = fallback_name + " 추천 패키지 여행"
        confirmed_pool.add(fallback_name)
        return fallback_name

async def main():
    print(f"🛒 1. '{SOURCE_SHEET_NAME}' 시트의 LET 수식 결과 추출 및 캐시 확인...")
    sheet, current_products, headers = load_google_sheet_data()
    old_cache = load_cache()
    
    if not current_products:
        print("ℹ️ 처리할 상품 데이터가 없습니다. 수식이 로딩 중이거나 비어있습니다.")
        return

    current_product_ids = {p["id"] for p in current_products}
    cleaned_cache = {str(p_id): data for p_id, data in old_cache.items() if str(p_id) in current_product_ids}
    
    targets_to_process = []
    new_cache = cleaned_cache.copy()
    
    for p in current_products:
        p_id = p["id"]
        current_hash = calculate_hash(p["name"])
        
        has_corrupted_result = (
            not p["current_result"] or
            "_" in p["current_result"] or 
            re.search(r'[A-Za-z]', p["current_result"]) or 
            p["current_result"].endswith(("포함", "제공", "특전")) or 
            not (25 <= len(p["current_result"]) <= 45) or
            " " not in p["current_result"][-8:]
        )
        
        if has_corrupted_result:
            if p_id in new_cache:
                del new_cache[p_id]
            targets_to_process.append(p)
        else:
            is_new = p_id not in cleaned_cache
            is_changed = not is_new and cleaned_cache[p_id]["hash"] != current_hash
            is_row_shifted = not is_new and p["current_result"] != cleaned_cache[p_id]["recomposed_name"]
            
            if is_new or is_changed or is_row_shifted:
                targets_to_process.append(p)

    confirmed_pool = set()
    for p in current_products:
        p_id = p["id"]
        if p_id in new_cache and p_id not in {t["id"] for t in targets_to_process}:
            confirmed_pool.add(new_cache[p_id]["recomposed_name"])
            
    print(f"📊 분석 결과: 현재 raw 시트 수식 데이터 {len(current_products):,}개 중")
    print(f"   - 기존 유지 상품: {len(current_products) - len(targets_to_process):,}개")
    print(f"   - 네이버 로직 기준 미달 및 신규 강제 수정 처리 대상: {len(targets_to_process):,}개")
    
    if not targets_to_process:
        print("✅ 새로 처리할 상품이 없습니다. 작업을 종료합니다.")
        save_cache(new_cache)
        return

    print(f"🚀 {len(targets_to_process)}개 상품에 대해 비동기 병렬 LLM 상품명 재구성 시작...")
    tasks = [call_llm_with_retry(target, confirmed_pool) for target in targets_to_process]
    llm_results = await asyncio.gather(*tasks)
    
    id_update_mapping = {target["id"]: final_name for target, final_name in zip(targets_to_process, llm_results)}
    
    for target, final_name in zip(targets_to_process, llm_results):
        new_cache[target["id"]] = {
            "origin_name": target["name"],
            "hash": calculate_hash(target["name"]),
            "recomposed_name": final_name
        }

    print("💾 raw 시트 G열 영역에 순수 벌크 데이터 적재를 준비 중...")
    
    max_row_num = max(p["row_num"] for p in current_products)
    g_col_output = []
    
    for r in range(2, max_row_num + 1):
        matching_product = next((p for p in current_products if p["row_num"] == r), None)
        
        if matching_product:
            p_id = matching_product["id"]
            if p_id in id_update_mapping:
                g_col_output.append([id_update_mapping[p_id]])
            else:
                g_col_output.append([matching_product["current_result"]])
        else:
            g_col_output.append([""])
            
    chunk_size = 10000
    total_output_len = len(g_col_output)
    print(f"⚡ 총 {total_output_len:,}행의 G열 상품명 리스트를 {chunk_size:,}행씩 분할 업로드합니다.")
    
    for i in range(0, total_output_len, chunk_size):
        chunk = g_col_output[i:i + chunk_size]
        start_row = i + 2  
        range_string = f"G{start_row}"
        
        sheet.update(range_name=range_string, values=chunk)
        print(f"   └ [G열 단독 업로드] {start_row:,} ~ {start_row + len(chunk) - 1:,} 행 완료")

    save_cache(new_cache)
    print("📝 로컬 캐시 파일(product_cache.json) 동기화 완료!")

if __name__ == "__main__":
    asyncio.run(main())
