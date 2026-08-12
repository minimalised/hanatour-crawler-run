import os
import json
import io
import openpyxl
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

def sync_sheets():
    # 1. 인증 정보 구성 (Drive 및 Sheets 읽기/쓰기 권한)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly"
    ]
    service_account_info = json.loads(os.environ["GOOGLE_JSON_RAW"])
    creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    
    drive_service = build('drive', 'v3', credentials=creds)
    gspread_client = gspread.authorize(creds)

    print("[1/4] Searching for shared .xlsx files...")
    
    # 2. 공유 문서함을 포함하여 전체 드라이브에서 가장 최근 수정된 .xlsx 파일 조회
    query = "name contains '.xlsx' and trashed = false"
    
    results = drive_service.files().list(
        q=query,
        corpora='allDrives',
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        orderBy="modifiedTime desc",
        pageSize=10,
        fields="files(id, name, modifiedTime)"
    ).execute()
    
    files = results.get('files', [])

    if not files:
        raise FileNotFoundError("구글 드라이브 권한 또는 .xlsx 파일 탐색에 실패했습니다.")

    # 가장 최근에 수정된 엑셀 파일 선택
    latest_file = files[0]
    print(f"[2/4] Latest File Detected: '{latest_file['name']}' (ID: {latest_file['id']}, Modified: {latest_file['modifiedTime']})")

    # 3. 파일 메모리 다운로드
    request = drive_service.files().get_media(fileId=latest_file['id'])
    file_stream = io.BytesIO()
    downloader = MediaIoBaseDownload(file_stream, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    
    file_stream.seek(0)

    # 4. openpyxl로 '대표상품리스트' 시트 데이터 읽기
    workbook = openpyxl.load_workbook(file_stream, data_only=True)
    sheet_name = "대표상품리스트"
    
    if sheet_name not in workbook.sheetnames:
        raise ValueError(f"엑셀 파일 내 '{sheet_name}' 시트가 존재하지 않습니다. (존재하는 시트: {workbook.sheetnames})")
    
    excel_sheet = workbook[sheet_name]
    data = []
    for row in excel_sheet.iter_rows(values_only=True):
        if any(cell is not None for cell in row):
            data.append([str(cell) if cell is not None else "" for cell in row])

    print(f"[3/4] Successfully extracted {len(data)} rows.")

    # 5. 타겟 스프레드시트에 쓰기
    target_id = os.environ["TARGET_SPREADSHEET_ID"]
    print("[4/4] Writing data to target Google Sheet ('판매상품리스트')...")
    target_sheet = gspread_client.open_by_key(target_id).worksheet("판매상품리스트")
    
    target_sheet.clear()
    target_sheet.update("A1", data)
    print("Done! Sync completed successfully.")

if __name__ == "__main__":
    sync_sheets()
