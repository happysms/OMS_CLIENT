# 애플리케이션 상태 저장

`state.py`의 `StateStore`가 로그인 설정·세션 키·저장한 조합·실행 이력을 SQLite에 저장합니다. 기본 DB는 저장소의 `.state/dashboard.sqlite3`이며 `DASHBOARD_STATE_DIR`로 위치를 지정합니다.

모의 계좌의 테이블과 거래 트랜잭션은 `paper_trading/service.py`가 같은 DB에서 관리합니다. 원본 시장 데이터는 `market_data/`가 `/data`에서 읽습니다. DB와 내보낸 결과는 Git에 포함하지 않습니다.
