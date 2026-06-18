import os
import io
import json
from datetime import datetime
import pandas as pd
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import gspread

# GitHub Secrets에 저장한 구글 인증 키 불러오기
creds_dict = json.loads(os.environ["GOOGLE_JSON_RAW"])
scopes = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/spreadsheets'
]
creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)

# API 서비스 빌드
drive_service = build('drive', 'v3', credentials=creds)
gc = gspread.authorize(creds)

# 환경 변수 및 날짜 계산
SPREADSHEET_ID = os.environ["TARGET_SPREADSHEET_ID"]
TARGET_SHEET_NAME = "raw2"
CURRENT_MONTH = f"{datetime.now().month}월"  # 월이 바뀌면 '7월', '8월' 자동 계산

print(f"⏰ {CURRENT_MONTH} 데이터 동기화를 시작합니다.")

# 1. '상품데이터 배포(X월)' 폴더 찾기
query = f"mimeType = 'application/vnd.google-apps.folder' and name contains '상품데이터 배포({CURRENT_MONTH})'"
results = drive_service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
folders = results.get('files', [])

if not folders:
    print(f"❌ {CURRENT_MONTH} 폴더를 찾을 수 없습니다.")
    exit()

folder_id = folders[0]['id']

# 2. 폴더 내에서 가장 최신 '판매상품리스트_*.xlsx' 파일 찾기
file_query = f"'{folder_id}' in parents and name contains '판매상품리스트_' and mimeType = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'"
file_results = drive_service.files().list(q=file_query, orderBy='createdTime desc', fields='files(id, name)').execute()
files = file_results.get('files', [])

if not files:
    print("❌ 처리할 신규 엑셀 파일이 없습니다.")
    exit()

latest_file = files[0]
print(f"👉 최신 파일 발견: {latest_file['name']}")

# 3. 엑셀 파일 메모리로 다운로드
request = drive_service.files().get_media(fileId=latest_file['id'])
fh = io.BytesIO()
downloader = MediaIoBaseDownload(fh, request)
done = False
while not done:
    status, done = downloader.next_chunk()
fh.seek(0)

# 4. Pandas로 50만 행 고속 로드 (결측치 처리 등)
df = pd.read_excel(fh)
df = df.fillna("")  # 빈 칸 처리
data_to_upload = [df.columns.values.tolist()] + df.values.tolist()

# 5. 내 구글 스프레드시트의 특정 시트 비우고 새로 밀어넣기
sh = gc.open_by_key(SPREADSHEET_ID)
worksheet = sh.worksheet(TARGET_SHEET_NAME)
worksheet.clear()  # 기존 50만 행 삭제
worksheet.update('A1', data_to_upload)  # 새 50만 행 업로드

print("✅ 구글 스프레드시트 업데이트 완료!")
