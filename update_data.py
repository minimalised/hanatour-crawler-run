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
print("📊 엑셀 데이터를 로드하는 중...")
df = pd.read_excel(fh)
df = df.fillna("")  # 빈 칸 처리

# 헤더와 데이터 분리
header = df.columns.values.tolist()
values = df.values.tolist()

# 5. 내 구글 스프레드시트의 특정 시트 비우고 새로 밀어넣기
sh = gc.open_by_key(SPREADSHEET_ID)
worksheet = sh.worksheet(TARGET_SHEET_NAME)

print("🧹 기존 시트 데이터를 비우는 중...")
worksheet.clear()  # 기존 데이터 삭제

# 대용량 처리를 위해 데이터를 행 단위로 쪼개어 대량 업로드 (행 용량 초과 방지)
CHUNK_SIZE = 10000  # 1만 행씩 나누어 안전하게 전송
print(f"🚀 {len(values):,}행 데이터를 {CHUNK_SIZE:,}행씩 분할 업로드 시작...")

# 첫 번째 청크에는 헤더를 포함하여 업로드
first_chunk = [header] + values[:CHUNK_SIZE-1]
worksheet.update(first_chunk, 'A1')  # update_values 대신 update 사용

# 그 다음 청크부터 차례대로 아래에 이어 붙이기
start_row = CHUNK_SIZE
for i in range(CHUNK_SIZE - 1, len(values), CHUNK_SIZE):
    chunk = values[i:i+CHUNK_SIZE]
    # 데이터가 밀려 들어갈 정확한 시작 셀 주소 계산 (예: A10000)
    range_start = f"A{start_row}"
    worksheet.update(chunk, range_start)  # update_values 대신 update 사용
    start_row += len(chunk)
    print(f"  .. {start_row:,}행 완료")

print("✅ 구글 스프레드시트 업데이트 완료!")
