import os
import json
import asyncio
import datetime
import gspread
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

# 1. GitHub Secrets 환경 변수로부터 구글 인증 정보 및 시트 연결
def get_gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_dict = json.loads(os.environ["GOOGLE_JSON_RAW"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

# 2. HTML 소스에서 하나투어 상품 정보 추출 함수
def parse_product_info(html_content, url):
    soup = BeautifulSoup(html_content, 'html.parser')
    
    try:
        # id (상품코드) 추출
        id_element = soup.select_one(".prod_code strong")
        prod_id = id_element.get_text(strip=True) if id_element else "N/A"
        
        # title (상품명) 추출
        title_element = soup.select_one(".item_title")
        title = title_element.get_text(strip=True) if title_element else "N/A"
        
        # price (가격) 추출 (원화 텍스트가 없는 순수 금액 추출)
        price_elements = soup.select(".price")
        price = "N/A"
        for elem in price_elements:
            txt = elem.get_text(strip=True)
            if "원" not in txt and txt:
                price = txt
                break
                
        # image_link 추출 (활성화된 스와이퍼 슬라이드의 이미지)
        img_element = soup.select_one(".swiper-slide.swiper-slide-active img")
        image_link = img_element["src"] if img_element and img_element.has_attr("src") else "N/A"
        
        return [prod_id, title, price, image_link]
        
    except Exception as e:
        print(f"[-] 파싱 오류 ({url}): {e}")
        return ["Error", "Error", "Error", "Error"]

# 3. 메인 실행 프로세스
async def main():
    spreadsheet_id = os.environ["SOURCE_SPREADSHEET_ID"]
        
    gc = get_gspread_client()
    spreadsheet = gc.open_by_key(spreadsheet_id)
    sheet = spreadsheet.worksheet("수동상품리스트")
    
    # A열의 모든 URL을 가져옵니다. (A1은 헤더 'url'이므로 2번째 행부터 가져옴)
    urls_to_crawl = sheet.col_values(1)[1:]
    print(f"[*] 총 {len(urls_to_crawl)}개의 URL을 탐색합니다.")
    
    update_payload = []
    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        for idx, url in enumerate(urls_to_crawl, start=2): # 행 번호는 2부터 시작 (A2)
            print(f"[*] [{idx}행] 크롤링 중: {url}")
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
                await page.wait_for_timeout(2000) # 동적 컨텐츠 렌더링 대기
                
                html_content = await page.content()
                product_data = parse_product_info(html_content, url)
                
                # 수집 시간(last_updated) 추가하여 페이로드 구성
                product_data.append(current_time)
                update_payload.append(product_data)
                
            except Exception as e:
                print(f"[-] {idx}행 접근 실패: {e}")
                update_payload.append(["Fail", "Fail", "Fail", "Fail", current_time])
        
        await browser.close()
        
    # 4. 구글 스프레드시트에 한 번에 업데이트 (B2 셀부터 범위 지정)
    end_row = 1 + len(update_payload)
    target_range = f"B2:F{end_row}"
    sheet.update(range_name=target_range, values=update_payload)
    print(f"[+] 스프레드시트 업데이트 완료! (범위: {target_range})")

if __name__ == "__main__":
    asyncio.run(main())
