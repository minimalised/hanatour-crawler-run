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

# 동시 처리 태스크 수 (Rate Limit 방어)
MAX_CONCURRENT_TASKS = 10 
semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
aclient = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

# ==========================================
# [데이터 전처리 함수 - 골프 CC 보호 및 출발지 버그 차단]
# ==========================================
def extract_meta_and_clean(title: str):
    """
    골프 자산(성문안CC 등)을 완벽하게 보호하고 출발 도시가 없는 경우 빈 값("")을 리턴합니다.
    """
    if not title:
        return "", "", ""
        
    # URL 및 링크 제거
    title_clean = re.sub(r'https?://\S+', ' ', title)
    
    # 1. 출발공항 패턴을 '순수 원본 문자열(title)'에서 엄격하게 정량 추출
    airport_match = re.search(r'\[?(청주|대구|부산|인천|무안|양양|제주)\]?\s*출발', title)
    if airport_match:
        city = airport_match.group(1).strip()
        departure = f"[{city}출발]"
    else:
        departure = "" # 없으면 철저히 공백 고정

    # 2. 일정(박/일) 추출
    duration_match = re.search(r'\d+박\s*\d+일|\d+일|\d+박\d+일|\d+~\d+일|\d+-\d+일', title_clean)
    duration = duration_match.group(0).strip() if duration_match else ""
    duration = duration.replace('~', '-') 

    # 3. [골프 핵심 자산] 알파벳 대소문자 CC가 지워지지 않도록 선제적으로 대문자 치환 보호
    title_clean = re.sub(r'\bCC\b|\bcc\b|Cc|cC', 'CC', title_clean)
    
    # 4. 의미 없는 5자리 이상 숫자 코드 박멸
    title_clean = re.sub(r'\b\d{5,}\b', ' ', title_clean)
    
    # 5. 블랙리스트 광고 단어 정밀 도려내기 (자산 가치를 훼손하는 수식어 전면 차단)
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

    # 6. 특수문자 청소 (한글, 숫자, 공백, 슬래시, 대시 및 보호된 골프장 영문 'CC'만 허용)
    title_clean = re.sub(r'[^가-힣0-9\s\-\/[A-Z]]', ' ', title_clean)
    
    # 7. CC를 제외한 나머지 낱개 영문 시스템 문자들만 청소
    title_clean = re.sub(r'\b(?![CC]\b)[A-Za-z]\b', ' ', title_clean)

    title_clean = re.sub(r'\s+', ' ', title_clean).strip()
    return departure, duration, title_clean

# ==========================================
# [구글 시트 연동 로직 - 가변형 전체 로드]
# ==========================================
def load_google_sheet_all_data():
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
    
    # 데이터가 있는 전체 영역 로드
    all_values = sheet.get_all_values()
    if len(all_values) <= 1:
        return sheet, []
        
    header = all_values[0]
    cell_range = all_values[1:] # 헤더 제외한 실제 데이터
    
    processed_rows = []
    for idx, row in enumerate(cell_range, start=2): # 시트 행 번호는 2부터 시작
        while len(row) < 7:
            row.append("")
        p_id = str(row[0]).strip()   
        p_name = str(row[1]).strip() 
        current_result = str(row[6]).strip() 
        
        if p_id and p_name: 
            processed_rows.append({
                "row_num": idx,
                "id": p_id,
                "name": p_name,
                "current_result": current_result
            })
    return sheet, processed_rows

# ==========================================
# 🛑 무한 과금 원천 차단형 비동기 LLM 엔지니어링 (1대1 단발 호출)
# ==========================================
async def call_llm_with_retry(target: Dict, confirmed_pool: Set[str]) -> str:
    async with semaphore:
        # 💡 실패 대비 원본 상품명 하드 복사 백업
        raw_original_name = target["name"]
        departure, duration, cleaned_title = extract_meta_and_clean(raw_original_name)
        
        options = ""
        if "NO쇼핑" in raw_original_name or "노쇼핑" in raw_original_name: options += "노쇼핑 "
        if "NO팁" in raw_original_name or "노팁" in raw_original_name: options += "노팁 "
        if "NO옵션" in raw_original_name or "노옵션" in raw_original_name: options += "노옵션 "
        options = options.strip()

        # 💡 라벨링 뇌절 전면 금지 지시 및 자산 보존 하드캐리 프롬프트
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
                messages=[
                    {"role": "user", "content": prompt}
                ],
                max_tokens=80,
                temperature=0.2
            )
            
            suggested_name = response.choices[0].message.content.strip()
            
            # 🛑 [사후 가드] 혹시라도 라벨이나 접두사를 붙여 나왔을 경우 완벽 도려내기
            suggested_name = re.sub(r'^(출력\s*결과|원복\s*핵심어|원본\s*핵심어|결과|refined_title)\s*:\s*', '', suggested_name, flags=re.IGNORECASE)
            suggested_name = suggested_name.replace('"', '').replace("'", "")
            
            # 특수문자 사후 공백 처리 안전장치
            suggested_name = re.sub(r'[\[\]_,\!#+/\(\)A-Za-z]', ' ', suggested_name)
            
            # 출발지 결합 예외처리 보정
            if departure:
                if not suggested_name.startswith(departure):
                    clean_opt = suggested_name.replace(departure.replace("[","").replace("]",""), "")
                    clean_opt = re.sub(r'^[ \t\s\-]+', '', clean_opt).strip()
                    suggested_name = f"{departure} {clean_opt}"
            
            suggested_name = re.sub(r'\s+', ' ', suggested_name).strip()
            
            # 💡 [최종 생존성 하드가드] 조합 문맥이 꼬이거나 글자 수 탈락 조짐 시 원본명 그대로 리턴
            if len(suggested_name) < 25 or len(suggested_name) > 50 or "결과" in suggested_name:
                return raw_original_name
                
            return suggested_name
            
        except Exception:
            # 에러 발생 시 완벽하게 안전한 원본 보호망 작동
            return raw_original_name

# ==========================================
# [메인 실행 엔진]
# ==========================================
async def main():
    print(f"🛒 1. 전체 상품 리스트 자동 탐색 엔진 가동...")
    sheet, current_products = load_google_sheet_all_data()
    
    if not current_products:
        print("ℹ️ 처리할 상품 데이터가 시트에 존재하지 않습니다.")
        return

    targets_to_process = current_products
    confirmed_pool = set()
    
    print(f"📊 총 {len(targets_to_process)}개의 상품 전체 행을 대상으로 대규모 병렬 정제를 시작합니다.")

    print(f"🚀 LLM 비동기 병렬 대량 요청 개시...")
    tasks = [call_llm_with_retry(target, confirmed_pool) for target in targets_to_process]
    llm_results = await asyncio.gather(*tasks)
    
    id_update_mapping = {target["id"]: final_name for target, final_name in zip(targets_to_process, llm_results)}
    
    print("💾 시트 G열 전체 영역 동기화 데이터 생성 중...")
    g_col_output = []
    for target in current_products:
        p_id = target["id"]
        if p_id in id_update_mapping:
            g_col_output.append([id_update_mapping[p_id]])
        else:
            g_col_output.append([target["current_result"]])
            
    # 💡 API 과부하 및 할당량 만료 방지를 위한 100개 단위 분할 분기 적재(Batch Update)
    batch_size = 100
    total_len = len(g_col_output)
    print(f"📦 구글 API 안정성을 위해 {batch_size}개 단위로 나누어 순차 적재를 진행합니다.")
    
    for i in range(0, total_len, batch_size):
        chunk = g_col_output[i:i + batch_size]
        start_row = 2 + i
        end_row = start_row + len(chunk) - 1
        range_string = f"G{start_row}:G{end_row}"
        
        sheet.update(range_string, chunk)
        print(f"   └ [적재 진행중] 시트 {range_string} 영역 동기화 완료 ({min(i + batch_size, total_len)}/{total_len})")
        await asyncio.sleep(1) # 구글 시트 API 쓰기 제한 우회 페이싱
        
    print("✅ 모든 상품 리스트 정제 및 안정성 가드 적재가 최종 완수되었습니다.")

if __name__ == "__main__":
    asyncio.run(main())
