import os
import json
import asyncio
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
        
        # price (가격) 추출
        price_elements = soup.select(".price")
        price = "N/A"
        for elem in price_elements:
            txt = elem.get_text(strip=True)
            if "원" not in txt and txt:
                price = txt
                break
                
        # image_link 추출
        img_element = soup.select_one(".swiper-slide.swiper-slide-active img")
        image_link = img_element["src"] if img_element and img_element.has_attr("src") else "N/A"
        
        return [prod_id, title, price, url, image_link]
        
    except Exception as e:
        print(f"[-] 파싱 오류 ({url}): {e}")
        return ["Error", "Error", "Error", url, "Error"]

# 3. 메인 실행 프로세스
async def main():
    spreadsheet_id = os.environ["SOURCE_SPREADSHEET_ID"]
        
    gc = get_gspread_client()
    spreadsheet = gc.open_by_key(spreadsheet_id)
    
    source_sheet = spreadsheet.worksheet("수동상품리스트")
    target_sheet = spreadsheet.worksheet("수동raw")
    
    # 1:1 행 매칭을 위해 원본 URL 리스트를 순서대로 가져옴
    urls_to_crawl = source_sheet.col_values(1)[1:]
    print(f"[*] 총 {len(urls_to_crawl)}개의 URL을 탐색합니다. (안정성 최우선 모드)")
    
    update_payload = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        for idx, url in enumerate(urls_to_crawl, start=2):
            if not url or not url.startswith("http"):
                update_payload.append(["N/A", "N/A", "N/A", url if url else "", "N/A"])
                continue
                
            print(f"[*] [{idx}행] 완벽 크롤링 중: {url}")
            try:
                # [안정성 확보] networkidle 조건으로 데이터 레이아웃이 100% 로드될 때까지 대기
                await page.goto(url, wait_until="networkidle", timeout=40000)
                # 혹시 모를 비동기 지연을 위해 2초 확정 대기 후 파싱
                await page.wait_for_timeout(2000) 
                
                html_content = await page.content()
                product_data = parse_product_info(html_content, url)
                update_payload.append(product_data)
                
            except Exception as e:
                print(f"[-] {idx}행 로딩 실패 또는 타임아웃: {e}")
                update_payload.append(["Fail", "Fail", "Fail", url, "Fail"])
        
        await browser.close()
        
    # 4. '수동raw' 시트 초기화 후 원샷 통적재
    if update_payload:
        print("[*] 기존 '수동raw' 시트의 영역을 깨끗하게 정리합니다.")
        target_sheet.batch_clear(["G2:K1000"])
        
        end_row = 1 + len(update_payload)
        target_range = f"G2:K{end_row}"
        target_sheet.update(range_name=target_range, values=update_payload)
        print(f"[+] '수동raw' 시트 1:1 완벽 동기화 완료! (범위: {target_range})")

if __name__ == "__main__":
    asyncio.run(main())
