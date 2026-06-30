import os
import json
import pandas as pd
from google.oauth2.service_account import Credentials
import gspread

# ==========================================
# 1. GitHub Secrets 기반 설정
# ==========================================
SPREADSHEET_ID = os.environ.get("SOURCE_SPREADSHEET_ID")

def extract_date_from_code(code):
    """기존 앱스 스크립트의 고속 정렬 기준 (판매상품코드에서 날짜 부분 추출)"""
    code_str = str(code) if pd.notnull(code) else ""
    if len(code_str) >= 12:
        return code_str[6:12]
    return "999999"

def main():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    google_json_raw = os.environ.get("GOOGLE_JSON_RAW")
    
    if not google_json_raw or not SPREADSHEET_ID:
        raise ValueError("GitHub Secrets 설정(GOOGLE_JSON_RAW 또는 SOURCE_SPREADSHEET_ID)을 확인해주세요.")
        
    service_account_info = json.loads(google_json_raw)
    creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    gc = gspread.authorize(creds)
    
    # 1. 원본 '판매상품리스트' 시트 데이터 로드
    print("🛒 1. '판매상품리스트' 시트에서 55만 행 대용량 데이터 로드 중...")
    doc = gc.open_by_key(SPREADSHEET_ID)
    source_sheet = doc.worksheet("판매상품리스트")
    all_values = source_sheet.get_all_values()
    
    if not all_values:
        print("ℹ️ '판매상품리스트' 시트에 데이터가 없습니다.")
        return
        
    header = all_values[0]
    data = all_values[1:]
    
    # 2. Pandas 데이터프레임 변환
    df = pd.DataFrame(data, columns=header)
    print(f"📦 로드 완료: 총 {len(df):,}행")
    
    # 3. 고속 정렬 및 중복 제거
    print("⚡ 2. 파이썬 Pandas 고속 정렬 및 '판매상품명' 기준 중복 제거 시작...")
    
    if '판매상품코드' not in df.columns or '판매상품명' not in df.columns:
        raise KeyError("시트에 '판매상품코드' 또는 '판매상품명' 컬럼이 정확히 존재하는지 확인해주세요.")
        
    # 날짜 기준 정렬
    df['sort_key'] = df['판매상품코드'].apply(extract_date_from_code)
    df = df.sort_values(by='sort_key', ascending=True).drop(columns=['sort_key'])
    
    # 빈 값 제외 및 '판매상품명' 기준 중복 제거 (첫 번째 값 유지)
    df = df.dropna(subset=['판매상품명'])
    df = df[df['판매상품명'].str.strip() != ""]
    df_cleaned = df.drop_duplicates(subset=['판매상품명'], keep='first').copy()
    print(f"🎯 정제 완료: {len(df_cleaned):,}행 남음.")
    
    # 4. '상품명_중복제거2' 시트에 즉시 적재
    print("💾 3. '상품명_중복제거2' 시트에 일괄 적재 시작...")
    try:
        target_sheet = doc.worksheet("상품명_중복제거2")
        target_sheet.clear()
        print("🧹 기존 '상품명_중복제거2' 시트를 비웠습니다.")
    except gspread.exceptions.WorksheetNotFound:
        target_sheet = doc.add_worksheet(title="상품명_중복제거2", rows=1000, cols=len(header))
        print("🆕 '상품명_중복제거2' 시트를 새로 생성했습니다.")
        
    # 헤더와 정제된 데이터를 합쳐서 리스트로 변환
    final_output = [df_cleaned.columns.tolist()] + df_cleaned.values.tolist()
    
    # 5. 대용량 안전 업로드 (1만 행씩 분할 적재)
    total_rows = len(final_output)
    chunk_size = 10000
    
    print(f"🚀 총 {total_rows:,}행(헤더 포함)을 {chunk_size:,}행씩 나누어 안전하게 업로드합니다.")
    
    for i in range(0, total_rows, chunk_size):
        chunk = final_output[i:i + chunk_size]
        start_row = i + 1
        end_row = i + len(chunk)
        
        # A{start_row} 형태로 범위 지정 (예: A1, A10001...)
        range_string = f"A{start_row}"
        
        target_sheet.update(range_name=range_string, values=chunk)
        print(f"  └ [진행] {start_row:,} ~ {end_row:,} 행 적재 완료")

    print(f"\n✨ 모든 프로세스가 성공적으로 종료되었습니다. '상품명_중복제거2' 시트에 최종 반영되었습니다.")

if __name__ == "__main__":
    main()
