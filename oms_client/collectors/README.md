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

## 과거 1분봉 보완

`usdt_candles.py`는 두 거래소 공개 REST API에서 최근 30일의 완료된 USDT/KRW 1분봉을 한 번 내려받습니다. 현재 진행 중인 분봉은 제외하고, 체결이 없어 응답에 없는 분은 빈 구간으로 남깁니다. 과거 매수·매도 호가나 호가 잔량을 복원하지 않습니다. 기존 1초 실시간 수집과 독립적으로 실행하며 호가 파일을 수정하지 않습니다.

```bash
python backfill_usdt.py --days 30 --exchange all
python backfill_usdt.py --days 0.01 --data-root /tmp/usdt-candles-smoke
```

`--exchange bithumb|upbit|all`, `--data-root`, `--max-requests`를 지원합니다. 기본 데이터 루트는 실시간 수집기와 같은 `data/market` 또는 `DASHBOARD_COLLECTOR_DATA_ROOT`입니다. UTC 현재 분 시작부터 과거로 지정 일수만큼 조회하며, 소수 일수는 완료된 분 단위로 내림합니다(최소 1분). `--days`는 최대 365일입니다.

파일은 `{data_root}/{bithumb|upbit}/candles_1m/USDT/YYYY/MM/DD.parquet`에 UTC 날짜별로 저장합니다. `candleTime`은 봉 시작 시각 ms이고, `availableTime=candleTime+60000`은 완료된 OHLCV를 분석에 사용할 수 있는 시각입니다. 봉 내부의 마지막 체결 시각을 완료 시각으로 사용하지 않습니다. `open`·`high`·`low`·`close`는 KRW 가격, `volume`은 실제 USDT 체결량, `trade_value`는 KRW 체결 금액입니다. `fetchedAt`은 실제 REST 조회 시각 ms입니다. `market=KRW-USDT`, `price_unit=KRW`, `quantity_unit=USDT`, `candle_interval_ms=60000`, `source=rest_1m`을 함께 저장합니다. 1초 호가와 비교할 때 체결량과 호가 잔량을 구분해야 합니다.

페이지당 최대 200개를 받아 가장 오래된 봉 시작 시각을 다음 exclusive cursor로 사용합니다. 내부 cursor는 UTC이며, [빗썸 공식 문서](https://apidocs.bithumb.com/reference/%EB%B6%84minute-%EC%BA%94%EB%93%A4-%EC%A1%B0%ED%9A%8C)에 맞춰 빗썸 요청의 `to`만 KST 오프셋 없는 문자열로 변환합니다. [업비트 공식 문서](https://docs.upbit.com/kr/reference/list-candles-minutes)에 따라 업비트는 UTC `Z` 문자열을 보냅니다. 양쪽의 요청 형식은 실제 공개 REST 조회로 대조했습니다.

전체 HTTP 요청은 최대 초당 3회이며 429·5xx·네트워크 오류는 제한된 횟수로 재시도합니다. `Retry-After`를 반영하고, 전체 HTTP 시도는 재시도 포함 기본 2,000회로 제한합니다(`--max-requests`, 최대 20,000). HTTP 200이라도 오류 객체이거나 잘못된 시장·단위·OHLCV이면 실패합니다. 페이지가 과거로 진행하지 않아도 중단합니다. 실패 전에 저장한 날짜 파일은 보존하며 재실행할 수 있습니다.

날짜별 기존 완료 봉은 `candleTime` 기준으로 보존하고 없는 봉만 추가합니다. 재실행해도 중복이나 기존 `fetchedAt` 변경이 없습니다. Parquet 임시 파일·fsync·원자적 교체·디렉터리 fsync로 반영하며, 별도 `.usdt-candles-1m.lock`이 중복 backfill만 차단합니다. 작업 결과는 마지막에 거래소별 페이지 수·반영 봉 수·시작/끝 시각을 JSON으로 출력합니다.
