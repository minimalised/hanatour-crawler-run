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
    text = re.sub(r'\b(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9]{8,}\b', '', text)
    text = re.sub(r'\b[A-Z]{3,}\d{3,}[A-Z]*\b', '', text) 
    text = text.replace("_", " ")
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
                                "너는 글로벌 커머스 마케팅 피드 최적화 전문 카피라이터야. "
                                "입력된 원본 상품명을 문맥적으로 분석하여 다음 [⚠️ 핵심 제약 가이드라인]을 만족하는 매력적인 상품명으로 재구성해줘.\n\n"
                                "⚠️ [핵심 제약 가이드라인]\n"
                                "1. 최종 상품명은 최소 15자 ~ 최대 45자 사이로 구성할 것. 짧아도 좋으니 이상한 코드를 강제로 붙여서 글자 수를 늘리지 마라.\n"
                                "2. 최종 상품명 내부에는 쉼표(,), 느낌표(!), 물결(~), 플러스(+), 언더바(_) 같은 부호나 특수문자를 절대 포함할 수 없음.\n"
                                "3. 결과물에 영문 알파벳(A-Z, a-z)이나 언더바(_)가 포함된 마스터 코드는 단 한 글자도 진입할 수 없음.\n"
                                "4. '9일 여행', '10일 패키지', '5일 일정', '4성급 호텔' 등 여행 상품의 중요한 정보가 되는 숫자와 결합된 일정 단어는 절대로 지우지 말고 무조건 포함시켜라.\n"
                                "5. '선착순특가', '실속여행', '신상품', '대박특가', '출발확정', '세미팩' 등 유치하고 진부한 광고성 홍보 단어는 무조건 삭제할 것.\n"
                                "6. 설명이나 서론, 후론 없이 오직 재구성된 상품명 '딱 한 줄'만 반환할 것.\n\n"
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
                
                if suggested_name not in confirmed_pool and len(suggested_name) >= 10:
                    confirmed_pool.add(suggested_name)
                    return suggested_name
                
                retry_count += 1
                extra_prompt = f"\n⚠️ 이전에 나온 결과와 중복되거나 너무 짧습니다. 다르게 재구성하세요."
                
            except Exception as e:
                print(f"❌ API 에러 발생 ({origin_name}): {e}")
                await asyncio.sleep(1)
                retry_count += 1
                
        fallback_name = re.sub(r'[A-Za-z_]', '', origin_name).strip()
        if not fallback_name:
            fallback_name = "추천 해외 여행 상품"
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
    
    confirmed_pool = set()
    for p in current_products:
        p_id = p["id"]
        if p_id in cleaned_cache and p["current_result"] == cleaned_cache[p_id]["recomposed_name"]:
            confirmed_pool.add(p["current_result"])

    targets_to_process = []
    new_cache = cleaned_cache.copy()
    
    for p in current_products:
        p_id = p["id"]
        current_hash = calculate_hash(p["name"])
        
        is_new = p_id not in cleaned_cache
        is_changed = not is_new and cleaned_cache[p_id]["hash"] != current_hash
        is_missing_result = not p["current_result"]
        is_row_shifted = not is_new and p["current_result"] != cleaned_cache[p_id]["recomposed_name"]
        
        has_corrupted_result = "_" in p["current_result"] or re.search(r'[A-Za-z]', p["current_result"])
        
        if is_new or is_changed or is_missing_result or is_row_shifted or has_corrupted_result:
            targets_to_process.append(p)
            
    print(f"📊 분석 결과: 현재 raw 시트 수식 데이터 {len(current_products):,}개 중")
    print(f"   - 기존 유지 상품: {len(current_products) - len(targets_to_process):,}개")
    print(f"   - 신규/변경/오류 수정 처리 대상: {len(targets_to_process):,}개")
    
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
