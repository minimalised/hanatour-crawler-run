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
    text = text.replace("_", " ")
    text = re.sub(r'[\[\(]\d*[가-힣]*세일[\]\)]', ' ', text)
    text = re.sub(r'[\[\(]\d*[가-힣]*특가[\]\)]', ' ', text)
    text = re.sub(r'[\[\(]\d*[가-힣]*출발[\]\)]', ' ', text)
    text = re.sub(r'\[\s*NO\s*(유류세|유류부담|인상|부담)[^\]]*\]', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'NO쇼핑', '노쇼핑', text, flags=re.IGNORECASE)
    text = re.sub(r'NO팁', '노팁', text, flags=re.IGNORECASE)
    text = re.sub(r'NO옵션', '노옵션', text, flags=re.IGNORECASE)
    
    trash_words = [
        "선착순특가", "실속여행", "신상품", "대박특가", "출발확정", "세미팩", 
        "[SK스토아 에디션]", "[USJ 오피셜 호텔]", "[USJ와패키지를한번에]", "[VIP]",
        "HIT!상품", "MD추천", "BEST상품", "추천상품", "효도락", "효율성甲", "얼리마켓", "스마트초이스", "유류세인상", "유류부담",
        "세이브", "우리끼리", "브랜드미적용", "스탠다드", "프리미엄", 
        "제우스 셀렉트", "제우스 시그니처", "현지투어플러스", "내나라여행", "제우스", "ZEUS", "하나투어"
    ]
    for word in trash_words:
        text = text.replace(word, "")
        
    text = re.sub(r'\bNO\b', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'\b\d{7,}\b', '', text)
    text = re.sub(r'[^가-힣A-Za-z0-9\s\-\~\[\]\(\)\&]', '', text)
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
    
    print(f"⏳ '{SOURCE_SHEET_NAME}' 시트에서 데이터를 가져오는 중...")
    all_values = sheet.get_all_values()
    
    if not all_values:
        return sheet, [], []
        
    rows = all_values[1:]
    processed_rows = []
    
    # 헤더 이름과 상관없이 오직 물리적 열(A=0, B=1, G=6)만 타겟팅
    id_idx = 0
    name_idx = 1
    result_idx = 6
    
    for idx, row in enumerate(rows, start=2): 
        while len(row) < 7:
            row.append("")
            
        p_id = str(row[id_idx]).strip()   
        p_name = str(row[name_idx]).strip() 
        current_result = str(row[result_idx]).strip() 
        
        if p_id and p_name and p_id.lower() not in ["id", "상품id"] and not p_id.startswith("idtitle"): 
            processed_rows.append({
                "row_num": idx,
                "id": p_id,
                "name": p_name,
                "current_result": current_result
            })
    return sheet, processed_rows, all_values[0]

def load_cache() -> Dict[str, Dict]:
    if os.path.exists(CACHE_FILE_PATH):
        with open(CACHE_FILE_PATH, "r", encoding="utf-8") as f:
            try: return json.load(f)
            except: return {}
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
        system_content = (
            "너는 네이버 쇼핑 검색 노출 로직(SEO) 최적화 카피라이터야.\n"
            "목적: [원본 상품명]에서 홍보 수식어와 내부 코드를 빼고 25자~45자 사이의 완성형 상품명 생성.\n\n"
            "⚠️ [필수 규칙]\n"
            "- 글자 수 필수 준수: 공백 포함 최소 25자 ~ 최대 45자 (절대 엄수)\n"
            "- 항공 코드 보존: 'CZ314', 'KE433' 등은 대괄호만 빼고 무조건 포함.\n"
            "- 물결표 변경: '~' 기호는 무조건 대시('-')로 변경\n"
            "- 제거 대상: '우리끼리', 'ZEUS', '스탠다드', '프리미엄', 'HIT', '추천' 및 영문 'NO'는 출력 금지.\n"
            "- 필수 포함: '노쇼핑', '노팁' 정보는 문장 뒤쪽에 포함.\n"
            "- 기호 금지: 오직 공백으로만 단어 구분.\n"
            "정해진 상품명 딱 한 줄만 출력해라. 부연설명 금지."
        )
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": f"원본: {origin_name}"}
        ]
        
        while retry_count < 3:
            try:
                current_temp = 0.2 if retry_count == 0 else 0.0
                response = await aclient.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=messages,
                    max_tokens=50,  
                    temperature=current_temp
                )
                suggested_name = response.choices[0].message.content.strip()
                suggested_name = suggested_name.replace('~', '-')
                suggested_name = re.sub(r'\bNO\b', ' ', suggested_name, flags=re.IGNORECASE)
                suggested_name = re.sub(r'[\[\]_,\!#+/\(\)]', ' ', suggested_name)
                suggested_name = re.sub(r'\s+', ' ', suggested_name).strip()
                
                if suggested_name not in confirmed_pool and 25 <= len(suggested_name) <= 45:
                    confirmed_pool.add(suggested_name)
                    return suggested_name
                
                retry_count += 1
                messages = [
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": f"이전 결과({suggested_name})는 조건 위반입니다. 규칙과 글자 수(25자~45자)를 맞춰 다시 딱 한 줄만 내놓으세요. 원본: {origin_name}"}
                ]
            except Exception as e:
                print(f"❌ API 에러 발생 ({origin_name}): {e}")
                await asyncio.sleep(1)
                retry_count += 1
                
        fallback_name = origin_name.replace('~', '-')
        fallback_name = re.sub(r'[\[\]_,\!#+/\(\)]', ' ', fallback_name)
        delete_keywords = ['HIT!상품', 'MD추천', 'BEST상품', '추천상품', '효도락', '효율성甲', '얼리마켓', '스마트초이스', '유류세인상', '유류부담', '세이브', '우리끼리', '브랜드미적용', '스탠다드', '프리미엄', '제우스', 'ZEUS']
        for kw in delete_keywords:
            fallback_name = fallback_name.replace(kw, '')
        fallback_name = re.sub(r'\bNO\b', ' ', fallback_name, flags=re.IGNORECASE).replace('NO쇼핑', '노쇼핑').replace('NO팁', '노팁').replace('NO옵션', '노옵션')
        fallback_name = re.sub(r'\s+', ' ', fallback_name).strip()
        
        if len(fallback_name) > 45: fallback_name = fallback_name[:41] + " 패키지여행"
        elif len(fallback_name) < 25: fallback_name = fallback_name + " 추천 패키지 여행"
        confirmed_pool.add(fallback_name)
        return fallback_name

async def main():
    sheet, current_products, headers = load_google_sheet_data()
    old_cache = load_cache()
    
    if not current_products:
        print("ℹ️ 처리할 상품 데이터가 없습니다.")
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
            p["current_result"].endswith(("포함", "제공", "특전")) or 
            not (25 <= len(p["current_result"]) <= 45) or
            " " not in p["current_result"][-8:]
        )
        
        if has_corrupted_result:
            if p_id in new_cache: del new_cache[p_id]
            targets_to_process.append(p)
        else:
            is_new = p_id not in cleaned_cache
            is_changed = not is_new and cleaned_cache[p_id]["hash"] != current_hash
            is_row_shifted = not is_new and p["current_result"] != cleaned_cache[p_id]["recomposed_name"]
            if is_new or is_changed or is_row_shifted:
                targets_to_process.append(p)

    # 🧪 [테스트 제한] 최대 100개만 선별하여 진행
    targets_to_process = targets_to_process[:100]

    confirmed_pool = set()
    for p in current_products:
        p_id = p["id"]
        if p_id in new_cache and p_id not in {t["id"] for t in targets_to_process}:
            confirmed_pool.add(new_cache[p_id]["recomposed_name"])
            
    print(f"📊 테스트 가동: 총 {len(targets_to_process)}개의 타겟 상품을 정제합니다.")
    
    if not targets_to_process:
        print("✅ 새로 반영할 테스트 항목이 없습니다.")
        return

    print(f"🚀 비동기 LLM 정제 요청 중...")
    tasks = [call_llm_with_retry(target, confirmed_pool) for target in targets_to_process]
    llm_results = await asyncio.gather(*tasks)
    
    id_update_mapping = {target["id"]: final_name for target, final_name in zip(targets_to_process, llm_results)}
    
    for target, final_name in zip(targets_to_process, llm_results):
        new_cache[target["id"]] = {
            "origin_name": target["name"],
            "hash": calculate_hash(target["name"]),
            "recomposed_name": final_name
        }

    print("💾 시트 G열 업데이트 행 매핑 중...")
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
            
    chunk_size = 1000  
    total_output_len = len(g_col_output)
    print(f"⚡ 총 {total_output_len:,}행 구조의 데이터를 분할 적재합니다.")
    
    for i in range(0, total_output_len, chunk_size):
        chunk = g_col_output[i:i + chunk_size]
        start_row = i + 2  
        range_string = f"G{start_row}:G{start_row + len(chunk) - 1}"
        
        sheet.update(range_string, chunk)
        print(f"   └ [업데이트 완료] {range_string}")

    save_cache(new_cache)
    print("📝 테스트 캐시 백업 성공.")

if __name__ == "__main__":
    asyncio.run(main())
