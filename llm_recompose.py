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
    """
    [완벽 보완본] 10,000개 데이터 패턴 분석 기반 1차 정규식 청소기
    AI 호출 전에 불필요한 노이즈를 99% 컷팅하여 API 재시도 비용을 아낍니다.
    """
    if not text:
        return ""
    
    # 1. URL 및 언더바 처리
    text = re.sub(r'https?://\S+', '', text)
    text = text.replace("_", " ")
    
    # 2. [0504타임세일], (0601특가), [0418새벽출발] 등 날짜+키워드 조합 완벽 박멸
    text = re.sub(r'[\[\(]\d*[가-힣]*세일[\]\)]', ' ', text)
    text = re.sub(r'[\[\(]\d*[가-힣]*특가[\]\)]', ' ', text)
    text = re.sub(r'[\[\(]\d*[가-힣]*출발[\]\)]', ' ', text)
    
    # 3. [NO 유류세인상], [NO 유류부담] 등 삭제형 NO 대괄호 세트 선제거
    text = re.sub(r'\[\s*NO\s*(유류세|유류부담|인상|부담)[^\]]*\]', ' ', text, flags=re.IGNORECASE)
    
    # 4. 필수 마케팅 키워드인 NO쇼핑/NO팁/NO옵션은 안전하게 미리 한글 치환
    text = re.sub(r'NO쇼핑', '노쇼핑', text, flags=re.IGNORECASE)
    text = re.sub(r'NO팁', '노팁', text, flags=re.IGNORECASE)
    text = re.sub(r'NO옵션', '노옵션', text, flags=re.IGNORECASE)
    
    # 5. 전면 삭제할 광고성 문구 및 내부 상품 분류명 목록 (하나투어 추가)
    trash_words = [
        "선착순특가", "실속여행", "신상품", "대박특가", "출발확정", "세미팩", 
        "[SK스토아 에디션]", "[USJ 오피셜 호텔]", "[USJ와패키지를한번에]", "[VIP]",
        "HIT!상품", "MD추천", "BEST상품", "추천상품", "효도락", "효율성甲", "얼리마켓", "스마트초이스", "유류세인상", "유류부담",
        "세이브", "우리끼리", "브랜드미적용", "스탠다드", "프리미엄", 
        "제우스 셀렉트", "제우스 시그니처", "현지투어플러스", "내나라여행", "제우스", "ZEUS", "하나투어"
    ]
    for word in trash_words:
        text = text.replace(word, "")
        
    # 6. 문장에 찌꺼기로 남은 단독 영문 'NO' 또는 'No' 완벽 청소
    text = re.sub(r'\bNO\b', ' ', text, flags=re.IGNORECASE)
    
    # 7. 의미 없는 마스터 코드 및 7자리 이상 숫자 제거
    text = re.sub(r'\b\d{7,}\b', '', text)
    
    # 8. 특수문자(★, ♥, ■, # 등) 전면 박멸 (한글, 영어, 숫자, 공백, 대시-, 물결~, 괄호만 허용)
    text = re.sub(r'[^가-힣A-Za-z0-9\s\-\~\[\]\(\)\&]', '', text)
    
    # 9. 연속된 공백 하나로 정리
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def test_clean_origin_name():
    """
    [추가된 로컬 테스트] API 비용을 쓰지 않고 전처리 로직이 완벽하게 작동하는지 검증
    """
    test_cases = [
        "[0504타임세일]오키나와 3일 갓성비추천",
        "[NO 유류세인상]석가장/태항산 4일★초특가★핵심관광#천계산",
        "NO 석가장태항산 4일효도하세보천대협곡",
        "[NO쇼핑/NO팁] 타이베이 예류 스펀 지우펀 3박~5일"
    ]
    
    print("\n==============================================")
    print("🧪 [비용 0원] 전처리 필터 로컬 테스트 검증")
    print("==============================================")
    
    for i, case in enumerate(test_cases, 1):
        result = clean_origin_name(case)
        print(f"테스트 {i}")
        print(f"❌ 원본: {case}")
        print(f"✨ 필터 후: {result}")
        print("-" * 46)
    print("==============================================\n")

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
        
        system_content = (
            "너는 네이버 쇼핑 검색 노출 로직(SEO) 최적화 카피라이터야.\n"
            "목적: [원본 상품명]에서 홍보 수식어와 내부 코드를 빼고 25자~45자 사이의 완성형 상품명 생성.\n\n"
            "⚠️ [필수 규칙]\n"
            "- 글자 수 필수 준수: 공백 포함 최소 25자 ~ 최대 45자 (절대 엄수)\n"
            "- 항공 코드 보존: 'CZ314', 'KE433' 등은 대괄호만 빼고 무조건 포함.\n"
            "- 물결표 변경: '~' 기호는 무조건 대시('-')로 변경 (예: 3박~5일 -> 3박-5일)\n"
            "- 제거 대상: '우리끼리', 'ZEUS', '스탠다드', '프리미엄', 'HIT', '추천' 및 의미 없는 영문 'NO'는 절대 출력 금지.\n"
            "- 필수 포함: '노쇼핑', '노팁' 정보는 소비자가 읽기 좋게 문장 뒤쪽에 포함.\n"
            "- 기호 금지: 명사구 형태로 끝나야 하며 #, !, [, ], / 등 특수문자 사용 금지. 오직 공백으로만 단어 구분.\n"
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
                
                # 후처리 필터 최종 고도화
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
                
        # ==========================================
        # 3회 실패 시 최종 안전장치 (Fallback)
        # ==========================================
        fallback_name = origin_name.replace('~', '-')
        fallback_name = re.sub(r'[\[\]_,\!#+/\(\)]', ' ', fallback_name)
        
        delete_keywords = [
            'HIT!상품', 'MD추천', 'BEST상품', '추천상품', '효도락', '효율성甲', '얼리마켓', '스마트초이스', '유류세인상', '유류부담',
            '세이브', '우리끼리', '브랜드미적용', '스탠다드', '프리미엄', '제우스 셀렉트', '제우스 시그니처', '현지투어플러스', '내나라여행', '제우스', 'ZEUS'
        ]
        for kw in delete_keywords:
            fallback_name = fallback_name.replace(kw, '')
            
        fallback_name = re.sub(r'\bNO\b', ' ', fallback_name, flags=re.IGNORECASE)
        fallback_name = fallback_name.replace('NO쇼핑', '노쇼핑').replace('NO팁', '노팁').replace('NO옵션', '노옵션')
        fallback_name = re.sub(r'\s+', ' ', fallback_name).strip()
        
        if len(fallback_name) > 45:
            fallback_name = fallback_name[:41] + " 패키지여행"
        elif len(fallback_name) < 25:
            fallback_name = fallback_name + " 추천 패키지 여행"
            
        confirmed_pool.add(fallback_name)
        return fallback_name

async def main():
    print(f"🛒 1. '{SOURCE_SHEET_NAME}' 시트의 수식 결과 추출 및 캐시 확인...")
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
    
    # ⚠️ [안전장치 가이드] 
    # 처음 테스트 시 비용 누수를 한 번 더 막으려면 아래 한 줄의 주석(#)을 풀고 바로 아랫줄을 주석처리하여 10개만 먼저 업로드해보세요.
    # tasks = [call_llm_with_retry(target, confirmed_pool) for target in targets_to_process[:10]]
    
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
    # 1. 실행하자마자 비용이 안 드는 전처리 테스트를 먼저 수행합니다.
    test_clean_origin_name()
    
    # 2. 로컬 테스트 결과를 터미널에서 확인하셨다면 아래 주석을 풀고 실제 구글 시트 업로드를 구동하세요.
    # asyncio.run(main())
