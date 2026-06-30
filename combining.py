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
SPREADSHEET_KEY = os.environ.get("SOURCE_SPREADSHEET_ID") 
SOURCE_SHEET_NAME = "상품명_중복제거"          # 🔄 중복제거된 시트를 타겟으로 일치
CACHE_FILE_PATH = "product_cache.json"          

MAX_CONCURRENT_TASKS = 10 
semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
aclient = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

def calculate_hash(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()

# ==========================================
# 2. 데이터 로드 및 캐시 관리
# ==========================================
def load_google_sheet_data():
    """구글 스프레드시트에서 데이터를 안전하게 로드합니다."""
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
    if not all_values or len(all_values) <= 1:
        return sheet, [], all_values[0] if all_values else []
    
    headers = all_values[0]
    rows = all_values[1:]
    
    processed_rows = []
    for idx, row in enumerate(rows, start=2):
        # ⚠️ IndexError 방지: 행의 길이가 G열(인덱스 6)보다 짧다면 패딩 추가
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
            
    return sheet, processed_rows, headers

def load_cache() -> Dict[str, Dict]:
    if os.path.exists(CACHE_FILE_PATH):
        with open(CACHE_FILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_cache(cache_data: Dict[str, Dict]):
    with open(CACHE_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache_data, f, ensure_ascii=False, indent=4)

# ==========================================
# 3. 비동기 LLM 호출 및 중복 검증 루프
# ==========================================
async def call_llm_with_retry(target: Dict, confirmed_pool: Set[str]) -> str:
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
# 4. 메인 오케스트레이터 (전체 흐름 제어)
# ==========================================
async def main():
    print("🛒 1. 구글 스프레드시트 데이터 및 로컬 캐시 로드 중...")
    sheet, current_products, headers = load_google_sheet_data()
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
        is_row_shifted = not is_new and p["current_result"] != cleaned_cache[p_id]["recomposed_name"]
        
        if is_new or is_changed or is_missing_result or is_row_shifted:
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
    
    # 💡 최적화 매핑 딕셔너리 생성
    update_mapping = {target["row_num"]: final_name for target, final_name in zip(targets_to_process, llm_results)}
    
    # 캐시 데이터 최적화 업데이트
    for target, final_name in zip(targets_to_process, llm_results):
        new_cache[target["id"]] = {
            "origin_name": target["name"],
            "hash": calculate_hash(target["name"]),
            "recomposed_name": final_name
        }

    # 💾 [수정] 대용량 안전 벌크 업로드 구현
    print("💾 구글 스프레드시트 G열에 대용량 벌크 데이터 적재를 준비 중...")
    
    # 헤더 행 확장 (G열 이름이 정의되어 있지 않다면 'recomposed_title' 등으로 지정)
    if len(headers) < 7:
        sheet.update_cells([gspread.cell.Cell(row=1, col=7, value="recomposed_title")])
    
    # 전체 시트의 G열 구조를 안전하게 빌드
    max_row_num = max(p["row_num"] for p in current_products)
    g_col_output = []
    
    # G2부터 최하단 행까지의 데이터를 채워 넣음
    for r in range(2, max_row_num + 1):
        if r in update_mapping:
            g_col_output.append([update_mapping[r]])
        else:
            # 타겟이 아니면 현재 시트에 들어있는 값을 그대로 유지
            matching_product = next((p for p in current_products if p["row_num"] == r), None)
            val = matching_product["current_result"] if matching_product else ""
            g_col_output.append([val])
            
    # ⚡ 1만 행 단위로 끊어서 G열에 고속 덮어쓰기 (할당량 제약 극복)
    chunk_size = 10000
    total_output_len = len(g_col_output)
    print(f"⚡ 총 {total_output_len:,}행의 G열 데이터를 {chunk_size:,}행씩 나누어 업데이트합니다.")
    
    for i in range(0, total_output_len, chunk_size):
        chunk = g_col_output[i:i + chunk_size]
        start_row = i + 2  # G2 행부터 시작하므로
        range_string = f"G{start_row}"
        
        sheet.update(range_name=range_string, values=chunk)
        print(f"  └ [G열 적재 완료] {start_row:,} ~ {start_row + len(chunk) - 1:,} 행 완료")

    save_cache(new_cache)
    print("📝 로컬 캐시 파일(product_cache.json) 갱신 완료!")

if __name__ == "__main__":
    asyncio.run(main())
