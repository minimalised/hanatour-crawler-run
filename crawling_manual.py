import os
import json
import asyncio
import gspread
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

def get_gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_dict = json.loads(os.environ["GOOGLE_JSON_RAW"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

def parse_product_info(html_content, url):
    soup = BeautifulSoup(html_content, 'html.parser')
    try:
        id_element = soup.select_one(".prod_code strong")
        prod_id = id_element.get_text(strip=True) if id_element else "N/A"
        
        title_element = soup.select_one(".item_title")
        title = title_element.get_text(strip=True) if title_element else "N/A"
        
        price_elements = soup.select(".price")
        price = "N/A"
        for elem in price_elements:
            txt = elem.get_text(strip=True)
            if "원" not in txt and txt:
                # [수정] 천단위 쉼표(,)를 완전히 제거하여 숫자 형식으로 변경
                price = txt.replace(",", "")
                break
                
        img_element = soup.select_one(".swiper-slide.swiper-slide-active img")
        image_link = img_element["src"] if img_element and img_element.has_attr("src") else "N/A"
        
        return [prod_id, title, price, url, image_link]
    except Exception as e:
        return ["Error", "Error", "Error", url, "Error"]

async def main():
    spreadsheet_id = os.environ["SOURCE_SPREADSHEET_ID"]
    gc = get_gspread_client()
    spreadsheet = gc.open_by_key(spreadsheet_id)
    
    source_sheet = spreadsheet.worksheet("수동상품리스트")
    target_sheet = spreadsheet.worksheet("수동raw")
    
    urls_to_crawl = source_sheet.col_values(1)[1:]
    print(f"[*] 총 {len(urls_to_crawl)}개의 URL 탐색 (상단 타깃팅 + 쉼표 제거 모드)")
    
    update_payload = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            locale="ko-KR"
        )
        page = await context.new_page()
        
        await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "stylesheet", "font", "media"] or "analytics" in route.request.url else route.continue_())
        
        for idx, url in enumerate(urls_to_crawl, start=2):
            if not url or not url.startswith("http"):
                update_payload.append(["N/A", "N/A", "N/A", url if url else "", "N/A"])
                continue
                
            print(f"[*] [{idx}행] 상품 정보 스캔 중: {url}")
            try:
                await page.goto(url, wait_until="commit", timeout=15000)
                
                try:
                    await page.wait_for_selector(".prod_code strong", timeout=6000)
                except:
                    pass
                
                html_content = await page.content()
                product_data = parse_product_info(html_content, url)
                update_payload.append(product_data)
                
                await asyncio.sleep(0.5)
                
            except Exception as e:
                print(f"[-] {idx}행 실패 패스: {e}")
                update_payload.append(["Fail", "Fail", "Fail", url, "Fail"])
        
        await browser.close()
        
    if update_payload:
        print("[*] 기존 '수동raw' 시트의 영역을 정리하고 새로 동기화합니다.")
        target_sheet.batch_clear(["G2:K1000"])
        
        end_row = 1 + len(update_payload)
        target_range = f"G2:K{end_row}"
        target_sheet.update(range_name=target_range, values=update_payload)
        print(f"[+] 쉼표 제거 완료 및 '수동raw' 시트 1:1 적재 성공!")

if __name__ == "__main__":
    asyncio.run(main())
