import os
import json
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

MAX_CONCURRENT_TASKS = 10 
semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
aclient = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

# ==========================================
# [데이터 전처리 함수]
# ==========================================
def extract_meta_and_clean(title: str):
    if not title:
        return "", "", ""
        
    title_clean = re.sub(r'https?://\S+', ' ', title)
    
    airport_match = re.search(r'\[?(청주|대구|부산|인천|무안|양양|제주)\]?\s*출발', title)
    if airport_match:
        city = airport_match.group(1).strip()
        departure = f"[{city}출발]"
    else:
        departure = "" 

    duration_match = re.search(r'\d+박\s*\d+일|\d+일|\d+박\d+일|\d+~\d+일|\d+-\d+일', title_clean)
    duration = duration_match.group(0).strip() if duration_match else ""
    duration = duration.replace('~', '-') 

    title_clean = re.sub(r'\bCC\b|\bcc\b|Cc|cC', 'CC', title_clean)
    title_clean = re.sub(r'\b\d{5,}\b', ' ', title_clean)
    
    kill_words = [
        "2색상품", "2색매력", "3색골프", "다색골프", "두도시한번에", "두도시", "한번에", "시티", 
        "골프장맵", "거리측정", "이용권", "추천", "명문골프장", "원주명문골프장", "명문", "지역", "쏙쏙", "핵심관광쏙쏙",
        "인기선택관광포함", "1인2만원제공", "무료업그레이드", "올어바웃", "도파민팡팡", "스마트초이스", "최저가도전", "여름맞이특가",
        "스테이크정식", "딤섬", "훠궈", "비파원훠궈", " Beer LAO 제공", "서핑체험", "얀바루캐녀닝", 
        "보트체험", "캠프파이어", "카발란위스키DIY", "네일아트", "스파", "발마사지", "우유비누", "맥주박물관", "식사포함", "음료교환권",
        "2030전용", "2030 전용", "4050 XTeen", "4050", "깐부여행", "대디앤미", "아빠와자녀한정", "1인1실", "3인 가족 전용", "인솔자동행", "세미팩"
    ]
    for kw in kill_words:
        title_clean = title_clean.replace(kw, " ")

    title_clean = re.sub(r'[^가-힣0-9\s\-\/[A-Z]]', ' ', title_clean)
    title_clean = re.sub(r'\b(?![CC]\b)[A-Za-z]\b', ' ', title_clean)
    title_clean = re.sub(r'\s+', ' ', title_clean).strip()
    return departure, duration, title_clean

# ==========================================
# [구글 캐시 시트 연동 로직]
# ==========================================
def load_cache_data(doc):
    """'cache' 시트에서 기존에 정제했던 [날것의 원본명 : 결과명] 맵을 고속 로드합니다."""
    try:
        cache_sheet = doc.worksheet("cache")
    except gspread.exceptions.WorksheetNotFound:
        cache_sheet = doc.add_worksheet(title="cache", rows="10000", cols="2")
        cache_sheet.update(range_name="A1:B1", values=[["original_raw_name", "refined_name"]])
        return cache_sheet, {}

    all_values = cache_sheet.get_all_values()
    if len(all_values) <= 1:
        return cache_sheet, {}

    # 🎯 변경: 띄어쓰기 오차를 완전히 방지하기 위해, 양끝 공백을 깎아낸(.strip()) 날것의 원본명을 Key로 빌드합니다.
    cache_dict = {row[0].strip(): row[1].strip() for row in all_values[1:] if len(row) >= 2 and row[0]}
    return cache_sheet, cache_dict

def append_to_cache(cache_sheet, new_items: List[List[str]]):
    if not new_items:
        return
    cache_sheet.append_rows(new_items)

# ==========================================
# [비동기 LLM 엔지니어링 호출]
# ==========================================
async def call_llm_with_retry(target: Dict, confirmed_pool: Set[str]) -> str:
    async with semaphore:
        raw_original_name = target["name"]
        departure, duration, cleaned_title = extract_meta_and_clean(raw_original_name)
        
        options = ""
        if "NO쇼핑" in raw_original_name or "노쇼핑" in raw_original_name: options += "노쇼핑 "
        if "NO팁" in raw_original_name or "노팁" in raw_original_name: options += "노팁 "
        if "NO옵션" in raw_original_name or "노옵션" in raw_original_name: options += "노옵션 "
        options = options.strip()

        prompt = f"""
당신은 네이버 쇼핑 입점 및 검색 최적화(SEO) 지침을 완벽하게 숙지한 글로벌 커머스 상품명 정제 전문가입니다.
원본 핵심어의 불필요한 미사여구를 제거하되, 해당 상품 고유의 가치 자산인 명사(골프장명, 리조트/호텔명, 항공사)는 무조건 최종 상품명에 노출시켜 네이밍을 완성하십시오.

반드시 아래 [⚠️ 핵심 제약 가이드라인]을 단 한치도 어기지 말고 그대로 이행하십시오.

지정 출발지: "{departure}"
여행 일정: "{duration}"
원본 핵심어: "{cleaned_title}"
필수 옵션 문구: "{options}"

⚠️ [핵심 제약 가이드라인]
1. 출력 형식 절대 엄수:
   - '원본 핵심어:', '출력 결과:', '결과:', 'refined_title' 같은 지시어나 라벨링 텍스트를 절대로 포함하지 마라.
   - 주의사항, 앞말, 뒷말, 따옴표 등을 일체 배제하고 오직 네이버 규격에 맞춰 재구성된 순수 상품명 '딱 한 줄'만 단독 출력해라.

2. 고유 고유자산 삭제 절대 금지:
   - 입력된 데이터에 포함된 구체적인 골프장 이름(예: 성문안CC, 봉래군정CC 등), 리조트/호텔명, 특정 항공사명은 변별력을 확보하는 핵심 키워드이므로 절대로 누락하거나 변경하지 마라.

3. 글자 수 엄수 (공백 포함 32자 ~ 45자):
   - 최종 결과물의 총 글자 수는 공백을 포함하여 반드시 '32자 이상', '45자 이하'여야 한다. (45자 절대 초과 금지)
   - 만약 자산을 다 포함하고도 32자 미만이라면, 해당 지역의 유명 명소나 '라운딩', '라운드', '골프여행' 등의 키워드를 조합하여 32자 이상으로 채워라.

4. 문장부호 및 특수문자 전면 제거:
   - 모든 기호와 대괄호를 100% 제거하고 오직 '한글', '숫자', '영문', '띄어쓰기'만 사용해라.

💡 [작업 참고 예시]
원본: 강원 원주 골프 2일 36홀 원주 골프장 성문안CC
결과: 강원 원주 골프 2일 36홀 성문안CC 라운딩 골프여행
"""
        try:
            response = await aclient.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=80,
                temperature=0.2
            )
            suggested_name = response.choices[0].message.content.strip()
            suggested_name = re.sub(r'^(출력\s*결과|원복\s*핵심어|원본\s*핵심어|결과|refined_title)\s*:\s*', '', suggested_name, flags=re.IGNORECASE)
            suggested_name = suggested_name.replace('"', '').replace("'", "")
            suggested_name = re.sub(r'[\[\]_,\!#+/\(\)A-Za-z]', ' ', suggested_name)
            
            if departure and not suggested_name.startswith(departure):
                clean_opt = suggested_name.replace(departure.replace("[","").replace("]",""), "").strip()
                suggested_name = f"{departure} {clean_opt}"
            
            suggested_name = re.sub(r'\s+', ' ', suggested_name).strip()
            if len(suggested_name) < 25 or len(suggested_name) > 50 or "결과" in suggested_name:
                return raw_original_name
            return suggested_name
        except Exception:
            return raw_original_name

# ==========================================
# [메인 실행 엔진]
# ==========================================
async def main():
    print(f"🛒 1. 전체 상품 리스트 자동 탐색 엔진 가동...")
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
    if len(all_values) <= 1:
        print("ℹ️ 처리할 데이터가 존재하지 않습니다.")
        return
        
    # 캐시 시트 로드
    cache_sheet, cache_dict = load_cache_data(doc)
    
    current_products = []
    for idx, row in enumerate(all_values[1:], start=2):
        if len(row) < 2:
            continue
        p_id = str(row[0]).strip()
        p_name = str(row[1]).strip()
        current_result = str(row[6]).strip() if len(row) > 6 else ""
        
        if p_id and p_name: 
            current_products.append({
                "row_num": idx,
                "id": p_id,
                "name": p_name,
                "current_result": current_result
            })
            
    print(f"📊 실재하는 유효 상품 {len(current_products)}개를 대상으로 처리를 시작합니다.")

    # 🌟 변경: raw 시트의 날것 그대로의 상품명(.strip())을 캐시 딕셔너리와 바로 동기화 대조합니다.
    targets_to_llm = []
    id_update_mapping = {}
    
    for prod in current_products:
        raw_name_key = prod["name"].strip()
        if raw_name_key in cache_dict:
            id_update_mapping[prod["id"]] = cache_dict[raw_name_key] # 완전 일치 캐시 적용
        else:
            targets_to_llm.append(prod)

    print(f"🎯 캐시 히트: {len(current_products) - len(targets_to_llm)}개 완료 (비용 0원)")
    
    # 신규 상품이 있을 때만 LLM 호출
    if targets_to_llm:
        print(f"🚀 신규 상품 {len(targets_to_llm)}개에 대해서만 LLM 비동기 대량 요청 개시...")
        confirmed_pool = set()
        tasks = [call_llm_with_retry(target, confirmed_pool) for target in targets_to_llm]
        llm_results = await asyncio.gather(*tasks)
        
        new_cache_rows = []
        for target, final_name in zip(targets_to_llm, llm_results):
            id_update_mapping[target["id"]] = final_name
            
            # 🎯 뇌절 단어가 섞인 게 아니라면, 실패/성공 여부 상관없이 
            # 날것의 '원본명'을 그대로 캐시에 태워 무적 잠금을 적용합니다.
            if "결과" not in final_name:
                new_cache_rows.append([target["name"].strip(), final_name])
                
        # 신규 데이터 캐시 업데이트
        append_to_cache(cache_sheet, new_cache_rows)

    # G열에 데이터 안전하게 적재
    print("💾 시트 G열 전체 영역 동기화 데이터 생성 중...")
    g_col_output = []
    for target in current_products:
        p_id = target["id"]
        final_title = id_update_mapping.get(p_id, target["current_result"])
        g_col_output.append([final_title])
            
    total_valid_len = len(g_col_output)
    batch_size = 100
    print(f"📦 구글 API 안정성을 위해 {batch_size}개 단위로 나누어 순차 적재를 진행합니다.")
    
    for i in range(0, total_valid_len, batch_size):
        chunk = g_col_output[i:i + batch_size]
        start_row = 2 + i
        end_row = start_row + len(chunk) - 1
        range_string = f"G{start_row}:G{end_row}"
        
        sheet.update(range_name=range_string, values=chunk)
        print(f"   └ [적재 진행중] 시트 {range_string} 영역 동기화 완료 ({min(i + batch_size, total_valid_len)}/{total_valid_len})")
        await asyncio.sleep(1) 
        
    print("✅ 모든 상품 리스트 정제 및 행 밀림 방지 가드 적재가 최종 완수되었습니다.")

if __name__ == "__main__":
    asyncio.run(main())
