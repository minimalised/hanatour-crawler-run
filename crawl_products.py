import os
import json
import time
import re
import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

BASE_URL = "https://run.hanatour.com/package/major-products?rprsProdCds="

def fetch_and_sync():
    # 1. 구글 스프레드시트 인증 및 연결
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    service_account_info = json.loads(os.environ["GOOGLE_JSON_RAW"])
    creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    client = gspread.authorize(creds)

    target_id = os.environ["TARGET_SPREADSHEET_ID"]
    spreadsheet = client.open_by_key(target_id)
    
    # 2. '대표상품리스트' 시트에서 전체 데이터 읽어오기
    source_sheet = spreadsheet.worksheet("대표상품리스트")
    all_rows = source_sheet.get_all_values()
    
    if len(all_rows) <= 1:
        print("No data found in '대표상품리스트'.")
        return

    # 헤더 제외 데이터 행 추출 (A열: 대표상품코드 / L열: 대표상품구분명)
    target_tasks = []
    for row in all_rows[1:]:
        if len(row) >= 12:
            prod_code = row[0].strip()      # A열 (0번 인덱스)
            category_name = row[11].strip() # L열 (11번 인덱스)
            
            # L열(대표상품구분명)이 '패키지'인 경우만 크롤링 대상으로 선정
            if prod_code and category_name == "패키지":
                target_tasks.append(prod_code)

    print(f"Total target package items to crawl: {len(target_tasks)}")

    # 적재할 결과 데이터 구조 (헤더 구성)
    output_data = [
        ["대표상품코드", "판매상품명", "판매가", "출발일시", "도착일시", "예약상태", "소스URL"]
    ]

    # 3. Playwright 브라우저 실행 및 크롤링
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        for idx, code in enumerate(target_tasks, 1):
            target_url = f"{BASE_URL}{code}"
            print(f"[{idx}/{len(target_tasks)}] Crawling Code: {code} -> {target_url}")

            try:
                page.goto(target_url, wait_until="networkidle", timeout=30000)
                time.sleep(1) # 동적 렌더링 대기

                # 하단 판매상품 리스트 영역(div.prod_list_wrap) 내부의 <li> 요소들만 정교하게 지정
                sales_item_elements = page.query_selector_all("div.prod_list_wrap ul.prod_list_ul > li")

                if not sales_item_elements:
                    print(f" - No sales products found for code: {code}")
                    continue

                for item in sales_item_elements:
                    # 1) 판매상품명 추출 (strong.item_title)
                    title_elem = item.query_selector("strong.item_title")
                    title = title_elem.inner_text().strip() if title_elem else ""

                    # 2) 판매가 추출 (strong.price) -> 숫자만 정제
                    price_elem = item.query_selector("strong.price")
                    raw_price = price_elem.inner_text().strip() if price_elem else ""
                    price = re.sub(r"[^\d]", "", raw_price) # 원, 쉼표 등 제거 후 숫자만 추출

                    # 3) 일시 정보 (출발/도착시간)
                    air_time_elems = item.query_selector_all("span.air_time em")
                    dept_time = air_time_elems[0].inner_text().strip() if len(air_time_elems) > 0 else ""
                    arrv_time = air_time_elems[1].inner_text().strip() if len(air_time_elems) > 1 else ""

                    # 4) 예약상태 (예약가능, 출발확정 등)
                    status_elem = item.query_selector("span.state")
                    status = status_elem.inner_text().strip() if status_elem else ""

                    if title and price:
                        output_data.append([code, title, price, dept_time, arrv_time, status, target_url])

            except Exception as e:
                print(f"Error crawling code {code}: {e}")
                continue

        browser.close()

    # 4. '대표상품raw' 시트에 결과 데이터 쓰기
    sheet_name = "대표상품raw"
    print(f"Writing extracted sales items to '{sheet_name}' sheet...")
    
    try:
        target_sheet = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        target_sheet = spreadsheet.add_worksheet(title=sheet_name, rows="3000", cols="10")

    target_sheet.clear()
    target_sheet.update(range_name="A1", values=output_data)
    print(f"Successfully inserted {len(output_data) - 1} sales products into '{sheet_name}'!")

if __name__ == "__main__":
    fetch_and_sync()
