import os
import json
import time
import hashlib
import asyncio
from typing import List, Dict

import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from openai import AsyncOpenAI

# ==========================================
# [공통 설정] GitHub Secrets 및 경로
# ==========================================
SPREADSHEET_ID = os.environ.get("SOURCE_SPREADSHEET_ID")
SOURCE_SHEET_NAME = "raw"
CACHE_FILE_PATH = "product_cache.json"

# 동시 요청 제한 (세마포어)
MAX_CONCURRENT_TASKS = 10 
semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

# OpenAI 비동기 클라이언트
aclient = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))


def extract_date_from_code(code):
    code_str = str(code) if pd.notnull(code) else ""
    if len(code_str) >= 12:
        return code_str[6:12]
    return "999999"


def calculate_hash(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()


def load_google_sheet_data(doc):
    sheet = doc.worksheet(SOURCE_SHEET_NAME)
    all_values = sheet.get_all_values()
    
    if not all_values:
        return sheet, []
    
    rows = all_values[1:]
    processed_rows = []
    
    for idx, row in enumerate(rows, start=2):
        while len(row) < 7:
            row.append("")
            
        p_id = str(row[0]).strip()   # A열: 상품 고유 ID
        p_name = str(row[1]).strip() # B열: 원본 상품명
        current_result = str(row[6]).strip() # G열: 기존 결과물
        
        if p_id and p_name:
            processed_rows.append({
                "row_num": idx,
                "id": p_id,
                "name": p_name,
                "current_result": current_result
            })
            
    return sheet, processed_rows


def load_cache() -> Dict[str, Dict]:
    if os.path.exists(CACHE_FILE_PATH):
        with open(CACHE_FILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache_data: Dict[str, Dict]):
    with open(CACHE_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache_data, f, ensure_ascii=False, indent=4)


# ==========================================
# 🛑 네이버 가이드라인 준수 (API 딱 1번만 호출)
# ==========================================
async def call_llm_clean(target: Dict) -> str:
    """중복 체크/재시도/ID 붙이기 절대 안 함. 네이버 쇼핑 노출용 순수 상품명만 1회 호출하여 반환."""
    async with semaphore:
        origin_name = target["name"]
        
        try:
            response = await aclient.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system", 
                        "content": (
                            "너는 네이버 쇼핑 입점 및 검색 최적화(SEO) 전문 마케터야. "
                            "입력된 상품명을 네이버 쇼핑 상품명 가이드라인을 '완벽하게' 준수하여 수정해줘.\n\n"
                            "⚠️ [네이버 쇼핑 필수 가이드라인]\n"
                            "1. 글자 수 제약: 최종 상품명은 공백 포함 최소 32자 ~ 최대 45자 사이로 구성할 것. (45자 절대 초과 금지)\n"
                            "2. 특수문자 전면 금지: 대괄호[], 쉼표(,), 느낌표(!), 물결(~), 플러스(+), 언더바(_) 등 모든 문장부호와 특수문자를 절대 포함하지 마라. 오직 띄어쓰기와 한글, 숫자, 영문만 허용한다.\n"
                            "3. 홍보성 키워드 삭제: '선착순특가', '한정특가', '대박', '출발확정', '실속' 같은 네이버 제재 대상 광고 단어는 무조건 삭제해라.\n"
                            "4. 출력 형식: 주의사항이나 설명, 기호 없이 네이버 쇼핑에 바로 등록할 깨끗한 상품명 '딱 한 줄'만 출력해라."
                        )
                    },
                    {"role": "user", "content": f"원본 상품명: {origin_name}"}
                ],
                max_tokens=80,
                temperature=0.4
            )
            
            # GPT가 생성한 순수한 이름 그대로 리턴 (ID 따위 절대 안 붙임)
            return response.choices[0].message.content.strip()
            
        except Exception as e:
            print(f"❌ API 에러 발생 ({origin_name}): {e}")
            # 에러 발생 시에만 노이즈 단어 기본 제거 후 반환
            fallback = origin_name.replace("[", "").replace("]", "").replace("출발확정", "").replace("한정특가", "").strip()
            return fallback[:40]


async def run_second_task_async(doc):
    print("\n🚀 2. 'raw' 시트 대상 상품명 네이버 최적화 가공 (2차 태스크) 시작...")
    sheet, current_products = load_google_sheet_data(doc)
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
        
        is_new = p_id not in cleaned_cache
        is_changed = not is_new and cleaned_cache[p_id]["hash"] != current_hash
        is_missing_result = not p["current_result"]
        
        if is_new or is_changed or is_missing_result:
            targets_to_process.append(p)
            
    print(f"📊 분석 결과: 전체 {len(current_products):,}개 중 신규/변경 처리 대상: {len(targets_to_process):,}개")
    
    if not targets_to_process:
        print("✅ 새로 처리할 상품이 없습니다. 2차 작업을 종료합니다.")
        save_cache(new_cache)
        return

    print(f"🤖 {len(targets_to_process)}개 상품에 대해 비동기 네이버 SEO 가공 시작 (1단어당 1회 호출)...")
    
    # 중복 체크 싹 빼고 1대1로 깔끔하게 매핑
    tasks = [call_llm_clean(target) for target in targets_to_process]
    llm_results = await asyncio.gather(*tasks)
    
    cells_to_update = []
    for target, final_name in zip(targets_to_process, llm_results):
        cell = gspread.cell.Cell(row=target["row_num"], col=7, value=final_name) # G열
        cells_to_update.append(cell)
        
        new_cache[target["id"]] = {
            "origin_name": target["name"],
            "hash": calculate_hash(target["name"]),
            "recomposed_name": final_name
        }

    if cells_to_update:
        print("💾 구글 스프레드시트 G열에 최종 상품명 적재 중...")
        sheet.update_cells(cells_to_update)
        print(f"✅ 구글 스프레드시트 {len(cells_to_update):,}개 행 업데이트 완료!")
        
    save_cache(new_cache)
    print("📝 로컬 캐시 파일(product_cache.json) 갱신 완료!")


def main():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    google_json_raw = os.environ.get("GOOGLE_JSON_RAW")
    
    if not google_json_raw or not SPREADSHEET_ID:
        raise ValueError("GitHub Secrets 설정을 확인해주세요.")
        
    service_account_info = json.loads(google_json_raw)
    creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    gc = gspread.authorize(creds)
    
    # [TASK 1] '판매상품리스트' 중복 제거 및 적재
    print("🛒 1. '판매상품리스트' 시트 데이터 로드 중...")
    doc = gc.open_by_key(SPREADSHEET_ID)
    source_sheet = doc.worksheet("판매상품리스트")
    all_values = source_sheet.get_all_values()
    
    if not all_values:
        return
        
    header = all_values[0]
    data = all_values[1:]
    
    df = pd.DataFrame(data, columns=header)
    df['sort_key'] = df['판매상품코드'].apply(extract_date_from_code)
    df = df.sort_values(by='sort_key', ascending=True).drop(columns=['sort_key'])
    df = df.dropna(subset=['판매상품명'])
    df = df[df['판매상품명'].str.strip() != ""]
    df_cleaned = df.drop_duplicates(subset=['판매상품명'], keep='first').copy()
    
    final_output = [df_cleaned.columns.tolist()] + df_cleaned.values.tolist()
    total_rows = len(final_output)

    try:
        target_sheet = doc.worksheet("상품명_중복제거")
        target_sheet.clear()
        target_sheet.resize(rows=total_rows, cols=len(header))
    except gspread.exceptions.WorksheetNotFound:
        target_sheet = doc.add_worksheet(title="상품명_중복제거", rows=total_rows, cols=len(header))
        
    chunk_size = 10000
    for i in range(0, total_rows, chunk_size):
        chunk = final_output[i:i + chunk_size]
        target_sheet.update(range_name=f"A{i+1}", values=chunk)

    print(f"✨ 1차 작업 완료.")

    # 쿨타임 대기
    print(f"\n⏳ 60초 대기...")
    time.sleep(60)

    # [TASK 2] 'raw' 시트 네이버 쇼핑 최적화 변환 실행
    asyncio.run(run_second_task_async(doc))
    print("\n🏁 모든 네이버 쇼핑 노출 파이프라인이 정상 종료되었습니다.")


if __name__ == "__main__":
    main()
