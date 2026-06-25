import os
import json
import hashlib
import asyncio
from typing import List, Dict, Set

import gspread
from google.oauth2.service_account import Credentials
from openai import AsyncOpenAI

# ==========================================
# 1. GitHub Secrets 기반 설정 및 초기화
# ==========================================
# ⭕ [교정 완료] 캡처본에 있던 실제 변수명 SOURCE_SPREADSHEET_ID로 정확히 바꿨습니다!
SPREADSHEET_KEY = os.environ.get("TARGET_SPREADSHEET_ID") 
SOURCE_SHEET_NAME = "raw"            # 원본 데이터 시트명
CACHE_FILE_PATH = "product_cache.json"          # GitHub 레포지토리에 저장할 캐시 파일 경로

# Rate Limit 방지를 위한 동시 요청 제한 (세마포어)
MAX_CONCURRENT_TASKS = 10 
semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

# OpenAI 비동기 클라이언트 (GitHub Secrets의 OPENAI_API_KEY 사용)
aclient = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

# MD5 해시 함수 (상품명 변경 감지용)
def calculate_hash(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()

# ==========================================
# 2. 데이터 로드 및 캐시 관리
# ==========================================
def load_google_sheet_data():
    """구글 스프레드시트에서 데이터를 단 1번의 호출로 읽어옵니다."""
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
        return sheet, []
    
    headers = all_values[0]
    rows = all_values[1:]
    
    processed_rows = []
    for idx, row in enumerate(rows, start=2):
        while len(row) < 7:
            row.append("")
            
        p_id = str(row[0]).strip()   # A열: 상품 고유 ID
        p_name = str(row[1]).strip() # B열: 원본 상품명
        current_result = str(row[6]).strip() # G열: 기존 재조합 결과물
        
        if p_id and p_name:
            processed_rows.append({
                "row_num": idx,
                "id": p_id,
                "name": p_name,
                "current_result": current_result
            })
            
    return sheet, processed_rows

def load_cache() -> Dict[str, Dict]:
    """기존 캐시 파일(JSON)을 로드합니다."""
    if os.path.exists(CACHE_FILE_PATH):
        with open(CACHE_FILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_cache(cache_data: Dict[str, Dict]):
    """업데이트된 캐시를 파일(JSON)로 저장합니다."""
    with open(CACHE_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache_data, f, ensure_ascii=False, indent=4)

# ==========================================
# 3. 비동기 LLM 호출 및 중복 검증 루프
# ==========================================
async def call_llm_with_retry(target: Dict, confirmed_pool: Set[str]) -> str:
    """LLM을 호출하고, 중복이 발생하면 최대 3회 재시도합니다."""
    async with semaphore:
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
                                "너는 커머스 마케팅 전문 카피라이터야. 입력된 상품명을 분석하여 "
                                "소비자의 클릭을 유도할 수 있는 매력적인 상품명으로 재구성해줘.\n"
                                "⭐ [핵심 규칙]\n"
                                "1. 불필요한 특수문자나 중복 키워드는 제거할 것\n"
                                "2. 브랜드명, 핵심 속성, 카테고리가 자연스럽게 연결되게 할 것\n"
                                "3. 최종 결과물은 오직 재구성된 상품명 딱 한 줄만 반환할 것.\n"
                                f"{extra_prompt}"
                            )
                        },
                        {"role": "user", "content": f"원본 상품명: {origin_name}"}
                    ],
                    max_tokens=80,
                    temperature=0.4
                )
                
                suggested_name = response.choices[0].message.content.strip()
                
                if suggested_name not in confirmed_pool:
                    confirmed_pool.add(suggested_name)
                    return suggested_name
                
                retry_count += 1
                extra_prompt = f"\n⚠️ [중요 안내] 이전에 '{suggested_name}'라는 결과가 이미 사용 중입니다. 다른 키워드를 써서 다른 형태의 이름으로 재구성하세요."
                print(f"⚠️ 중복 감지 ({origin_name} -> {suggested_name}) - 재시도 {retry_count}회차")
                
            except Exception as e:
                print(f"❌ API 에러 발생 ({origin_name}): {e}")
                await asyncio.sleep(1)
                retry_count += 1
                
        sanitized_name = origin_name.replace("[", "").replace("]", "")[:15]
        fallback_name = f"{sanitized_name}_{target['id']}"
        confirmed_pool.add(fallback_name)
        return fallback_name

# ==========================================
# 4. 메인 오케스트레이터 (전체 흐름 제어)
# ==========================================
async def main():
    print("🛒 1. 구글 스프레드시트 데이터 및 로컬 캐시 로드 중...")
    sheet, current_products = load_google_sheet_data()
    old_cache = load_cache()
    
    if not current_products:
        print("ℹ️ 처리할 상품 데이터가 없습니다.")
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
        
        if is_new or is_changed or is_missing_result:
            targets_to_process.append(p)
            
    print(f"📊 분석 결과: 전체 {len(current_products):,}개 중")
    print(f"   - 기존 유지 상품: {len(current_products) - len(targets_to_process):,}개")
    print(f"   - 신규/변경 처리 대상: {len(targets_to_process):,}개")
    
    if not targets_to_process:
        print("✅ 새로 처리할 상품이 없습니다. 작업을 종료합니다.")
        save_cache(new_cache)
        return

    print(f"🚀 {len(targets_to_process)}개 상품에 대해 비동기 병렬 LLM 처리를 시작합니다...")
    
    tasks = [call_llm_with_retry(target, confirmed_pool) for target in targets_to_process]
    llm_results = await asyncio.gather(*tasks)
    
    cells_to_update = []
    for target, final_name in zip(targets_to_process, llm_results):
        cell = gspread.cell.Cell(row=target["row_num"], col=7, value=final_name)
        cells_to_update.append(cell)
        
        new_cache[target["id"]] = {
            "origin_name": target["name"],
            "hash": calculate_hash(target["name"]),
            "recomposed_name": final_name
        }

    if cells_to_update:
        print("💾 구글 스프레드시트 G열에 일괄 데이터를 반영하고 있습니다...")
        sheet.update_cells(cells_to_update)
        print(f"✅ 구글 스프레드시트 {len(cells_to_update):,}개 행 업데이트 완료!")
        
    save_cache(new_cache)
    print("📝 로컬 캐시 파일(product_cache.json) 갱신 완료!")

if __name__ == "__main__":
    asyncio.run(main())
