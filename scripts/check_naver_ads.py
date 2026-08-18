import os
import time
import json
import base64
import hashlib
import hmac
import requests
import gspread
from google.oauth2.service_account import Credentials

# ----------------------------------------------------
# 1. GitHub Secrets에서 전달된 환경변수 로드
# ----------------------------------------------------
NAVER_API_LICENSE_KEY = (os.environ.get("NAVER_API_LICENSE_KEY") or "").strip()
NAVER_API_SECRET_KEY = (os.environ.get("NAVER_API_SECRET_KEY") or "").strip()
NAVER_CUSTOMER_ID = (os.environ.get("NAVER_CUSTOMER_ID") or "").strip()

TARGET_SPREADSHEET_ID = (os.environ.get("TARGET_SPREADSHEET_ID") or "").strip()
GOOGLE_JSON_RAW = os.environ.get("GOOGLE_JSON_RAW")

# 기획전 종료 판단 키워드 (HTML 본문에 포함 시 '종료' 처리)
EXPIRED_KEYWORDS = [
    "종료된 기획전", 
    "존재하지 않는 페이지", 
    "판매가 종료", 
    "이벤트가 종료", 
    "찾을 수 없습니다",
    "판매 중지"
]

# ----------------------------------------------------
# 2. 네이버 검색광고 API HMAC 서명 생성
# ----------------------------------------------------
def generate_signature(timestamp, method, uri, secret_key):
    if not secret_key:
        raise ValueError("NAVER_API_SECRET_KEY 환경변수가 설정되지 않았습니다.")
    message = f"{timestamp}.{method}.{uri}"
    hash_obj = hmac.new(secret_key.encode('utf-8'), message.encode('utf-8'), hashlib.sha256).digest()
    return base64.b64encode(hash_obj).decode('utf-8')

def get_naver_headers(method, uri):
    timestamp = str(int(time.time() * 1000))
    signature = generate_signature(timestamp, method, uri, NAVER_API_SECRET_KEY)
    return {
        "Content-Type": "application/json; charset=UTF-8",
        "X-Timestamp": timestamp,
        "X-API-KEY": NAVER_API_LICENSE_KEY,
        "X-Customer": str(NAVER_CUSTOMER_ID),
        "X-Signature": signature
    }

# ----------------------------------------------------
# 3. 네이버 전체 소재 Landing URL 수집
# ----------------------------------------------------
def fetch_all_ads():
    base_url = "https://api.searchad.naver.com"
    ad_list = []

    # 1) 전체 캠페인 조회
    uri_camp = "/ncc/campaigns"
    res_camp = requests.get(f"{base_url}{uri_camp}", headers=get_naver_headers("GET", uri_camp))
    
    if res_camp.status_code != 200:
        print(f"[Error] 캠페인 조회 실패 (HTTP {res_camp.status_code}): {res_camp.text}")
        return []
    
    campaigns = res_camp.json()

    for camp in campaigns:
        camp_id = camp.get('nccCampaignId')
        camp_name = camp.get('name', '')

        # 2) 해당 캠페인의 광고그룹 조회
        uri_groups = "/ncc/adgroups"
        res_groups = requests.get(
            f"{base_url}{uri_groups}", 
            headers=get_naver_headers("GET", uri_groups), 
            params={"nccCampaignId": camp_id}
        )
        
        if res_groups.status_code != 200:
            continue
            
        adgroups = res_groups.json()

        for group in adgroups:
            group_id = group.get('nccAdgroupId')
            group_name = group.get('name', '')

            # 3) 광고그룹 내 소재 조회
            uri_ads = "/ncc/ads"
            res_ads = requests.get(
                f"{base_url}{uri_ads}", 
                headers=get_naver_headers("GET", uri_ads), 
                params={"nccAdgroupId": group_id}
            )
            
            if res_ads.status_code == 200:
                ads = res_ads.json()
                for ad in ads:
                    ad_detail = ad.get('ad', {})
                    pc_url = ad_detail.get('pc', {}).get('final', '')
                    mobile_url = ad_detail.get('mobile', {}).get('final', '')

                    ad_list.append({
                        "ad_id": ad.get('nccAdId'),
                        "campaign_name": camp_name,
                        "adgroup_name": group_name,
                        "pc_url": pc_url,
                        "mobile_url": mobile_url,
                        "inspect_status": ad.get('inspectStatus', '')
                    })
            time.sleep(0.05)  # API Rate Limit 준수

    return ad_list

# ----------------------------------------------------
# 4. 랜딩 URL 접속 상태 및 기획전 종료 여부 점검
# ----------------------------------------------------
def check_url_status(url):
    if not url:
        return "URL 없음", "-"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(url, timeout=10, headers=headers, allow_redirects=True)
        
        if response.status_code != 200:
            return f"HTTP {response.status_code}", "오류"

        # HTML 본문 내 종료 키워드 검사
        page_text = response.text
        for kw in EXPIRED_KEYWORDS:
            if kw in page_text:
                return f"종료 문구 감지('{kw}')", "종료"

        return "정상 접속", "정상"

    except requests.exceptions.Timeout:
        return "타임아웃 (10초 초과)", "오류"
    except Exception as e:
        return f"접속 실패 ({str(e)[:25]})", "오류"

# ----------------------------------------------------
# 5. 구글 스프레드시트 데이터 업데이트
# ----------------------------------------------------
def main():
    print(">>> 1. 네이버 검색광고 API 연동 및 소재 수집 시작...")
    ads = fetch_all_ads()
    print(f"총 {len(ads)}개의 소재를 수집했습니다.")

    headers = ["캠페인명", "광고그룹명", "소재 ID", "기기 구분", "랜딩 URL", "상세 점검 결과", "상태"]
    rows = [headers]

    print(">>> 2. 랜딩 URL 접속 및 기획전 종료 상태 점검 중...")
    for idx, item in enumerate(ads, start=1):
        if item['pc_url']:
            result_msg, status = check_url_status(item['pc_url'])
            rows.append([item['campaign_name'], item['adgroup_name'], item['ad_id'], "PC", item['pc_url'], result_msg, status])
            
        if item['mobile_url']:
            result_msg, status = check_url_status(item['mobile_url'])
            rows.append([item['campaign_name'], item['adgroup_name'], item['ad_id'], "MO", item['mobile_url'], result_msg, status])

        if idx % 20 == 0:
            print(f"진행 상황: {idx}/{len(ads)} 소재 검사 완료")

    print(">>> 3. 구글 스프레드시트 기록 중...")
    
    # GOOGLE_JSON_RAW 환경변수를 직접 JSON으로 파싱하여 인증
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    service_account_info = json.loads(GOOGLE_JSON_RAW)
    creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    client = gspread.authorize(creds)

    # 1) 스프레드시트 열기
    doc = client.open_by_key(TARGET_SPREADSHEET_ID)

    # 2) '기획전URL' 시트 선택 (없으면 자동 생성)
    TARGET_SHEET_NAME = "기획전URL"
    try:
        sheet = doc.worksheet(TARGET_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        print(f"'{TARGET_SHEET_NAME}' 시트가 존재하지 않아 새 시트를 생성합니다.")
        sheet = doc.add_worksheet(title=TARGET_SHEET_NAME, rows="1000", cols="10")

    # 3) 기존 내용 초기화 후 데이터 업데이트
    sheet.clear()
    sheet.update('A1', rows)

    print(f">>> '{TARGET_SHEET_NAME}' 시트에 업데이트가 완료되었습니다!")

if __name__ == "__main__":
    main()
