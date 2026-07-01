import os
import json
import hashlib
import asyncio
import re
from typing import List, Dict, Set

import gspread
from google.oauth2.service_account import Credentials
from openai import AsyncOpenAI

# ==========================================
# [설정 및 환경 변수]
# ==========================================
SPREADSHEET_KEY = os.environ.get("SOURCE_SPREADSHEET_ID") 
SOURCE_SHEET_NAME = "raw"                        
CACHE_FILE_PATH = "product_cache.json"          

MAX_CONCURRENT_TASKS = 10 
semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
aclient = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

def calculate_hash(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()

# ==========================================
# [데이터 전처리 함수]
# ==========================================
def extract_meta_and_clean(title: str):
    """
    [2色매력]과 같이 한자가 섞인 쓰레기 수식어를 파이썬 단에서 먼저 도려내고,
    알파벳 및 불필요 특수문자를 제거하여 깨끗한 뼈대 데이터를 구축하는 함수
    """
    if not title:
        return "", "", ""
        
    # 1. 출발공항 패턴 파이썬 단에서 선제 추출
    airport_match = re.search(r'(청주|대구|부산|인천|무안|양양|제주)\s*출발', title)
    departure = f"[{airport_match.group(0).strip()}]" if airport_match else ""

    # 2. 일정(박/일) 추출 및 기호 정제 (ex: 4일, 3박5일)
    duration_match = re.search(r'\d+박\s*\d+일|\d+일|\d+박\d+일|\d+~\d+일|\d+-\d+일', title)
    duration = duration_match.group(0).strip() if duration_match else ""
    duration = duration.replace('~', '-') 

    # 3. 알파벳(항공코드), 한자(色 등), 불필요 특수문자 일괄 제거
    clean_title = re.sub(r'[A-Za-z]', ' ', title)
    clean_title = re.sub(r'[^가-힣0-9\s\-\/]', ' ', clean_title)

    # 4. 상품명 오염 및 자질구레한 단어 1차 청소
    kill_words = [
        "2색상품", "2색매력", "매력", "두도시한번에", "두도시", "한번에", "시티", 
        "골프장맵", "거리측정", "이용권", "추천", "쏙쏙", "오키짱쇼", "국제거리", 
        "스테이크정식", "갓성비", "취향저격"
    ]
    for kw in kill_words:
        clean_title = clean_title.replace(kw, " ")

    # 5. 일정 뒤에 나오는 잡다한 텍스트 찌꺼기 1차 컷 (선택 사항)
    if duration:
        for pat in [duration, duration.replace('-', '~')]:
            if pat in clean_title:
                clean_title = clean_title.split(pat)[0].strip() + " " + duration
                break

    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
    return departure, duration, clean_title

# ==========================================
# [사후 검증 함수]
# ==========================================
def validate_naver_title(title: str) -> bool:
    """최소한의 비정상 출력 검증 (글자 수가 아닌 금지어 및 깨짐 위주)"""
    if not title:
        return False
        
    # 문장 끝에 '까', '포', '제' 등 한 글자만 남고 끝나는 비정상 패턴 필터링
    if re.search(r'\s[가-힣]$', title):
        return False
        
    forbidden = ["추천", "베스트", "대박", "특가", "명문", "지역", "골프장맵", "이용권", "2색상품", "쏙쏙"]
    if any(word in title for word in forbidden):
        return False
    return True

# ==========================================
# [구글 시트 연동 로직]
# ==========================================
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
    
    all_values = sheet.get_all_values()
    if not all_values:
        return sheet, [], []
        
    rows = all_values[1:]
    processed_rows = []
    
    for idx, row in enumerate(rows, start=2): 
        while len(row) < 7:
            row.append("")
        p_id = str(row[0]).strip()   
        p_name = str(row[1]).strip() 
        current_result = str(row[6]).strip() 
        
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

# ==========================================
# [LLM 호출 및 비동기 엔진 - 글자수 무제한 해제]
# ==========================================
async def call_llm_with_retry(target: Dict, confirmed_pool: Set[str]) -> str:
    async with semaphore:
        departure, duration, cleaned_title = extract_meta_and_clean(target["name"])
        
        options = ""
        if "NO쇼핑" in target["name"] or "노쇼핑" in target["name"]: options += "노쇼핑 "
        if "NO팁" in target["name"] or "노팁" in target["name"]: options += "노팁 "
        if "NO옵션" in target["name"] or "노옵션" in target["name"]: options += "노옵션 "
        options = options.strip()

        # [🚨 리미트 완전 전면 철폐] 글자 제한을 없애고 오직 단어 완결성만 명령
        prompt = f"""
당신은 여행 상품명 정제 자동화 로봇입니다. 
글자 수 제한(25자~45자) 규격에 맞추려고 단어를 억지로 자르거나 축약하지 마십시오. 
문장 중간에 단어가 절대 잘리지 않도록 안전하고 완성도 높은 명사구 형태의 상품명을 생성하세요. 

[입력 정형 데이터]
- 지정 출발지: "{departure}"
- 여행 일정: "{duration}"
- 주요 지역/도시명: "{cleaned_title}"
- 필수 옵션 문구: "{options}"

[📋 조립 규칙]
1. 🚫 **단어 절대 절단 금지**: 문장 끝이나 중간이 '까', '포', '제' 같이 한 글자만 남고 잘리는 현상은 절대 금지합니다. 단어는 무조건 완성된 형태로만 출력되어야 합니다.
2. 📏 **글자 수 제약 해제**: 길이가 더 짧아지거나 훨씬 더 길어져도 완벽히 무관하니, 텍스트가 잘리지 않고 온전하게 표현되는 것을 최우선으로 하십시오.
3. 🗺️ **어순 공식**: {{지정 출발지}} + {{주요 지역/도시명}} + {{여행 일정}} + {{필수 옵션 문구}} + 패키지여행
4. 🚫 **알파벳 출력 금지**: 영문 항공 코드나 알파벳은 완전히 배제하십시오.

최종 완성된 정제 상품명 '딱 한 줄'만 JSON 포맷으로 출력하십시오.
"""

        # JSON 스키마에서 글자수 관련 메타 제약을 완전히 제거
        json_schema_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "naver_seo_flexible_title_schema",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "refined_title": {
                            "type": "string",
                            "description": "글자 수 제한 없이 단어가 온전하게 마감된 최종 정제 상품명"
                        }
                    },
                    "required": ["refined_title"],
                    "additionalProperties": False
                }
            }
        }

        retry_count = 0
        while retry_count < 3:
            try:
                response = await aclient.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant that outputs compliant JSON based on the provided schema."},
                        {"role": "user", "content": prompt}
                    ],
                    response_format=json_schema_format,
                    temperature=0.1
                )
                
                res_json = json.loads(response.choices[0].message.content)
                suggested_name = res_json.get("refined_title", "").strip()
                
                # 기호 사후 보정 및 깔끔한 공백 정리
                suggested_name = re.sub(r'[\[\]_,\!#+/\(\)A-Za-z]', ' ', suggested_name)
                if departure and not suggested_name.startswith(departure):
                    clean_opt = suggested_name.replace(departure.replace("[","").replace("]",""), "")
                    clean_opt = re.sub(r'^[ \t\s\-]+', '', clean_opt).strip()
                    suggested_name = f"{departure} {clean_opt}"
                
                suggested_name = re.sub(r'\s+', ' ', suggested_name).strip()
                
                # 사후 검증 (끝자리 잘림 현상이 없는지 최종 체크)
                if suggested_name not in confirmed_pool and validate_naver_title(suggested_name):
                    confirmed_pool.add(suggested_name)
                    return suggested_name
                
                retry_count += 1
            except Exception:
                await asyncio.sleep(0.3)
                retry_count += 1
                
        # 3회 실패 시 Fallback 로직도 글자수 리미트 삭제
        fallback_name = cleaned_title
        if departure:
            fallback_name = f"{departure} {fallback_name}"
        if options:
            fallback_name = f"{fallback_name} {options}"
        if not fallback_name.endswith("패키지여행"):
            fallback_name = f"{fallback_name} 패키지여행"
            
        final_fallback = re.sub(r'\s+', ' ', fallback_name).strip()
        confirmed_pool.add(final_fallback)
        return final_fallback

# ==========================================
# [메인 실행 엔진]
# ==========================================
async def main():
    print(f"🛒 1. 유연한 글자수 자동화 구조화 엔진 가동...")
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
        
        # 비정상적인 종결('까' 등)이나 찌꺼기가 들어간 경우 무조건 타겟팅에 포함하여 재추출
        has_corrupted_result = (
            not p["current_result"] or
            "_" in p["current_result"] or 
            re.search(r'\s[가-힣]$', p["current_result"]) or
            p["current_result"].endswith(("자유쇼핑", "골프장맵", "두도시"))
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

    # 🧪 안전을 위해 상위 100개 슬라이싱 (실배포시 아래 라인을 주석처리하거나 삭제하세요)
    targets_to_process = targets_to_process[:100]
    
    confirmed_pool = set()
    for p in current_products:
        p_id = p["id"]
        if p_id in new_cache and p_id not in {t["id"] for t in targets_to_process}:
            confirmed_pool.add(new_cache[p_id]["recomposed_name"])
            
    print(f"📊 처리 타겟팅: 총 {len(targets_to_process)}개의 상품을 정제합니다.")
    if not targets_to_process:
        print("✅ 새로 반영할 항목이 없습니다.")
        return

    print(f"🚀 구조화 비동기 병렬 요청 호출...")
    tasks = [call_llm_with_retry(target, confirmed_pool) for target in targets_to_process]
    llm_results = await asyncio.gather(*tasks)
    
    id_update_mapping = {target["id"]: final_name for target, final_name in zip(targets_to_process, llm_results)}
    
    for target, final_name in zip(targets_to_process, llm_results):
        new_cache[target["id"]] = {
            "origin_name": target["name"],
            "hash": calculate_hash(target["name"]),
            "recomposed_name": final_name
        }

    print("💾 시트 G열 순번 매핑 및 배치 적재 중...")
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
    
    for i in range(0, total_output_len, chunk_size):
        chunk = g_col_output[i:i + chunk_size]
        start_row = i + 2  
        range_string = f"G{start_row}:G{start_row + len(chunk) - 1}"
        sheet.update(range_string, chunk)
        print(f"   └ [시트 반영 완수] {range_string}")

    save_cache(new_cache)
    print("📝 캐시 백업 성공. 프로세스 완료.")

if __name__ == "__main__":
    asyncio.run(main())
