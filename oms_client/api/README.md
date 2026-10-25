# 웹 API

브라우저의 요청을 인증·검증하고 담당 도메인 서비스에 전달합니다. 가격 계산이나 모의 체결 규칙은 해당 기능 패키지에 둡니다.

| 파일 / 폴더 | 담당 |
|---|---|
| `app.py` | Flask 생성, 설정, 저장소·작업 큐·라우트 연결 |
| `auth.py`, `templates/` | 로그인, 세션, CSRF, 출처 검사, 로그인 화면 |
| `jobs.py` | 분석·백테스트 작업 실행, 실행 이력, 결과 보관 한도 |
| `exports.py` | 전체 분석 결과·거래 내역 CSV 생성 |
| `validation.py`, `errors.py` | HTTP 입력 검증과 오류 응답 |
| `services.py` | 실시간 분석·모의 계좌의 지연 생성과 종료 |
| `routes/data.py` | 종목 목록·저장 범위 조회 |
| `routes/analysis.py` | 과거·실시간 분석, 조합 저장, 이력·CSV |
| `routes/backtesting.py` | 백테스트 요청·결과·거래 CSV |
| `routes/paper_trading.py` | 모의 계좌·주문·초기화 |
| `routes/oms.py` | OMS 연결 상태 |
| `routes/workspace.py` | 탭 목록, 세션 정보, 상태 확인, 화면 파일 제공 |

새 계산은 도메인에, HTTP 경로는 `routes/`의 해당 기능에 추가합니다. 화면 파일을 추가하면 `routes/workspace.py`의 공개 파일 목록에도 등록합니다. 문서·검사 도구·상태 파일은 웹으로 제공하지 않습니다.
