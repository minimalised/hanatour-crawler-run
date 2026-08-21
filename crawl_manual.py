import os
import json
import asyncio
import re  # 정규식 라이브러리 추가
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
        
        # 가격 추출 로직 고도화
        price_elements = soup.select(".price")
        price = "N/A"
        for elem in price_elements:
            txt = elem.get_text(strip=True)
            digits = re.sub(r'[^0-9]', '', txt)
            if digits:  
                price = int(digits)  
                break
                
        # bg_alpha만 아니면 예외 없이 무조건 첫 배너 이미지 추출
        image_link = "N/A"
        img_elements = soup.select(".swiper-slide img")
        for img in img_elements:
            src = img.get("src", "").strip()
            if src and "bg_alpha" not in src:
                image_link = src
                break
                
        return [prod_id, title, price, url, image_link]
    except Exception as e:
        return ["Error", "Error", "Error", url, "Error"]

async def main():
    spreadsheet_id = os.environ["SOURCE_SPREADSHEET_ID"]
    gc = get_gspread_client()
    spreadsheet = gc.open_by_key(spreadsheet_id)
    
    source_sheet = spreadsheet.worksheet("수동상품리스트")
    target_sheet = spreadsheet.worksheet("수동raw")
    
    all_rows = source_sheet.get_all_values()[1:]  # 헤더 제외
    
    print(f"[*] 총 {len(all_rows)}개의 데이터 행 탐색 (종합 무결점 모드)")
    
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
        
        for idx, row in enumerate(all_rows, start=2):
            url = row[0].strip() if len(row) > 0 else ""
            custom_title = row[1].strip() if len(row) > 1 else ""
            
            if not url or not url.startswith("http"):
                final_title = custom_title if custom_title else "N/A"
                update_payload.append(["N/A", final_title, "N/A", url if url else "", "N/A"])
                continue
                
            print(f"[*] [{idx}행] 상품 데이터 추출 중: {url}")
            try:
                await page.goto(url, wait_until="commit", timeout=15000)
                
                try:
                    await page.wait_for_selector(".prod_code strong", timeout=6000)
                except:
                    pass
                
                html_content = await page.content()
                product_data = parse_product_info(html_content, url)
                
                if custom_title:
                    product_data[1] = custom_title  # 수동 상품명 반영
                
                update_payload.append(product_data)
                await asyncio.sleep(0.5)
                
            except Exception as e:
                print(f"[-] {idx}행 실패 패스: {e}")
                fail_title = custom_title if custom_title else "Fail"
                update_payload.append(["Fail", fail_title, "Fail", url, "Fail"])
        
        await browser.close()
        
    if update_payload:
        # [수정] G열~K열 대신 H열~L열로 변경
        target_sheet.batch_clear(["H2:L1000"])
        end_row = 1 + len(update_payload)
        target_range = f"H2:L{end_row}"
        
        target_sheet.update(
            range_name=target_range, 
            values=update_payload, 
            value_input_option="USER_ENTERED"
        )
        print(f"[+] [동기화 최종 완료] 모든 상품 정보가 무결점으로 업데이트되었습니다.")

if __name__ == "__main__":
    async_playwright_used = True
    asyncio.run(main())
