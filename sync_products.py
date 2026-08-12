import os
import json
import gspread
from google.oauth2.service_account import Credentials

def sync_sheets():
    # 1. 인증 정보 설정 (Repository Secret에서 받아온 JSON 사용)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    service_account_info = json.loads(os.environ["GOOGLE_JSON_RAW"])
    creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    client = gspread.authorize(creds)

    # 2. 원본 및 타겟 시트 ID 환경변수 로드
    source_id = os.environ["SOURCE_SPREADSHEET_ID"]
    target_id = os.environ["TARGET_SPREADSHEET_ID"]

    # 3. 원본 데이터 읽기 (대표상품리스트 시트)
    print("Fetching data from source spreadsheet...")
    source_sheet = client.open_by_key(source_id).worksheet("대표상품리스트")
    data = source_sheet.get_all_values()

    if not data:
        print("No data found in source sheet.")
        return

    # 4. 타겟 시트에 데이터 쓰기 (판매상품리스트 시트)
    print("Writing data to target spreadsheet...")
    target_sheet = client.open_by_key(target_id).worksheet("판매상품리스트")
    
    # 기존 데이터를 비우고 최신 데이터로 갱신
    target_sheet.clear()
    target_sheet.update("A1", data)
    print(f"Successfully synced {len(data)} rows.")

if __name__ == "__main__":
    sync_sheets()
