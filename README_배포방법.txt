# KREAM · POIZON 역소싱 V12 MOBILE

## 목적
집 PC의 localhost가 아니라, 아울렛/코스트코 등 외부에서 휴대폰 브라우저로 접속해
POIZON/KREAM 데이터를 넣고 자동 비교 → 텔레그램 전송까지 하는 현장용 버전입니다.

## 배포 준비
이 폴더의 아래 파일을 GitHub 저장소에 올립니다.
- streamlit_app.py
- requirements.txt
- .gitignore
- .streamlit/config.toml

중요: `.streamlit/secrets.toml` 실제 파일은 GitHub에 절대 올리지 마세요.

## Streamlit Community Cloud 배포
1. GitHub에 새 저장소를 만들고 위 파일들을 업로드합니다.
2. Streamlit Community Cloud에서 Create app을 누릅니다.
3. 해당 GitHub 저장소와 `streamlit_app.py`를 선택합니다.
4. Advanced settings > Secrets에 아래 3개를 넣습니다.

APP_PASSWORD = "현장접속용비밀번호"
TELEGRAM_BOT_TOKEN = "실제봇토큰"
TELEGRAM_CHAT_ID = "실제챗ID"

5. Deploy 후 생성된 `https://....streamlit.app` 주소를 휴대폰 홈 화면에 저장합니다.

## 현장 사용 순서
상품 발견 → 모델/품번·가격 확인 → POIZON 가져오기 → KREAM 가져오기 →
자동 비교 → 후보 저장 → 텔레그램 전송 → 최종 매입판정.

## 주의
Community Cloud의 로컬 파일 저장은 영구 데이터베이스 용도로 보장되지 않습니다.
V12 MOBILE은 '현장 판정' 우선 버전입니다.
장기 실적/후보 데이터는 다음 버전에서 외부 저장소(Google Sheets/DB 등) 연동을 권장합니다.
