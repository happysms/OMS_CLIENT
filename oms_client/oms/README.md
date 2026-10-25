# OMS 연동

현재 `status.py`는 OMS 미연결 상태를 반환하며 `/api/oms/status`에서 사용합니다.

향후 실제 OMS의 계좌·주문·체결 조회와 전략 실행 제어 어댑터를 이 패키지에 추가합니다. 화면은 `frontend/features/oms/`, HTTP 요청은 `api/routes/oms.py`에 연결합니다. 모의 계좌 체결 규칙은 `paper_trading/`에서 관리합니다.
