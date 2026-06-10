import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from playwright.sync_api import sync_playwright
from openai import OpenAI

# 1. 환경 변수 세팅
GOOGLE_JSON_RAW = os.environ.get("GOOGLE_JSON_RAW")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
SOURCE_ID = os.environ.get("SOURCE_SPREADSHEET_ID")
TARGET_ID = os.environ.get("TARGET_SPREADSHEET_ID")

client = OpenAI(api_key=OPENAI_API_KEY)

def get_gspread_client(json_raw):
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(json_raw)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)

# 2. 하나투어 리스트 페이지 맞춤형 크롤러
def crawl_hanatour_products(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = context.new_page()
        
        products_data = []
        try:
            print(f"[Crawl] 페이지 접속 중: {url}")
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            
            # 하나투어 리스트의 동적 렌더링 대기
            page.wait_for_selector(".prod_list_wrap ul.type li", timeout=15000)
            
            items = page.locator(".prod_list_wrap ul.type li").all()
            print(f"[Crawl] 총 {len(items)}개의 상품을 발견했습니다.")
            
            # 상위 5개 상품 정보 추출
            for idx, item in enumerate(items[:5]):
                try:
                    title = item.locator(".item_title").inner_text().strip()
                    stit = item.locator(".item_text.stit").inner_text().strip() if item.locator(".item_text.stit").count() > 0 else ""
                    hash_tags = item.locator(".hash_group").inner_text().strip().replace("\n", " ") if item.locator(".hash_group").count() > 0 else ""
                    price = item.locator(".price").inner_text().strip() if item.locator(".price").count() > 0 else ""
                    
                    product_desc = f"[{idx+1}번 상품]\n- 상품명: {title}\n- 특징: {stit}\n- 태그: {hash_tags}\n- 가격: {price}\n"
                    products_data.append(product_desc)
                except Exception as e:
                    continue
                    
            browser.close()
            return "\n".join(products_data)
        except Exception as e:
            print(f"[Error] 크롤링 실패: {e}")
            browser.close()
            return None

# 3. 네이버 파워링크 카피 생성 함수
def generate_naver_copy(combined_text):
    if not combined_text:
        return None
        
    prompt = f"""
귀하는 대한민국 최고 수준의 퍼포먼스 마케팅 에이전시 소속 검색광고(SA) 전문가입니다.
제공된 '하나투어 여행 상품 목록 데이터'를 철저히 분석하여, 네이버 파워링크 검색광고 규격 및 아래 조건에 100% 일치하는 카피를 작성하십시오.

[분석할 상품 데이터]
{combined_text}

[작성 조건 - 필독 및 엄수]

1. 광고 제목 (개수: 정확히 15개)
   - 글자 수 제한: 공백 포함 무조건 '15자 이하'
   - 작성 스타일: 주요 지역을 중심으로 간단명료하게 작성하세요.
   - 예시 스타일: '인증샷맛집 방콕 5일 여행', '초특급호텔 방콕파타야 패키지', '노쇼핑 방콕 파타야 여행'처럼 핵심 지역+속성 기반으로 직관적이고 심플하게 구성합니다.

2. 설명 문구 (개수: 정확히 4개)
   - 글자 수 제한: 공백 포함 무조건 '30자 이상 ~ 40자 이하' (29자 이하 또는 41자 이상은 절대 안 됨)
   - 필수 포함 요소: 긁어온 데이터를 토대로 [지역, 핵심 명소, 여행 일정(예: 4박5일, 5일 등), 패키지 형태(예: 노쇼핑, 세이브, 프리미엄, 2030전용)]를 문장 속에 자연스럽게 모두 녹여내야 합니다.
   - 예시 스타일: '방콕 파타야 5일 세이브 패키지! 산호섬 투어와 디너크루즈 포함.' (공백 포함 39자)

[출력 포맷]
반드시 아래 형식을 지킨 순수한 JSON 데이터로만 응답하세요. 다른 부연 설명이나 마크다운 래퍼는 절대 금지합니다.
{{
  "titles": ["제목1", "제목2", ..., "제목15"],
  "descriptions": ["설명1", "설명2", "설명3", "설명4"]
}}
"""
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"[Error] OpenAI API 호출 실패: {e}")
        return None

# 4. 메인 파이프라인
def main():
    print("[System] 작업 시작...")
    gc = get_gspread_client(GOOGLE_JSON_RAW)
    
    # ─── 구글 시트 및 특정 워크시트 로드 ───
    source_wb = gc.open_by_key(SOURCE_ID)
    try:
        source_sheet = source_wb.worksheet("상품랜딩리스트")
    except gspread.exceptions.WorksheetNotFound:
        print("[Fatal Error] 소스 시트에 '상품랜딩리스트' 워크시트가 존재하지 않습니다.")
        return

    target_wb = gc.open_by_key(TARGET_ID)
    try:
        # 지정된 이름의 워크시트가 있는지 먼저 확인
        target_sheet = target_wb.worksheet("제목설명문구")
    except gspread.exceptions.WorksheetNotFound:
        # 시트가 없으면 새로 생성 (행: 1000, 열: 20 기본 세팅)
        print("[System] '제목설명문구' 시트가 없어 새로 생성합니다.")
        target_sheet = target_wb.add_worksheet(title="제목설명문구", rows="1000", cols="20")
    
    # 소스 시트의 1번째 열에서 헤더를 제외한 URL 리스트 가져오기
    urls = source_sheet.col_values(1)[1:] 
    
    # 타겟 시트 헤더 세팅 (비어있을 경우만)
    if not target_sheet.row_values(1):
        headers = ["랜딩 URL"] + [f"제목_{i}" for i in range(1, 16)] + [f"설명_{i}" for i in range(1, 5)]
        target_sheet.append_row(headers)

    for url in urls:
        if not url.startswith("http"):
            continue
            
        product_corpus = crawl_hanatour_products(url)
        if not product_corpus:
            print(f"[Skip] 데이터를 가져오지 못했습니다: {url}")
            continue
            
        copies = generate_naver_copy(product_corpus)
        if not copies:
            print(f"[Skip] 카피 생성 실패: {url}")
            continue
            
        # ─── 데이터 가드레일 (조건 검증) ───
        raw_titles = copies.get("titles", [])
        raw_descs = copies.get("descriptions", [])
        
        final_titles = []
        for t in raw_titles[:15]:
            final_titles.append(t[:15].strip())
            
        final_descs = []
        for d in raw_descs[:4]:
            d_clean = d.strip()
            if len(d_clean) > 40:
                d_clean = d_clean[:40]  # 40자 초과 시 안전하게 컷
            final_descs.append(d_clean)
        
        # 데이터 부족 시 채우기 보정
        while len(final_titles) < 15: final_titles.append("추천 여행 패키지")
        while len(final_descs) < 4: final_descs.append("하나투어 엄선 추천 여행 패키지 지금 확인해보세요.") # 기본 31자
        
        # 5. 구글 스프레드시트 적재
        row_data = [url] + final_titles + final_descs
        target_sheet.append_row(row_data)
        print(f"[Success] 시트 적재 완료 -> URL: {url}")

if __name__ == "__main__":
    main()
