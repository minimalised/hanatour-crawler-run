import os
import json
import time
import hashlib
import asyncio
from typing import List, Dict, Set

import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from openai import AsyncOpenAI

# ==========================================
# [공통 설정] GitHub Secrets 및 경로
# ==========================================
SPREADSHEET_ID = os.environ.get("SOURCE_SPREADSHEET_ID")
SOURCE_SHEET_NAME = "raw"           # 2차 LLM 작업 대상 시트명
CACHE_FILE_PATH = "product_cache.json"

# Rate Limit 방지를 위한 동시 요청 제한 (세마포어)
MAX_CONCURRENT_TASKS = 10 
semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

# OpenAI 비동기 클라이언트
aclient = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))


# ==========================================
# [1차 작업용] 헬퍼 함수
# ==========================================
def extract_date_from_code(code):
    """기존 앱스 스크립트의 고속 정렬 기준 (판매상품코드에서 날짜 부분 추출)"""
    code_str = str(code) if pd.notnull(code) else ""
    if len(code_str) >= 12:
        return code_str[6:12]
    return "999999"


# ==========================================
# [2차 작업용] 헬퍼 및 비동기 함수
# ==========================================
def calculate_hash(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()

def load_google_sheet_data(doc):
    """구글 스프레드시트 'raw' 시트에서 데이터를 읽어옵니다."""
    sheet = doc.worksheet(SOURCE_SHEET_NAME)
    all_values = sheet.get_all_values()
    
    if not all_values:
        return sheet, []
    
    headers = all_values[0]
    rows = all_values[1:]
    
    processed_rows = []
    for idx, row in enumerate(rows, start=2):
        # G열(7번째 열)까지 공간 확보
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
    if os.path.exists(CACHE_FILE_PATH):
        with open(CACHE_FILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_cache(cache_data: Dict[str, Dict]):
    with open(CACHE_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache_data, f, ensure_ascii=False, indent=4)

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
                                "너는 글로벌 커머스 마케팅 피드 최적화 전문 카피라이터야. "
                                "입력된 원본 상품명을 분석하여 다음 [⚠️ 핵심 제약 가이드라인]을 반드시 '모두' 만족하는 매력적인 상품명으로 재구성해줘.\n\n"
                                "⚠️ [핵심 제약 가이드라인]\n"
                                "1. 글자 수 제약: 최종 상품명은 공백 포함 최소 32자 ~ 최대 45자 사이로 구성할 것. (★45자 절대 초과 금지★)\n"
                                "2. 문장부호 및 특수문자 사용 제한:\n"
                                "   - 최종 상품명 내부에는 쉼표(,), 느낌표(!), 물결(~), 플러스(+), 언더바(_) 같은 부호나 특수문자를 절대 포함할 수 없음.\n"
                                "   - 기간이나 일정 범위를 나타낼 때는 반드시 붙임표 대시 기호(예: '4-5일')로 치환하여 표현할 것.\n"
                                "3. 불필요한 노이즈 텍스트 전면 제거:\n"
                                "   - '선착순특가', '실속여행', '신상품', '대박특가' 등 유치하고 진부한 광고성 홍보 단어는 무조건 삭제할 것.\n"
                                "   - 상품명 뒤나 중간에 붙은 영문+숫자 조합의 마스터 상품 코드(예: '_CHP1002607257C9')는 패턴을 파악하여 전면 배제할 것.\n"
                                "   - 결과물에 '주의:', '경고:', '안내:' 등 시스템 지시어 성격의 텍스트 삽입을 절대 금지함.\n"
                                "4. 정보 보완 및 네이밍 구성 요소:\n"
                                "   - 광고어와 코드를 지워서 남은 텍스트가 짧아지면, 해당 여행지의 대표적인 핵심 명소, 유명 호텔/리조트, 주요 골프장, 항공사 등 소비자가 검색할 만한 실용적인 키워드를 자연스럽게 유추하여 조합해 글자 수(최소 32자)를 채울 것.\n"
                                "5. 출력 형식:\n"
                                "   - 설명이나 서론, 후론 없이 오직 재구성된 상품명 '딱 한 줄'만 반환할 것.\n\n"
                                "💡 [출력 예시 참고]\n"
                                "- 원본: 선착순특가홍콩 4일 실속여행_CHP1002607257C9\n"
                                "- 추천 결과: 홍콩 중심가 침사추이 4일 패키지 여행 명소 관광 국적기 탑승\n\n"
                                f"{extra_prompt}"
                            )
                        },
                        {"role": "user", "content": f"원본 상품명: {origin_name}"}
                    ],
                    max_tokens=80,
                    temperature=0.3
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
# 2차 작업 비동기 메인 컨트롤러
# ==========================================
async def run_second_task_async(doc):
    print("\n🚀 2. 'raw' 시트 대상 상품명 LLM 재조합 (2차 태스크) 시작...")
    sheet, current_products = load_google_sheet_data(doc)
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
        print("✅ 새로 처리할 상품이 없습니다. 2차 작업을 종료합니다.")
        save_cache(new_cache)
        return

    print(f"🤖 {len(targets_to_process)}개 상품에 대해 비동기 병렬 LLM 처리를 시작합니다...")
    
    tasks = [call_llm_with_retry(target, confirmed_pool) for target in targets_to_process]
    llm_results = await asyncio.gather(*tasks)
    
    cells_to_update = []
    for target, final_name in zip(targets_to_process, llm_results):
        cell = gspread.cell.Cell(row=target["row_num"], col=7, value=final_name) # col=7이 G열입니다.
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


# ==========================================
# 동기 전체 메인 함수 (오케스트레이터)
# ==========================================
def main():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    google_json_raw = os.environ.get("GOOGLE_JSON_RAW")
    
    if not google_json_raw or not SPREADSHEET_ID:
        raise ValueError("GitHub Secrets 설정(GOOGLE_JSON_RAW 또는 SOURCE_SPREADSHEET_ID)을 확인해주세요.")
        
    service_account_info = json.loads(google_json_raw)
    creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    gc = gspread.authorize(creds)
    
    # ------------------------------------------
    # [TASK 1] '판매상품리스트' 중복 제거 및 적재
    # ------------------------------------------
    print("🛒 1. '판매상품리스트' 시트에서 55만 행 대용량 데이터 로드 중...")
    doc = gc.open_by_key(SPREADSHEET_ID)
    source_sheet = doc.worksheet("판매상품리스트")
    all_values = source_sheet.get_all_values()
    
    if not all_values:
        print("ℹ️ '판매상품리스트' 시트에 데이터가 없습니다.")
        return
        
    header = all_values[0]
    data = all_values[1:]
    
    df = pd.DataFrame(data, columns=header)
    print(f"📦 로드 완료: 총 {len(df):,}행")
    
    print("⚡ 파이썬 Pandas 고속 정렬 및 '판매상품명' 기준 중복 제거 시작...")
    if '판매상품코드' not in df.columns or '판매상품명' not in df.columns:
        raise KeyError("시트에 '판매상품코드' 또는 '판매상품명' 컬럼이 정확히 존재하는지 확인해주세요.")
        
    df['sort_key'] = df['판매상품코드'].apply(extract_date_from_code)
    df = df.sort_values(by='sort_key', ascending=True).drop(columns=['sort_key'])
    
    df = df.dropna(subset=['판매상품명'])
    df = df[df['판매상품명'].str.strip() != ""]
    df_cleaned = df.drop_duplicates(subset=['판매상품명'], keep='first').copy()
    print(f"🎯 정제 완료: {len(df_cleaned):,}행 남음.")
    
    final_output = [df_cleaned.columns.tolist()] + df_cleaned.values.tolist()
    total_rows = len(final_output)

    print("💾 '상품명_중복제거' 시트에 일괄 적재 시작...")
    required_rows = total_rows
    
    try:
        target_sheet = doc.worksheet("상품명_중복제거")
        target_sheet.clear()
        target_sheet.resize(rows=required_rows, cols=len(header))
        print(f"🧹 기존 '상품명_중복제거' 시트를 비우고 크기를 {required_rows:,}행으로 조정했습니다.")
    except gspread.exceptions.WorksheetNotFound:
        target_sheet = doc.add_worksheet(title="상품명_중복제거", rows=required_rows, cols=len(header))
        print(f"🆕 '상품명_중복제거' 시트를 {required_rows:,}행 크기로 새로 생성했습니다.")
        
    chunk_size = 10000
    print(f"🚀 총 {total_rows:,}행(헤더 포함)을 {chunk_size:,}행씩 나누어 안전하게 업로드합니다.")
    
    for i in range(0, total_rows, chunk_size):
        chunk = final_output[i:i + chunk_size]
        start_row = i + 1
        end_row = i + len(chunk)
        range_string = f"A{start_row}"
        target_sheet.update(range_name=range_string, values=chunk)
        print(f"  └ [진행] {start_row:,} ~ {end_row:,} 행 적재 완료")

    print(f"✨ 1차 작업 완료: '상품명_중복제거' 시트에 최종 반영되었습니다.")

    # ------------------------------------------
    # [중간 대기] API 초과 및 과부하 방지 쿨타임
    # ------------------------------------------
    wait_time = 60
    print(f"\n⏳ API 안정성을 위해 {wait_time}초 동안 대기 후 2차 작업을 시작합니다...")
    time.sleep(wait_time)

    # ------------------------------------------
    # [TASK 2] 'raw' 시트 상품명 LLM 재조합 (비동기 루프 호출)
    # ------------------------------------------
    asyncio.run(run_second_task_async(doc))
    
    print("\n🏁 모든 자동화 파이프라인 프로세스가 완벽하게 종료되었습니다.")


if __name__ == "__main__":
    main()
