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
    1차 전처리: 완벽한 스팸성 수식어 및 네이버 금지 기호(#) 제거
    단, 항공 코드는 보존하기 위해 알파벳 전면 제거 정규식은 적용하지 않음
    """
    if not text:
        return ""
    
    text = re.sub(r'https?://\S+', '', text)
    text = text.replace("_", " ")
    
    # [사용자 피드백 반영] 전면 삭제할 광고성 문구 및 내부 상품 분류명 목록
    trash_words = [
        "선착순특가", "실속여행", "신상품", "대박특가", "출발확정", "세미팩", 
        "[SK스토아 에디션]", "[USJ 오피셜 호텔]", "[USJ와패키지를한번에]", "[VIP]",
        "HIT!상품", "MD추천", "BEST상품", "추천상품", "효도락", "효율성甲", "얼리마켓", "스마트초이스", "유류세인상", "유류부담",
        "세이브", "우리끼리", "브랜드미적용", "스탠다드", "프리미엄", "제우스 셀렉트", "제우스 시그니처", "현지투어플러스", "내나라여행", "제우스", "ZEUS"
    ]
    for word in trash_words:
        text = text.replace(word, "")
        
    text = re.sub(r'\b\d{7,}\b', '', text)
    # 물결표(~), 대시(-), 괄호[], () 및 한글/영어/숫자/공백만 허용하고 샵(#), 쉼표(,) 등은 전면 제거
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
        
        # [구조화된 시스템 프롬프트 반영]
        system_content = (
            "너는 네이버 쇼핑 검색 노출 로직(SEO) 최적화 및 커머스 마케팅 피드 전문 카피라이터야.\n"
            "너의 유일한 목적은 제공된 [원본 상품명]에서 불필요한 홍보성 수식어와 내부 상품 분류 코드를 제거하고, 핵심 정보(항공 코드, 노쇼핑 여부 등)를 살려 네이버 쇼핑 노출 스코어가 가장 높은 25자 이상 45자 이하의 매력적인 완성형 상품명으로 재조합하는 것이다.\n\n"
            
            "⚠️ [네이버 쇼핑 SEO 핵심 가이드라인]\n"
            "1. 🎯 엄격한 글자 수 제한 (공백 포함 25자 ~ 45자):\n"
            "   - 최종 추천 결과물의 길이는 반드시 최소 25자에서 최대 45자 사이여야 한다. (★45자 절대 초과 금지, 25자 미만 절대 금지★)\n"
            "   - 대략 5개~8개의 단어(어절) 조합으로 구성하면 이 길이에 부합한다.\n"
            "2. 🧠 정보 중심의 유기적 재조합 (키워드 단순 나열 금지):\n"
            "   - 단어를 무작정 이어 붙이지 말고, 소비자가 읽기 쉽고 로직이 선호하는 부드러운 문장 구조로 재조합해라.\n"
            "3. 🛑 항목별 필터링 규칙 (필수 준수):\n"
            "   - [항공편 코드 유지]: 'CZ314', 'KE433' 같은 항공 코드 및 숫자는 상품 식별에 필수적이므로, 대괄호만 제거하고 상품명 앞이나 중간에 텍스트 형태로 '반드시 보존'하라.\n"
            "   - [NO쇼핑 / NO팁 필수 반영]: 이는 소비자의 상품 선택에 중요한 정보이므로 제거하지 말고, 문장 속에 자연스럽게 포함시켜라. (예: '~ 노쇼핑 노팁 패키지')\n"
            "   - [출발지 정보 보존]: '[부산출발]', '[청주출발]' 등은 필수 정보이므로 괄호를 제거하고 텍스트로 살려라.\n"
            "   - [광고성 수식어 및 내부 상품 분류 전면 삭제]:\n"
            "     * 'HIT!', 'MD추천', 'BEST상품', '추천상품', '효도락', '효율성甲', '얼리마켓', '스마트초이스' 등 홍보성 문구 삭제.\n"
            "     * '세이브', '우리끼리', '브랜드미적용', '스탠다드', '프리미엄', '제우스 셀렉트', '제우스 시그니처', '현지투어플러스', '내나라여행', '제우스', 'ZEUS' 등 기업 내부 상품 등급 및 분류명은 무조건 전면 삭제.\n"
            "     * 단, '골프', '레포츠', '트레킹', '허니문', '크루즈'는 상품의 핵심 테마이므로 문맥상 필요한 경우 텍스트로 보존하라.\n\n"
            
            "⚠️ [올바른 변환 예시 (Few-Shot)]\n"
            "- 입력: [CZ314/313]상해/소주/주가각 4일 NO쇼핑 상해3박 스타벅스리저브\n"
            "- 출력: CZ314 상해 소주 주가각 4일 노쇼핑 패키지 여행\n"
            "- 입력: [ZEUS] 제주 3일 JW메리어트 럭셔리호캉스\n"
            "- 출력: 제주 3일 JW메리어트 럭셔리 호캉스 패키지 여행\n"
            "- 입력: [우리끼리] 장가계 4일 칠성산 투어 패키지\n"
            "- 출력: 장가계 4일 칠성산 유리다리 삼림공원 패키지 여행\n"
            "- 입력: [NO쇼핑] 타이베이 예류 스펀 지우펀 3박~5일 시내호텔숙박\n"
            "- 출력: 타이베이 예류 스펀 지우펀 3박-5일 노쇼핑 시내호텔 패키지\n\n"
            
            "⚠️ [출력 제한 사양]\n"
            "- 원본 상품명에 물결표(~)가 있을 경우, 삭제하지 말고 반드시 대시(-)로 변경하여 출력할 것. (예: 3박~5일 ➡️ 3박-5일)\n"
            "- 쉼표(,), 느낌표(!), 샵(#), 플러스(+), 언더바(_), 슬래시(/), 대괄호([]) 등의 기호는 최종본에 절대 사용 금지. 단어 구분은 오직 공백으로만 하라.\n"
            "- 부가적인 설명 없이 오직 가공 완료된 상품명 '딱 한 줄'만 출력할 것."
        )

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": f"원본 상품명: {origin_name}"}
        ]
        
        while retry_count < 3:
            try:
                # 실패 횟수가 누적될수록 엄격도를 높이기 위해 온도를 낮춤
                current_temp = 0.3 if retry_count == 0 else 0.1
                
                response = await aclient.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=messages,
                    max_tokens=80,
                    temperature=current_temp
                )
                
                suggested_name = response.choices[0].message.content.strip()
                
                # [후처리 필터 고도화 및 부호 규칙 반영]
                suggested_name = suggested_name.replace('~', '-')  # 물결표 치환
                suggested_name = re.sub(r'[\[\]_,\!#+/\(\)]', ' ', suggested_name)  # 샵(#)을 포함한 금지 특수문자 제거
                suggested_name = re.sub(r'\s+', ' ', suggested_name).strip()
                
                # 중복 및 글자수 조건 체크
                if suggested_name not in confirmed_pool and 25 <= len(suggested_name) <= 45:
                    confirmed_pool.add(suggested_name)
                    return suggested_name
                
                # 조건 실패 시 이전 대화 내역에 피드백을 누적하는 대화형 구조로 전환
                retry_count += 1
                messages.append({"role": "assistant", "content": suggested_name})
                messages.append({
                    "role": "user", 
                    "content": f"⚠️ 실패 피드백: 방금 준 결과물은 {len(suggested_name)}자이거나 규칙(물결표를 대시로 변경, 샵(#) 및 내부 분류명 완벽 제거 등)을 위반했습니다. 네이버 쇼핑 SEO 규격(공백 포함 25~45자)을 엄격히 준수하여 최종 상품명 딱 한 줄만 다시 출력하세요."
                })
                
            except Exception as e:
                print(f"❌ API 에러 발생 ({origin_name}): {e}")
                await asyncio.sleep(1)
                retry_count += 1
                
        # ==========================================
        # 3회 실패 시 최종 안전장치 (Fallback 로직 고도화)
        # ==========================================
        fallback_name = origin_name.replace('~', '-')
        fallback_name = re.sub(r'[\[\]_,\!#+/\(\)]', ' ', fallback_name)
        
        # 수식어 및 내부 분류명 강제 제거
        delete_keywords = [
            'HIT!상품', 'MD추천', 'BEST상품', '추천상품', '효도락', '효율성甲', '얼리마켓', '스마트초이스', '유류세인상', '유류부담',
            '세이브', '우리끼리', '브랜드미적용', '스탠다드', '프리미엄', '제우스 셀렉트', '제우스 시그니처', '현지투어플러스', '내나라여행', '제우스', 'ZEUS'
        ]
        for kw in delete_keywords:
            fallback_name = fallback_name.replace(kw, '')
            
        # NO시리즈 한글화 변환 및 공백 정리
        fallback_name = fallback_name.replace('NO쇼핑', '노쇼핑').replace('NO팁', '노팁').replace('NO옵션', '노옵션')
        fallback_name = re.sub(r'\s+', ' ', fallback_name).strip()
        
        if len(fallback_name) > 45:
            fallback_name = fallback_name[:41] + " 패키지여행"
        elif len(fallback_name) < 25:
            fallback_name = fallback_name + " 추천 패키지 여행"
            
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
    
    targets_to_process = []
    new_cache = cleaned_cache.copy()
    
    for p in current_products:
        p_id = p["id"]
        current_hash = calculate_hash(p["name"])
        
        # [데이터 초기 청소 대응을 위한 검사 조건 완화]
        # 캐시와 시트를 싹 지운 상태라면 모든 유효한 데이터가 타겟에 잡히도록 유도
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
