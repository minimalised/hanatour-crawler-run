import os
import json
import io
import pandas as pd
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

def download_from_google_drive(file_id, service_account_info):
    """구글 드라이브에서 엑셀 또는 CSV 파일 다운로드"""
    print("1. 구글 드라이브에서 원본 파일 다운로드 중...")
    creds = service_account.Credentials.from_service_account_info(
        service_account_info, 
        scopes=['https://www.googleapis.com/auth/drive']
    )
    service = build('drive', 'v3', credentials=creds)
    
    request = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while done is False:
        status, done = downloader.next_chunk()
        print(f"다운로드 진행률: {int(status.progress() * 100)}%")
        
    fh.seek(0)
    return fh, service

def upload_to_google_drive(service, parent_id, file_name, df_result):
    """중복 제거 + 재조합 완료된 파일을 다시 구글 드라이브에 업로드"""
    print("5. 최종 결과를 구글 드라이브에 업로드 중...")
    
    # 메모리 상에서 CSV 파일 형태로 변환
    output_stream = io.StringIO()
    df_result.to_csv(output_stream, index=False, encoding='utf-8-sig')
    output_stream.seek(0)
    
    # 바이트 스트림으로 변환
    bio = io.BytesIO(output_stream.getvalue().encode('utf-8-sig'))
    
    file_metadata = {
        'name': file_name,
        'parents': [parent_id] if parent_id else []
    }
    media = MediaIoBaseUpload(bio, mimetype='text/csv', resumable=True)
    
    # 파일 생성
    uploaded_file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id'
    ).execute()
    print(f"🎉 업로드 완료! 구글 드라이브 파일 ID: {uploaded_file.get('id')}")

def extract_date_from_code(code):
    """기존 앱스 스크립트의 고속 정렬 기준 (판매상품코드에서 날짜 부분 정렬 조건 추출)"""
    code_str = str(code) if pd.notnull(code) else ""
    # 기존 코드: codeA.length >= 12 ? codeA.substring(6, 12) : "999999"
    if len(code_str) >= 12:
        return code_str[6:12]
    return "999999"

def process_product_pipeline():
    # 1. 환경 변수에서 구글 인증 정보 및 파일 ID 가져오기
    env_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    file_id = os.environ.get("GOOGLE_DRIVE_FILE_ID")
    parent_folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID") # 결과물이 저장될 드라이브 폴더 ID (선택)
    
    if not env_json or not file_id:
        raise ValueError("GitHub Secrets 설정(인증 정보 또는 파일 ID)을 확인해주세요.")
        
    service_account_info = json.loads(env_json)
    
    # 2. 파일 다운로드
    file_stream, drive_service = download_from_google_drive(file_id, service_account_info)
    
    # 3. 데이터 로드 (55만 행 대용량)
    print("2. 55만 행 대용량 데이터 로드 중...")
    # 원본 파일이 엑셀(.xlsx)이면 pd.read_excel, CSV 파일이면 pd.read_csv를 사용하세요.
    # 여기서는 대용량에 더 흔히 쓰이는 CSV 기준으로 작성했습니다.
    df = pd.read_csv(file_stream) 
    print(f"데이터 로드 완료: 총 {len(df)}행")
    
    # 4. [최적화 1] 고속 문자열 정렬 (기존 앱스 스크립트의 localeCompare 로직 이식)
    print("3. 고속 정렬 알고리즘 작동 중...")
    # '판매상품코드' 컬럼에서 날짜 키를 추출하여 임시 정렬 컬럼 생성
    df['sort_key'] = df['판매상품코드'].apply(extract_date_from_code)
    # 정렬 수행
    df = df.sort_values(by='sort_key', ascending=True).drop(columns=['sort_key'])
    
    # 5. [최적화 2] 단일 루프 Set 필터링 (Pandas 고속 중복 제거 기능 사용)
    print("4. 판매상품명 기준 중복 제거 정제 시작...")
    # '판매상품명'이 빈 값(결측치)인 행 제거
    df = df.dropna(subset=['판매상품명'])
    # 기존 앱스 스크립트처럼 처음 매칭된 데이터를 유지(keep='first')하며 중복 제거
    df_cleaned = df.drop_duplicates(subset=['판매상품명'], keep='first')
    print(f"정제 완료: {len(df_cleaned)}행 남음.")
    
    # -------------------------------------------------------------------------
    # 🔥 6. 여기에 기존 질문자님의 [3-1, 3-2 상품명 재조합 로직]을 연결합니다.
    print("5. 상품명 재조합 단계 진입 (기존 3-1, 3-2 단계)...")
    
    recombined_rows = []
    
    for index, row in df_cleaned.iterrows():
        original_name = row['판매상품명']
        product_code = row['판매상품코드']
        
        # 💡 [질문자님의 기존 재조합 로직을 이 아래에 구현해 넣으시면 됩니다]
        # 예시:
        # if 일치조건_만족:
        #     final_name = original_name
        # else:
        #     final_name = f"[재조합] {original_name}"
        
        final_name = original_name  # 임시 지정 (실제 로직으로 대체 필요)
        
        # 행 데이터를 복사하고 최종 상품명을 업데이트
        new_row = row.to_dict()
        new_row['판매상품명'] = final_name  # 혹은 신규 컬럼에 추가
        recombined_rows.append(new_row)
        
    df_final_result = pd.DataFrame(recombined_rows)
    # -------------------------------------------------------------------------
    
    # 7. 구글 드라이브에 정제 및 재조합 완료된 파일 업로드 ("상품명_중복제거.csv")
    upload_to_google_drive(drive_service, parent_folder_id, "상품명_중복제거.csv", df_final_result)
    print("✨ 모든 프로세스가 완벽하게 종료되었습니다.")

if __name__ == "__main__":
    process_product_pipeline()
