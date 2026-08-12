import os
import json
import io
import openpyxl
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

ROOT_FOLDER_ID = "1tTZ8KjqzxV-9t9Vje5jQma8gRmyb8Wl3"

def sync_sheets():
    # 1. 인증 정보 구성
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly"
    ]
    service_account_info = json.loads(os.environ["GOOGLE_JSON_RAW"])
    creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    
    drive_service = build('drive', 'v3', credentials=creds)
    gspread_client = gspread.authorize(creds)

    # 2. 최신 .xlsx 파일 탐색 (하위 연월 폴더 검색 포함)
    print("Searching for the latest .xlsx file across subfolders...")
    
    # 엑셀 파일 MimeType 및 휴지통 제외 조건
    query = (
        "mimeType = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' "
        "and trashed = false"
    )
    
    results = drive_service.files().list(
        q=query,
        orderBy="modifiedTime desc",  # 가장 최근에 수정/업로드된 파일이 최상단
        pageSize=10,
        fields="files(id, name, modifiedTime)"
    ).execute()
    
    files = results.get('files', [])

    if not files:
        raise FileNotFoundError("구글 드라이브에서 .xlsx 파일을 찾지 못했습니다.")

    # 가장 최근 수정된 엑셀 파일 선택
    latest_file = files[0]
    print(f"Latest File Found: {latest_file['name']} (Modified: {latest_file['modifiedTime']})")

    # 3. 최신 엑셀 파일 메모리로 다운로드
    request = drive_service.files().get_media(fileId=latest_file['id'])
    file_stream = io.BytesIO()
    downloader = MediaIoBaseDownload(file_stream, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    
    file_stream.seek(0)

    # 4. openpyxl로 '대표상품리스트' 시트 읽기
    workbook = openpyxl.load_workbook(file_stream, data_only=True)
    if "대표상품리스트" not in workbook.sheetnames:
        raise ValueError("엑셀 내 '대표상품리스트' 시트가 존재하지 않습니다.")
    
    excel_sheet = workbook["대표상품리스트"]
    data = []
    for row in excel_sheet.iter_rows(values_only=True):
        if any(cell is not None for cell in row):
            data.append([str(cell) if cell is not None else "" for cell in row])

    print(f"Extracted {len(data)} rows from '{latest_file['name']}'.")

    # 5. 타겟 스프레드시트에 '판매상품리스트' 시트로 덮어쓰기
    target_id = os.environ["TARGET_SPREADSHEET_ID"]
    print("Updating target Google Sheet...")
    target_sheet = gspread_client.open_by_key(target_id).worksheet("판매상품리스트")
    
    target_sheet.clear()
    target_sheet.update("A1", data)
    print("Successfully updated '판매상품리스트'!")

if __name__ == "__main__":
    sync_sheets()
