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

def extract_duration_and_clean(title: str):
    """
    파이썬 단에서 원본 상품명 분석 후 일정(박/일)을 정형 데이터로 선제 추출
    """
    duration_match = re.search(r'\d+박\s*\d+일|\d+일|\d+박\d+일', title)
    duration = duration_match.group(0).strip() if duration_match else "일정미상"
    
    # 원본에서 수천 가지 종류의 '날짜 세일/특가/방영' 대괄호 찌꺼기를 정규식으로 1차 청소
    clean_title = re.sub(r'[\[\(][^\]\)]*(세일|특가|출발|방영|추천|확정|타임|에디션|특전)[\]\)]', ' ', title)
    clean_title = re.sub(r'[\[\(]\d*월\d*일[^\]\)]*[\]\)]', ' ', clean_title)
    clean_title = re.sub(r'[A-Za-z]', ' ', clean_title) # 알파벳 완전 제거로 환각 에러 원천 봉쇄
    clean_title = re.sub(r'[^가-힣0-9\s\-]', ' ', clean_title)
    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
    
    return duration, clean_title

def validate_naver_title(title: str) -> bool:
    """네이버 쇼핑 상품명 규격 사후 검증 가드 (25자~45자 체크)"""
    if not title or not (25 <= len(title) <= 45):
        return False
    # 주관적 금지어 최종 필터
    forbidden = ["추천", "베스트", "대박", "특가", "명문", "지역"]
    if any(word in title for word in forbidden):
        return False
    return True

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
    
    # 물리적 열 매핑 고정 (A=ID, B=원본상품명, G=최종결과)
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

async def call_llm_with_retry(target: Dict, confirmed_pool: Set[str]) -> str:
    async with semaphore:
        # 파이썬 사전 전처리 및 정형 데이터 바인딩
        duration, cleaned_title = extract_duration_and_clean(target["name"])
        
        # 핵심 옵션 단어 필터링
        options = ""
        if "NO쇼핑" in target["name"] or "노쇼핑" in target["name"]: options += "노쇼핑 "
        if "NO팁" in target["name"] or "노팁" in target["name"]: options += "노팁 "
        if "NO옵션" in target["name"] or "노옵션" in target["name"]: options += "노옵션 "
        options = options.strip()

        # 성공 레퍼런스를 벤치마킹한 구조화 퓨샷 프롬프트
        prompt = f"""
당신은 네이버 쇼핑 검색 최적화(SEO) 및 소비자 클릭률(CTR)을 극대화하는 퍼포먼스 마케팅 카피라이팅 전문가입니다.
제공된 여행 정형 데이터를 기반으로 가이드라인을 완벽히 준수하는 군더더기 없는 깔끔한 상품명 1개를 생성하세요.

[입력 정형 데이터]
- 원본 상품명: {cleaned_title}
- 여행 일정: {duration}
- 핵심 필수 옵션: {options}

[🧱 1단계: 상품명 조합 어순 공식]
최종 상품명은 반드시 아래의 어순 구조를 물리적으로 지켜야 하며, 단어 사이에 띄어쓰기를 명확히 하십시오.
- 구조 공식: {{주요 지역/도시명}} + {{여행 일정}} + {{핵심 자산 고유명사 (호텔명/골프장명 등 최대 2개)}} + {{필수 옵션}}

[📋 2단계: 완벽한 상품명 생성을 위한 퓨샷 예시 (Few-Shot)]
아래의 예시를 보고 문장의 깔끔함, 혜택 찌꺼기가 제거된 상태를 그대로 학습하십시오.
- 입력 원본 상품명: "경남 남해 골프 2일 36홀 남해명문골프장 아난티남해 골프장맵 거 패키지여행"
- 출력 결과: "경남 남해 골프 2일 36홀 아난티남해 패키지여행"
- 입력 원본 상품명: "[0504타임세일]오키나와 3일 갓성비추천 나하시내숙박 츄라우미수족관 오키짱쇼 국제거리"
- 출력 결과: "오키나와 3일 나하시내 숙박 츄라우미수족관 패키지여행"

[⚠️ 3단계: 핵심 제약 가이드라인]
1. 글자 수 제약: 최종 생성 문장은 공백 포함 반드시 25자 이상 ~ 45자 이하로 채우십시오.
2. 혜택 단어 박멸: '골프장맵', '거리측정기', '음료교환권', '스테이크정식', '지하철패스' 같은 조잡한 포함사항이나 사은품 명사는 무조건 제외하십시오.
3. 주관적 홍보어 금지: '추천', '베스트', '명문', '최고', '대박', '2색상품' 같은 내부 분류명이나 홍보 수식어는 절대 추가하지 마십시오.
4. 기호 사용 금지: 오직 한글, 숫자, 공백만 허용합니다. 영어 알파벳은 절대 출력 금지합니다.
"""
        # 구조적 JSON 오염 방지를 위한 스키마 강제 지정 가드
        json_schema_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "naver_single_title_schema",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "refined_title": {"type": "string"}
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
                    temperature=0.2 # 온도를 완전히 낮춰 기계적 정교함 극대화
                )
                
                res_json = json.loads(response.choices[0].message.content)
                suggested_name = res_json.get("refined_title", "").strip()
                
                # 부호 사후 정제 가드
                suggested_name = suggested_name.replace('~', '-')
                suggested_name = re.sub(r'[\[\]_,\!#+/\(\)A-Za-z]', ' ', suggested_name)
                suggested_name = re.sub(r'\s+', ' ', suggested_name).strip()
                
                # 사후 검증 검사기 가동
                if suggested_name not in confirmed_pool and validate_naver_title(suggested_name):
                    confirmed_pool.add(suggested_name)
                    return suggested_name
                
                retry_count += 1
            except Exception as e:
                await asyncio.sleep(0.5)
                retry_count += 1
                
        # 3회 실패 시 작동할 강제 사후 보정 가드 (수식어 공해 완전 차단)
        fallback_name = cleaned_title
        kill_pats = ['2색상품', '명문골프장', '지역골프장', '골프장맵', '거리측정', '이용권', '패키지여행', '추천']
        for pat in kill_pats:
            fallback_name = fallback_name.replace(pat, '')
        fallback_name = re.sub(r'\s+', ' ', fallback_name).strip()
        
        if options: fallback_name = f"{fallback_name} {options}"
        
        # 물리적 길이 보정 패딩
        if len(fallback_name) > 45: fallback_name = fallback_name[:41] + " 여행"
        elif len(fallback_name) < 25: fallback_name = fallback_name + " 패키지여행 상품"
        
        confirmed_pool.add(fallback_name)
        return fallback_name

async def main():
    print(f"🛒 1. 구조화 매핑 기반 데이터 가드 엔진 시동...")
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

    # 🧪 [안전 검증] 오차 범위를 완전히 줄이기 위해 상위 100개 선별 테스트 가동
    targets_to_process = targets_to_process[:100]
    
    confirmed_pool = set()
    for p in current_products:
        p_id = p["id"]
        if p_id in new_cache and p_id not in {t["id"] for t in targets_to_process}:
            confirmed_pool.add(new_cache[p_id]["recomposed_name"])
            
    print(f"📊 자가진단 반영 테스트: 총 {len(targets_to_process)}개의 상품을 정제 타겟팅합니다.")
    if not targets_to_process:
        print("✅ 새로 반영할 테스트 항목이 없습니다.")
        return

    print(f"🚀 구조화 JSON 스키마 API 병렬 요청 중...")
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
    
    for i in range(0, total_output_len, chunk_size):
        chunk = g_col_output[i:i + chunk_size]
        start_row = i + 2  
        range_string = f"G{start_row}:G{start_row + len(chunk) - 1}"
        sheet.update(range_string, chunk)
        print(f"   └ [하드웨어 가드 업데이트 완료] {range_string}")

    save_cache(new_cache)
    print("📝 완벽 보정 테스트 캐시 백업 성공.")

if __name__ == "__main__":
    asyncio.run(main())
