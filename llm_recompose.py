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
# [데이터 전처리 함수 - 5, 6, 7번 완전 박멸 및 1,2,3,4번 보존]
# ==========================================
def extract_meta_and_clean(title: str):
    """
    마케터님과 확정한 룰에 따라 상품 고유 자산(호텔, 항공, CC)은 철저히 보존하고,
    5, 6, 7번(타겟, 사은품, 광고 스팸어) 및 URL, 기계적 코드는 완벽히 도려내는 전처리 엔진
    """
    if not title:
        return "", "", ""
        
    # URL 및 뭉개진 링크 문자열 완전 제거
    title_clean = re.sub(r'https?://\S+', ' ', title)
    
    # 1. [3번 자산] 출발공항 패턴 한글명으로 선제 추출 ([대구출발] 등)
    airport_match = re.search(r'(청주|대구|부산|인천|무안|양양|제주)\s*출발', title_clean)
    departure = f"[{airport_match.group(0).strip()}]" if airport_match else ""

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
# [사후 검증 가드 함수]
# ==========================================
def validate_naver_title(title: str) -> bool:
    """최종 생성된 타이틀의 비정상 마무리를 검사하는 품질 하드 가드"""
    if not title:
        return False
        
    # 문장 끝이 '까', '제', '포', '거' 등 한 글자 조사나 단어로 조기 짤림 종결되는 버그 완벽 검출
    if re.search(r'\s[가-힣]$', title) or title.endswith(("까", "포", "제", "거", "포함", "제공", "특전")):
        return False
        
    # 최종 결과물에 섞이지 말아야 할 스팸어 2중 체크
    forbidden = ["추천", "베스트", "대박", "특가", "명문", "골프장맵", "이용권", "2색상품", "쏙쏙", "매력", "두도시"]
    if any(word in title for word in forbidden):
        return False
    return True

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
    
    # 🧪 [테스트 가드] 데이터가 무한 증식하는 것을 막기 위해 상위 딱 100개 행(2행~101행) 영역만 물리적으로 고정 호출
    # A열(ID), B열(원본상품명), G열(최종결과)을 포함하기 위해 G101까지 범위를 하드 코딩하여 가져옵니다.
    cell_range = sheet.get("A2:G101")
    
    processed_rows = []
    for idx, row in enumerate(cell_range, start=2):
        # 열의 길이가 부족할 경우 G열까지 빈 문자열 패딩
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
# [LLM 호출 및 비동기 엔진 - 차별화 자산 보존형]
# ==========================================
async def call_llm_with_retry(target: Dict, confirmed_pool: Set[str]) -> str:
    async with semaphore:
        departure, duration, cleaned_title = extract_meta_and_clean(target["name"])
        
        # 원본에서 마케팅 핵심 옵션(노쇼핑/노팁/노옵션)만 정확하게 격리 분리
        options = ""
        if "NO쇼핑" in target["name"] or "노쇼핑" in target["name"]: options += "노쇼핑 "
        if "NO팁" in target["name"] or "노팁" in target["name"]: options += "노팁 "
        if "NO옵션" in target["name"] or "노옵션" in target["name"]: options += "노옵션 "
        options = options.strip()

        # [차별화 보존 프롬프트] 획일화된 도배성 제목을 양산하지 않도록 지시
        prompt = f"""
당신은 네이버 쇼핑 최상위 노출 규격을 준수하는 여행 전문 퍼포먼스 마케팅 카피라이터입니다.
지역과 일정이 같더라도 상품들끼리 제목이 똑같이 복제되어 출력되는 로봇 같은 행위를 엄격히 금지합니다.
[원본 핵심어]에 살아있는 **해당 상품만의 고유한 자산(예: 쉐라톤, 크라운플라자, 풀만, 홀리데이인, 아난티, 대한항공, 1일자유 등 고유 식별 명사)**을 절대로 생략하지 말고 최종 명사구에 반드시 포함하여 다른 상품들과 확실하게 차별화하십시오.

[입력 정형 데이터]
- 지정 출발지: "{departure}"
- 여행 일정: "{duration}"
- 원본 핵심어: "{cleaned_title}"
- 필수 옵션 문구: "{options}"

[🧱 1단계: 명사구 조합 어순 공식]
최종 상품명은 조사나 설명조의 문장이 아니어야 하며, 반드시 아래의 띄어쓰기 조합 공식을 물리적으로 따라야 합니다.
- 구조 공식: {{지정 출발지}} + {{주요 여행 지역/도시명}} + {{여행 일정}} + {{★해당 상품 고유의 자산(호텔명/항공사/핵심특전 명사)★}} + {{필수 옵션 문구}} + 패키지여행 (또는 자유여행/골프/크루즈 상품 성격에 맞는 종결어)

[📋 2단계: 완벽한 차별화 생성을 위한 퓨샷 예시 (Few-Shot)]
지역과 일정이 완벽히 겹치더라도 고유 식별 자산에 따라 제목이 서로 완벽하게 분리되어야 네이버 SEO 어뷰징에 걸리지 않습니다.

■ 예시 1 (호텔명에 따른 명확한 상품 간 식별성 확보)
- 원본 핵심어: "옌타이 연태 3일 월드체인 쉐라톤 전객실 오션뷰 금사탄 해변인근"
- 출력 결과: {{"refined_title": "옌타이 연태 3일 쉐라톤 전객실 오션뷰 패키지여행"}}
- 원본 핵심어: "옌타이 연태 3일 월드체인 크라운플라자 시내중심 대학가 인근 자유여행 최적"
- 출력 결과: {{"refined_title": "옌타이 연태 3일 크라운플라자 시내중심 자유여행"}}

■ 예시 2 (항공사 및 일정 자산 분리 보존)
- 원본 핵심어: "오키나와 4일 대한항공 전일정나하숙박 1일자유 츄라우미수족관"
- 출력 결과: {{"refined_title": "오키나와 4일 대한항공 전일정 나하숙박 1일자유 패키지여행"}}

[⚠️ 3단계: 핵심 제약 가이드라인]
1. 🚫 **단어 절대 절단 금지**: 글자 수 제한에 구애받지 마십시오. 문장 끝이 '까', '제', '포', '거' 같이 한 글자만 남고 뚝 끊기게 만드는 행위는 절대 금지합니다. 문장이 길어져도 상관없으니 단어를 완벽한 형태로 종결하십시오.
2. 🚫 **기계적 복사 금지**: 모든 오키나와 상품, 모든 제남 골프 상품을 복사 붙여넣기 한 것처럼 똑같은 제목으로 세팅하지 마십시오. 개별 고유 자산을 뼈대에 넣어 다채롭게 구성하십시오.
3. 🚫 **알파벳 및 한자 출력 금지**: 영문이나 한자는 단 한 글자도 결과물에 섞이지 않게 하십시오. 범위 기호는 오직 대시(-)만 허용합니다.
"""

        json_schema_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "naver_seo_unique_flexible_schema",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "refined_title": {
                            "type": "string",
                            "description": "상품 간 고유한 차별화 식별 자산이 온전히 보존되고 단어 잘림이 없는 최종 정제 상품명"
                        }
                    },
                    "required": ["refined_title"],
                    "additionalProperties": False
                }
            }
        }

        retry_count = 0
        while retry_count < 3:
            try:
                response = await aclient.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant that outputs compliant JSON based on the provided schema."},
                        {"role": "user", "content": prompt}
                    ],
                    response_format=json_schema_format,
                    temperature=0.3 # 고유 자산 매핑 자율성과 다채로운 변별성을 확보하기 위해 온도 미세 조율
                )
                
                res_json = json.loads(response.choices[0].message.content)
                suggested_name = res_json.get("refined_title", "").strip()
                
                # 특수문자 사후 공백 처리 및 출발지 결합 보정
                suggested_name = re.sub(r'[\[\]_,\!#+/\(\)A-Za-z]', ' ', suggested_name)
                if departure and not suggested_name.startswith(departure):
                    clean_opt = suggested_name.replace(departure.replace("[","").replace("]",""), "")
                    clean_opt = re.sub(r'^[ \t\s\-]+', '', clean_opt).strip()
                    suggested_name = f"{departure} {clean_opt}"
                
                suggested_name = re.sub(r'\s+', ' ', suggested_name).strip()
                
                # 사후 가드 통과 시 풀에 등록 후 즉시 반환
                if suggested_name not in confirmed_pool and validate_naver_title(suggested_name):
                    confirmed_pool.add(suggested_name)
                    return suggested_name
                
                retry_count += 1
            except Exception:
                await asyncio.sleep(0.3)
                retry_count += 1
                
        # 3회 실패 시 발동할 하드웨어 Fallback 보정 엔진 (고유 자산 최대한 유지 보존)
        fallback_name = cleaned_title
        if departure: fallback_name = f"{departure} {fallback_name}"
        if options: fallback_name = f"{fallback_name} {options}"
        
        # 종결어 누락 방지 안전선
        if not any(fallback_name.endswith(word) for word in ["패키지여행", "자유여행", "크루즈", "골프", "여행"]):
            fallback_name = f"{fallback_name} 패키지여행"
            
        final_fallback = re.sub(r'\s+', ' ', fallback_name).strip()
        confirmed_pool.add(final_fallback)
        return final_fallback

# ==========================================
# [메인 실행 엔진]
# ==========================================
async def main():
    print(f"🛒 1. 상위 100개 행 타겟 고정식 무한 테스트 엔진 가동...")
    sheet, current_products = load_google_sheet_data_fixed_100()
    
    if not current_products:
        print("ℹ️ 처리할 상위 100개 상품 데이터가 시트에 존재하지 않습니다.")
        return

    # 테스트 목적상, 상위 100개 고정 영역 내에 있는 모든 상품을 무조건 새로 정제하도록 타겟팅 배정
    targets_to_process = current_products
    
    confirmed_pool = set()
    print(f"📊 물리 테스트 타겟팅: 정확히 시트 상위 {len(targets_to_process)}개 행을 정밀 정제합니다.")

    print(f"🚀 차별화 자산 매핑 비동기 병렬 API 요청 중...")
    tasks = [call_llm_with_retry(target, confirmed_pool) for target in targets_to_process]
    llm_results = await asyncio.gather(*tasks)
    
    id_update_mapping = {target["id"]: final_name for target, final_name in zip(targets_to_process, llm_results)}
    
    print("💾 시트 G열 상위 100칸 영역 정밀 동기화 적재 중...")
    g_col_output = []
    
    # 2행부터 101행까지만 정확하게 출력값 리스트를 빌드
    for target in current_products:
        p_id = target["id"]
        if p_id in id_update_mapping:
            g_col_output.append([id_update_mapping[p_id]])
        else:
            g_col_output.append([target["current_result"]])
            
    # 정확히 2행부터 꽂히도록 범위 스트링 고정 가드
    range_string = f"G2:G{2 + len(g_col_output) - 1}"
    sheet.update(range_string, g_col_output)
    print(f"   └ [테스트 가드 적재 완수] 시트 {range_string} 영역 동기화 완료.")
    print("✅ 프로세스 완수.")

if __name__ == "__main__":
    asyncio.run(main())
