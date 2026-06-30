import os
import json
import hashlib
import asyncio
import re  # 💡 정규식 처리를 위해 추가
from typing import List, Dict, Set

import gspread
from google.oauth2.service_account import Credentials
from openai import AsyncOpenAI

# ==========================================
# 1. GitHub Secrets 기반 설정 및 초기화
# ==========================================
SPREADSHEET_KEY = os.environ.get("SOURCE_SPREADSHEET_ID") 
SOURCE_SHEET_NAME = "raw"                        # 🎯 정확하게 raw 시트만 타겟팅
CACHE_FILE_PATH = "product_cache.json"          

MAX_CONCURRENT_TASKS = 10 
semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
aclient = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

def calculate_hash(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()

def clean_origin_name(text: str) -> str:
    """💡 LLM에 보내기 전, 상품 코드 및 불필요한 데이터 오염을 전처리하는 함수"""
    if not text:
        return ""
    
    # 1. 영문+숫자가 혼합된 마스터 상품코드 패턴 제거 (예: _CAB312260730KEA, ATP304260730CIA 등)
    #    언더바(_)로 시작하거나 시작하지 않는 8자 이상의 영숫자 조합 제거
    text = re.sub(re.compile(r'_?[A-Za-z0-9]{8,}'), '', text)
    
    # 2. URL 링크나 카테고리 번호가 상품명 뒤에 엉겨 붙어 들어오는 경우 차단
    text = re.sub(re.compile(r'https?://\S+'), '', text) # URL 제거
    text = re.sub(re.compile(r'\b\d{8,}\b'), '', text)    # 8자리 이상 순수 숫자 제거
    
    # 3. 연속된 공백 하나로 정리
    text = re.sub(r'\s+', ' ', text).strip()
    return text

# ==========================================
# 2. 데이터 로드 및 캐시 관리
# ==========================================
def load_google_sheet_data():
    """raw 시트에서 수식으로 불러와진 A~F열 및 기존 G열 데이터를 안전하게 읽어옵니다."""
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
    for idx, row in enumerate(rows, start=2): # 2행부터 데이터 시작
        # G열(인덱스 6)까지 안전하게 읽기 위한 패딩 처리
        while len(row) < 7:
            row.append("")
            
        p_id = str(row[0]).strip()   # A열: 상품 고유 ID (cd)
        p_name = str(row[1]).strip() # B열: LET 함수로 가공되어 출력된 원본 상품명 (title)
        current_result = str(row[6]).strip() # G열: 기존에 적재되어 있던 LLM 결과물
        
        # ID와 상품명이 모두 존재하는 유효한 행만 처리 대상으로 솎아냄
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
        # 💡 전처리 함수를 거쳐 맑은 데이터만 추출
        origin_name = clean_origin_name(target["name"])
        
        # 만약 전처리 후 이름이 너무 짧거나 비어버렸다면 원본 백업본 활용
        if not origin_name or len(origin_name) < 3:
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
                                "- 원본: 대련 3일 시내중심4성급호텔 핫플레이스동방수성 트램탑승 연화산전망대 야시장 특식2회\n"
                                "- 추천 결과: 대련 시내 중심 4성급 호텔 투어 동방수성 트램 탑승 연화산 전망대 야시장 여행\n\n"
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
        
        if is_new or is_changed or is_missing_result or is_row_shifted:
            targets_to_process.append(p)
            
    print(f"📊 분석 결과: 현재 raw 시트 수식 데이터 {len(current_products):,}개 중")
    print(f"   - 기존 유지 상품: {len(current_products) - len(targets_to_process):,}개")
    print(f"   - 신규/변경 처리 대상: {len(targets_to_process):,}개")
    
    if not targets_to_process:
        print("✅ 새로 처리할 상품이 없습니다. 작업을 종료합니다.")
        save_cache(new_cache)
        return

    print(f"🚀 {len(targets_to_process)}개 상품에 대해 비동기 병렬 LLM 상품명 재구성 시작...")
    tasks = [call_llm_with_retry(target, confirmed_pool) for target in targets_to_process]
    llm_results = await asyncio.gather(*tasks)
    
    # 💡 고유 ID(상품코드)를 key로 매핑 딕셔너리 생성하여 수식의 유동성 방어
    id_update_mapping = {target["id"]: final_name for target, final_name in zip(targets_to_process, llm_results)}
    
    # 캐시 데이터 갱신
    for target, final_name in zip(targets_to_process, llm_results):
        new_cache[target["id"]] = {
            "origin_name": target["name"],
            "hash": calculate_hash(target["name"]),
            "recomposed_name": final_name
        }

    # 💾 G열 안전 적재 시작 (A~F열 수식은 절대 건드리지 않음)
    print("💾 raw 시트 G열 영역에 순수 벌크 데이터 적재를 준비 중...")
    
    max_row_num = max(p["row_num"] for p in current_products)
    g_col_output = []
    
    # G2부터 최하단 행까지 순치적으로 매핑 결과 리스트 빌드
    for r in range(2, max_row_num + 1):
        matching_product = next((p for p in current_products if p["row_num"] == r), None)
        
        if matching_product:
            p_id = matching_product["id"]
            if p_id in id_update_mapping:
                g_col_output.append([id_update_mapping[p_id]])
            else:
                g_col_output.append([matching_product["current_result"]])
        else:
            g_col_output.append([""]) # 데이터가 유실되거나 비어있는 행 대응
            
    # ⚡ 1만 행 단위 청크로 끊어서 오직 G열에만 고속 쓰기 실행
    chunk_size = 10000
    total_output_len = len(g_col_output)
    print(f"⚡ 총 {total_output_len:,}행의 G열 상품명 리스트를 {chunk_size:,}행씩 분할 업로드합니다.")
    
    for i in range(0, total_output_len, chunk_size):
        chunk = g_col_output[i:i + chunk_size]
        start_row = i + 2  # G2 행부터 인덱스 매핑 시작
        range_string = f"G{start_row}"
        
        # ⚠️ 중요: A:F열 수식을 침범하지 않기 위해 "G{start_row}"로 명시적 분할 적재
        sheet.update(range_name=range_string, values=chunk)
        print(f"   └ [G열 단독 업로드] {start_row:,} ~ {start_row + len(chunk) - 1:,} 행 완료")

    save_cache(new_cache)
    print("📝 로컬 캐시 파일(product_cache.json) 동기화 완료!")

if __name__ == "__main__":
    asyncio.run(main())
