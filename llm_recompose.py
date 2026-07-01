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

MAX_CONCURRENT_TASKS = 10 
semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
aclient = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

# ==========================================
# [데이터 전처리 함수 - 출발지 버그 완벽 수정 버전]
# ==========================================
def extract_meta_and_clean(title: str):
    """
    원본에 지정 도시 이름([대구출발], 청주출발 등)이 명학하게 박혀있는 경우에만 출발지를 추출합니다.
    원본에 출발 도시가 없으면 departure를 무조건 빈 값("")으로 밀어내어 유령 출발지 생성을 차단합니다.
    """
    if not title:
        return "", "", ""
        
    # URL 및 뭉개진 링크 문자열 완전 제거
    title_clean = re.sub(r'https?://\S+', ' ', title)
    
    # 💡 [버그 완벽 수정] 공백 치환 전, "순수 원본 문자열(title)"에서 출발공항 패턴을 칼같이 추출
    airport_match = re.search(r'\[?(청주|대구|부산|인천|무안|양양|제주)\]?\s*출발', title)
    if airport_match:
        city = airport_match.group(1).strip()
        departure = f"[{city}출발]"
    else:
        departure = "" # 원본에 없으면 절대 지어내지 않고 빈 값 고정

    # 2. 일정(박/일) 정밀 추출 (ex: 4일, 3박5일)
    duration_match = re.search(r'\d+박\s*\d+일|\d+일|\d+박\d+일|\d+~\d+일|\d+-\d+일', title_clean)
    duration = duration_match.group(0).strip() if duration_match else ""
    duration = duration.replace('~', '-') # 물결 기호 대시로 치환

    # 3. 알파벳 항공 코드 및 5자리 이상의 의미 없는 시스템 카테고리 숫자 코드 박멸
    title_clean = re.sub(r'[A-Za-z]', ' ', title_clean)
    title_clean = re.sub(r'\b\d{5,}\b', ' ', title_clean)
    
    # 4. 특수문자, 대괄호 기호 전면 청소 (슬래시/ 와 대시- 는 지역 및 일정 구분을 위해 보존)
    title_clean = re.sub(r'[^가-힣0-9\s\-\/]', ' ', title_clean)

    # 5. [6, 7번 및 5번 타겟명 완전 삭제] 네이버 SEO를 저해하고 제목을 조잡하게 만드는 스팸 블랙리스트
    kill_words = [
        # 7번 광고 노이즈 단어
        "2색상품", "2색매력", "3색골프", "다색골프", "두도시한번에", "두도시", "한번에", "시티", 
        "골프장맵", "거리측정", "이용권", "추천", "쏙쏙", "핵심관광쏙쏙", "대박", "특가", "명문", "지역",
        "인기선택관광포함", "1인2만원제공", "무료업그레이드", "올어바웃", "도파민팡팡", "스마트초이스", "최저가도전", "여름맞이특가",
        # 6번 사은품 및 현지 식사/체험 단어
        "스테이크정식", "딤섬", "훠궈", "비파원훠궈", " Beer LAO 제공", "서핑체험", "얀바루캐녀닝", 
        "보트체험", "캠프파이어", "카발란위스키DIY", "네일아트", "스파", "발마사지", "우유비누", "맥주박물관", "식사포함", "음료교환권",
        # 5번 애매한 타겟/컨셉 세그먼트 단어 (지시대로 우선 제외 조치)
        "2030전용", "2030 전용", "4050 XTeen", "4050", "깐부여행", "대디앤미", "아빠와자녀한정", "1인1실", "3인 가족 전용", "인솔자동행", "세미팩"
    ]
    for kw in kill_words:
        title_clean = title_clean.replace(kw, " ")

    title_clean = re.sub(r'\s+', ' ', title_clean).strip()
    return departure, duration, title_clean

# ==========================================
# [구글 시트 연동 로직 - 상위 100개 고정형]
# ==========================================
def load_google_sheet_data_fixed_100():
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
    
    cell_range = sheet.get("A2:G101")
    
    processed_rows = []
    for idx, row in enumerate(cell_range, start=2):
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
        departure, duration, cleaned_title = extract_meta_and_clean(target["name"])
        
        options = ""
        if "NO쇼핑" in target["name"] or "노쇼핑" in target["name"]: options += "노쇼핑 "
        if "NO팁" in target["name"] or "노팁" in target["name"]: options += "노팁 "
        if "NO옵션" in target["name"] or "노옵션" in target["name"]: options += "노옵션 "
        options = options.strip()

        # 💡 GPT가 퓨샷 예시를 보고 '대구출발'을 오인 복사하지 않도록 예시 변수화 처리
        prompt = f"""
당신은 네이버 쇼핑 입점 및 검색 최적화(SEO) 지침을 완벽하게 숙지한 글로벌 커머스 상품명 정제 전문가야.
지저분하고 노이즈가 많은 원본 여행 상품명을 분석하여, 네이버 쇼핑 검색 엔진에 즉시 노출 가능한 형태의 깨끗하고 매력적인 상품명으로 재구성해라.

반드시 아래 [⚠️ 4대 핵심 제약 가이드라인]을 한 치의 오차도 없이 완벽하게 준수해야 한다.

지정 출발지: "{departure}"
여행 일정: "{duration}"
원본 핵심어: "{cleaned_title}"
필수 옵션 문구: "{options}"

⚠️ [핵심 제약 가이드라인]
1. 글자 수 엄수 (공백 포함 32자 ~ 45자)
   - 최종 결과물의 총 글자 수는 공백을 포함하여 반드시 '32자 이상', '45자 이하'여야 한다.
   - ★45자를 단 한 자라도 초과하거나 32자 미만으로 출력하는 것을 절대 금지한다.★

2. 문장부호 및 특수문자 전면 제거
   - 대괄호([, ]), 소괄호((, )), 슬래시(/), 쉼표(,), 물결(~), 플러스(+), 언더바(_) 등 모든 기호와 문장부호를 100% 제거해라.
   - 오직 '한글', '숫자', '영문', '띄어쓰기'만 사용해서 상품명을 구성해야 한다.

3. 시스템 및 광고성 노이즈 전면 도려내기
   - '[출발확정]', '[한정특가]', '[스마트초이스]' 같은 대괄호 문구를 무조건 삭제해라.
   - '실속여행', '대박특가', '최저가보장', '인기No.1', '베스트셀러' 등 유치한 광고성 홍보 단어는 흔적도 없이 지워라.

4. 정보 보완 및 명사구 조합 구조 공식
   - 뼈대 구조: {{지정 출발지}} + {{주요 여행 지역/도시명}} + {{여행 일정}} + {{★해당 상품 고유의 자산(호텔명/항공사/핵심특전 명사)★}} + {{필수 옵션 문구}} + 패키지여행 (또는 자유여행/골프 상품 성격에 맞는 종결어)
   - 맨 앞에는 입력된 [지정 출발지] 데이터만 넣어야 하며, 없는 경우 절대로 지어내어 넣지 말고 생략해라.
   - 주의사항, '추천 결과:' 같은 불필요한 시스템 지시어를 앞뒤에 삽입하는 것을 절대 금지한다.

💡 [작업 참고 예시]
- 지정 출발지: "[청주출발]"
- 원본 핵심어: "옌타이 연태 3일 월드체인 쉐라톤 전객실 오션뷰"
- 출력 결과: [청주출발] 옌타이 연태 3일 쉐라톤 전객실 오션뷰 패키지여행

- 지정 출발지: ""
- 원본 핵심어: "오키나와 4일 대한항공 전일정나하숙박 1일자유"
- 출력 결과: 오키나와 4일 대한항공 전일정 나하숙박 1일자유 패키지여행
"""

        try:
            response = await aclient.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "user", "content": prompt}
                ],
                max_tokens=80,
                temperature=0.3
            )
            
            suggested_name = response.choices[0].message.content.strip()
            
            # 특수문자 사후 공백 처리 안전장치
            suggested_name = re.sub(r'[\[\]_,\!#+/\(\)A-Za-z]', ' ', suggested_name)
            
            # 💡 [버그 수정] departure가 원본에 '실제로 존재할 때만' 파이썬 강제 결합 사후 가드 발동
            if departure:
                if not suggested_name.startswith(departure):
                    clean_opt = suggested_name.replace(departure.replace("[","").replace("]",""), "")
                    clean_opt = re.sub(r'^[ \t\s\-]+', '', clean_opt).strip()
                    suggested_name = f"{departure} {clean_opt}"
            
            suggested_name = re.sub(r'\s+', ' ', suggested_name).strip()
            return suggested_name
            
        except Exception:
            fallback_name = cleaned_title
            if departure: fallback_name = f"{departure} {fallback_name}"
            if options: fallback_name = f"{fallback_name} {options}"
            if not any(fallback_name.endswith(word) for word in ["패키지여행", "자유여행", "크루즈", "골프", "여행"]):
                fallback_name = f"{fallback_name} 패키지여행"
            return re.sub(r'\s+', ' ', fallback_name).strip()

# ==========================================
# [메인 실행 엔진]
# ==========================================
async def main():
    print(f"🛒 1. 상위 100개 행 타겟 고정식 무한 테스트 엔진 가동...")
    sheet, current_products = load_google_sheet_data_fixed_100()
    
    if not current_products:
        print("ℹ️ 처리할 상위 100개 상품 데이터가 시트에 존재하지 않습니다.")
        return

    targets_to_process = current_products
    confirmed_pool = set()
    
    print(f"📊 물리 테스트 타겟팅: 정확히 시트 상위 {len(targets_to_process)}개 행을 정밀 정제합니다.")

    print(f"🚀 차별화 자산 매핑 비동기 병렬 API 요청 중 (과금 잠금 제어)...")
    tasks = [call_llm_with_retry(target, confirmed_pool) for target in targets_to_process]
    llm_results = await asyncio.gather(*tasks)
    
    id_update_mapping = {target["id"]: final_name for target, final_name in zip(targets_to_process, llm_results)}
    
    print("💾 시트 G열 상위 100칸 영역 정밀 동기화 적재 중...")
    g_col_output = []
    
    for target in current_products:
        p_id = target["id"]
        if p_id in id_update_mapping:
            g_col_output.append([id_update_mapping[p_id]])
        else:
            g_col_output.append([target["current_result"]])
            
    range_string = f"G2:G{2 + len(g_col_output) - 1}"
    sheet.update(range_string, g_col_output)
    print(f"   └ [테스트 가드 적재 완수] 시트 {range_string} 영역 동기화 완료.")
    print("✅ 프로세스 완수.")

if __name__ == "__main__":
    asyncio.run(main())
