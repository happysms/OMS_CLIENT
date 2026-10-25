# 공개 시세 수집

`usdt_orderbook.py`는 빗썸·업비트 `KRW-USDT` 공개 WebSocket의 최우선 매수·매도 가격과 잔량을 받습니다. 최신 정상 호가를 1초에 한 번 스냅샷으로 저장합니다. 두 거래소 연결은 독립적이며 거래소 메시지 자체가 1초마다 도착한다는 뜻은 아닙니다.

```bash
python collect_usdt.py run
python collect_usdt.py status
python collect_usdt.py run --duration 20 --data-root /tmp/usdt-market --state-dir /tmp/usdt-state
```

기본 데이터 위치는 저장소의 `data/market`입니다. `DASHBOARD_COLLECTOR_DATA_ROOT` 또는 `--data-root`로 바꿀 수 있습니다. 기존 `/data` 수집·보존 정책과 분리합니다. 상태와 SQLite WAL spool은 `.state/collectors/usdt`에 두며 `--state-dir`로 바꿉니다. 데이터 루트마다 별도 상태 디렉터리를 사용해야 하며, 잠금과 SQLite 루트 바인딩으로 상태 디렉터리 공유도 차단합니다. 데이터와 상태는 Git에 포함하지 않습니다.

UTC 시간별 파일 경로는 `{data_root}/{bithumb|upbit}/ob_1s/USDT/YYYY/MM/DD/HH.parquet`입니다. 데이터 컬럼은 다음과 같습니다.

| 컬럼 | 의미 |
| --- | --- |
| `sampleTime` | 실제 채택 시각이 속한 UTC 1초 구간의 시작 ms. 거래소별 중복 방지 키 |
| `desktopTime`, `sampledAt` | 스냅샷을 실제로 채택한 로컬 UTC epoch ms. 백테스트 조회 시각 |
| `serverTime` | 거래소 원본 이벤트 시각, ms로 정규화 |
| `receivedTime` | 원본 메시지를 로컬에서 받은 UTC epoch ms |
| `b1_p`, `a1_p` | 같은 원본 메시지의 최우선 매수·매도 가격, KRW |
| `bid_size`, `ask_size` | 같은 메시지의 실제 최우선 USDT 잔량. 가격을 곱한 KRW 금액이 아님 |
| `quote_age_ms` | `sampledAt - serverTime` |
| `price_unit`, `quantity_unit` | `KRW`, `USDT` |
| `sample_interval_ms`, `market` | `1000`, `KRW-USDT` |

원본 이벤트와 로컬 수신 시각 중 하나라도 10초보다 오래됐으면 저장하지 않습니다. 거래소 시계는 현재보다 최대 5초 앞서는 오차를 허용하지만, 로컬 수신 시각이 실제 채택 시각보다 미래인 호가는 저장하지 않습니다. 연결이 끊기면 캐시를 비우며 재연결 후 새 호가를 기다립니다. 가격이 양수가 아니거나 역전됐거나 잔량이 없거나 유한하지 않으면 해당 거래소의 그 초를 건너뜁니다. 정상 잔량 0은 보존합니다. 지연·절전으로 놓친 초를 과거 데이터로 채우지 않습니다. `sampleTime`으로 관측 시각을 앞당겨 해석하지 말고 실제 관측 시각인 `desktopTime`을 사용해야 합니다.

스냅샷은 먼저 SQLite WAL 트랜잭션으로 커밋한 뒤 기본 5초마다 Parquet 임시 파일에 기록하고 fsync·원자적 교체를 수행합니다. 파일과 디렉터리 동기화가 성공한 행만 spool에서 지웁니다. 재시작 후 남은 spool은 `(exchange, sampleTime)`으로 병합하므로 중복으로 늘어나지 않습니다. 데이터 루트 잠금으로 다른 상태 디렉터리를 사용하는 중복 수집기도 차단합니다. 디스크 장애 때 미발행 행은 보존하며, 기본 36,000행 한도에 도달하면 비정상 종료해 서비스가 재시도하도록 합니다. `--flush-seconds`, `--max-pending`으로 한도를 조정할 수 있습니다.

`status.json`은 약 5초마다 원자적으로 갱신합니다. `heartbeat_ms`, 최근 수집·파일 반영 시각, pending 수, 거래소별 연결 상태·최근 원본 호가·건너뛴 이유·오류 횟수를 제공합니다. 호가마다 로그를 남기지 않습니다. SIGTERM/SIGINT에서는 수신을 멈추고 마지막 spool을 파일로 반영합니다. 이 수집기는 인증 키와 주문 API를 사용하지 않습니다.
