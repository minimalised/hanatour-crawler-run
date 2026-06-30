import os
import json
import pandas as pd
from google.oauth2 import service_account
from googleapiclient.discovery import build

def get_sheets_service(env_json):
    """구글 시트 API 서비스 객체 생성"""
    creds = service_account.Credentials.from_service_account_info(
        env_json, 
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    return build('sheets', 'v4', credentials=creds)

def extract_date_from_code(code):
    """기존 앱스 스크립트의 고속 정렬 기준 (판매상품코드에서 날짜 부분 추출)"""
    code_str = str(code) if pd.notnull(code) else ""
    if len(code_str) >= 12:
        return code_str[6:12]
    return "999999"

def process_product_pipeline():
    # 1. 환경 변수에서 GitHub Secrets 값 로드
    raw_json = os.environ.get("GOOGLE_JSON_RAW")
    source_id = os.environ.get("SOURCE_SPREADSHEET_ID")
    target_id = os.environ.get("TARGET_SPREADSHEET_ID")
    
    if not raw_json or not source_id or not target_id:
        raise ValueError("GitHub Secrets 설정이 누락되었습니다. 이미지의 변수명들을 다시 확인해주세요.")
        
    service_account_info = json.loads(raw_json)
    sheets_service = get_sheets_service(service_account_info)
    
    # 2. 원본 구글 시트에서 데이터 가져오기 (판매상품리스트 시트의 A:Z 전체)
    print("1. '판매상품리스트' 시트에서 55만 행 데이터 로드 중...")
    range_name = "'판매상품리스트'!A:Z" 
    result = sheets_service.spreadsheets().values().get(
        spreadsheetId=source_id, 
        range=range_name
    ).execute()
    
    rows = result.get('values', [])
    if not rows:
        print("처리할 데이터가 원본 시트에 없습니다.")
        return
        
    # 3. Pandas 데이터프레임으로 변환
    header = rows[0]
    data = rows[1:]
    df = pd.DataFrame(data, columns=header)
    print(f"데이터 로드 완료: 총 {len(df)}행")
    
    # 예외 방지: 판매상품코드와 판매상품명 컬럼이 없으면 에러 방지용 검사
    if '판매상품코드' not in df.columns or '판매상품명' not in df.columns:
        raise KeyError("시트에 '판매상품코드' 또는 '판매상품명' 컬럼명이 정확히 존재하는지 확인해주세요.")

    # 4. [최적화 1] 고속 문자열 정렬 (기존 앱스 스크립트 localeCompare 로직)
    print("2. 고속 정렬 알고리즘 작동 중...")
    df['sort_key'] = df['판매상품코드'].apply(extract_date_from_code)
    df = df.sort_values(by='sort_key', ascending=True).drop(columns=['sort_key'])
    
    # 5. [최적화 2] 판매상품명 기준 중복 제거 (기존 앱스 스크립트 Set 필터링 로직)
    print("3. 판매상품명 기준 중복 제거 정제 시작...")
    df = df.dropna(subset=['판매상품명'])
    df = df[df['판매상품명'].str.strip() != ""] # 빈 공백 문자열 제거
    df_cleaned = df.drop_duplicates(subset=['판매상품명'], keep='first')
    print(f"정제 완료: {len(df_cleaned)}행 남음.")
    
    # -------------------------------------------------------------------------
    # 🔥 6. 여기에 기존 3-1, 3-2 상품명 재조합 로직을 연결하세요!
    print("4. 상품명 재조합 단계 진입 (기존 3-1, 3-2 단계)...")
    
    recombined_rows = []
    for index, row in df_cleaned.iterrows():
        original_name = row['판매상품명']
        
        # 💡 [질문자님의 기존 재조합 로직이 들어갈 자리]
        # 만약 OpenAI API를 쓰신다면 os.environ.get("OPENAI_API_KEY")로 키를 가져와서 쓰시면 됩니다.
        final_name = original_name # 임시값
        
        new_row = row.to_dict()
        new_row['판매상품명'] = final_name
        recombined_rows.append(new_row)
        
    df_final_result = pd.DataFrame(recombined_rows)
    # -------------------------------------------------------------------------
    
    # 7. 타겟 구글 시트("상품명_중복제거")에 결과 밀어넣기
    print("5. 타겟 스프레드시트에 결과 데이터 쓰는 중...")
    
    # 덮어쓰기를 위해 데이터프레임을 다시 리스트 형태로 변환 (헤더 포함)
    output_values = [df_final_result.columns.tolist()] + df_final_result.values.tolist()
    
    # 기존 데이터 깨끗하게 비우기 (Clear)
    sheets_service.spreadsheets().values().clear(
        spreadsheetId=target_id,
        range="A:Z"
    ).execute()
    
    # 정제된 데이터 쓰기 (Update)
    body = {'values': output_values}
    sheets_service.spreadsheets().values().update(
        spreadsheetId=target_id,
        range="A1",
        valueInputOption="RAW",
        body=body
    ).execute()
    
    print("✨ 모든 프로세스가 완벽하게 종료되었습니다. 타겟 스프레드시트를 확인하세요!")

if __name__ == "__main__":
    process_product_pipeline()
