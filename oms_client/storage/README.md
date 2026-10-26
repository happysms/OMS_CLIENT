# 애플리케이션 상태 저장

`state.py`의 `StateStore`가 로그인 설정·세션 키·저장한 조합·실행 이력을 SQLite에 저장합니다. 기본 DB는 저장소의 `.state/dashboard.sqlite3`이며 `DASHBOARD_STATE_DIR`로 위치를 지정합니다.

모의 계좌의 테이블과 거래 트랜잭션은 `paper_trading/service.py`가 같은 DB에서 관리합니다. 원본 시장 데이터는 `market_data/`가 `/data`에서 읽습니다. DB와 내보낸 결과는 Git에 포함하지 않습니다.

`premiums.py`는 같은 상태 디렉터리의 `premiums.sqlite3`에 빗썸·OKX 공통 종목의 시간별 괴리 합계·표본 수·최솟값·최댓값·원본 fingerprint를 영구 보관합니다. 분석 CSV 임시 캐시의 TTL이나 서버 재시작으로 삭제하지 않습니다. 3일·7일 평균은 합계와 표본 수에서 조회 시 계산합니다. 자동 설정·작업 진행률도 저장하고 집계 프로세스 간 배타 잠금은 `premiums.lock`을 사용합니다. 원본 호가와 로그인·모의계좌 DB를 변경하지 않습니다.

`orderbook_samples.py`는 독립 USDT 수집기의 SQLite WAL spool과 UTC 시간별 Parquet 저장을 담당합니다. 먼저 커밋한 행을 임시 Parquet·fsync·원자적 교체로 반영하고 성공한 행만 spool에서 삭제합니다. 데이터 루트와 상태 디렉터리를 각각 잠그며, 재시작 후 `(exchange, sampleTime)`으로 병합합니다. 스키마·단위·실행 방법은 `../collectors/README.md`에 있습니다.

호가 수집기의 2~5단계 깊이는 nullable 컬럼으로 확장합니다. 이전 SQLite spool과 기존 시간 파일의 1호가 기록을 유지하며 과거에 없던 깊이를 추정하지 않습니다. `candle_samples.py`는 공개 REST 완료 1분봉을 별도 잠금과 UTC 날짜별 Parquet으로 보관합니다.
