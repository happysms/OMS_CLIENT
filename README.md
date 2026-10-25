# OMS Client

데이터 확인, 분석, 백테스트, 모의투자를 한 곳에서 사용하는 웹 도구입니다. 서버가 로컬 Parquet를 DuckDB로 조회·계산하고 브라우저는 조건 입력과 결과 표시를 담당합니다. 앞으로 OMS의 계좌·주문·전략 제어를 같은 작업 공간에 연결할 수 있도록 기능 모듈과 탭을 나눴습니다.

## 현재 기능

| 탭 | 기능 |
|---|---|
| 데이터 | 저장된 호가 종목·기간 확인, 단일 종목 중간가·매수/매도 호가 조회, 표·CSV |
| 분석 | A~D 가격 조합, 과거/실시간 분석, 이동평균·수익률·Z-score, 조합 저장·분석 이력 |
| 백테스트 | 단일 종목 이동평균 전략, 다음 구간 체결, 수수료·슬리피지, 자산 곡선·지표·거래 내역 |
| 모의투자 | 빗썸·업비트 KRW 현금 계좌, 서버 호가 기반 모의 주문, 잔고·포지션·체결 기록 |
| OMS | 계좌·주문·전략 제어를 연결할 예정인 영역. 현재 상태 조회만 제공 |

현재 서버에는 실주문 전송이나 실제 거래 계정 API 키가 없습니다. 모의투자는 별도 SQLite 장부에만 반영됩니다. 기존 운영 OMS의 주문·포지션은 변경하지 않습니다.

## 구조

```text
server.py                       서버 실행 진입점
routing.py                      외부 HTTPS 연결 관리 진입점
oms_client/
  market_data/                  Parquet 조회, 종목·저장 범위, 공개 실시간 시세
  analysis/                     수식 계산, 과거·실시간 분석, 차트 샘플링
  backtesting/                  전략·비용·체결 모형과 성과 계산
  paper_trading/                모의 주문·잔고·포지션·체결 장부
  oms/                          OMS 연결 상태와 향후 운영 연동
  api/                          로그인, HTTP 요청, 작업 큐, 결과 CSV
    routes/                     데이터·분석·백테스트·모의투자·OMS별 API
  storage/                      로그인·조합·조회 이력의 SQLite 저장
  infrastructure/               외부 HTTPS 터널과 프로세스 관리
  cli.py                        서버 실행·비밀번호 설정 명령
  paths.py                      저장소 기준 경로
frontend/
  app.js                        브라우저 진입점
  shell/                        시작 처리와 탭 전환
  features/                     data·analysis·backtesting·paper_trading·oms 화면
  shared/                       공통 차트·API 통신·입력·표시 도구
  scripts/                      개발용 JavaScript 검사 도구
tests/                          기능·인증·동시성·계산 검증
deploy/                         사용자 서비스 설치와 설정 원본
```

각 기능 폴더의 `README.md`에 담당 로직과 수정 위치를 적었습니다. 계산 규칙은 `oms_client/`의 해당 기능에, 요청·응답은 `api/routes/`에, 화면은 `frontend/features/`의 같은 기능에 추가합니다.

요청 흐름은 `frontend → api/routes → 기능 로직 → market_data 또는 storage`입니다. 시장 데이터 접근은 `market_data/`에 모아 분석·백테스트·모의투자가 공유합니다. 실시간 시세의 연결·파싱은 `market_data/quotes.py`, 수식 계산과 시계열 누적은 `analysis/live.py`가 담당합니다.

| 변경할 내용 | 수정 위치 |
|---|---|
| 새로운 데이터 저장 형식·거래소 시세 | `oms_client/market_data/` |
| 새 분석 함수·통계 | `oms_client/analysis/` |
| 전략·체결 가정·백테스트 지표 | `oms_client/backtesting/` |
| 모의 계좌·가상 주문 규칙 | `oms_client/paper_trading/` |
| OMS 계좌·주문·전략 운영 연결 | `oms_client/oms/` |
| 로그인·API·실행 작업 관리 | `oms_client/api/` |
| 각 탭 화면 | `frontend/features/` |
| 서비스 설치·외부 접속 | `deploy/`, `oms_client/infrastructure/` |

원본 데이터, 인증 정보, 계좌 장부, 결과 CSV, 터널 토큰과 실행 파일은 저장소에서 제외합니다. `.state/`, `.tools/`, `.env`는 Git에 포함하지 않습니다. 프런트엔드는 외부 CDN이나 실행 프레임워크 없이 동작합니다.

## 실행

Python 3.12 환경에서 검증했습니다. 기존 작업 공간에서는 `/home/minsung1/dev/.venv/bin/python`을 사용할 수 있습니다.

```bash
cd /home/minsung1/dev/OMS_CLIENT
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python server.py --init-password
.venv/bin/python server.py --host 0.0.0.0 --port 8765
```

브라우저에서 `http://127.0.0.1:8765` 또는 `http://SERVER_IP:8765`에 접속해 비밀번호로 로그인합니다. `0.0.0.0`은 수신 주소이며 다른 기기에서는 실제 서버 IP를 사용합니다. 비밀번호를 바꾸려면 `--init-password`를 다시 실행하고 서버를 재시작합니다.

인증 상태와 세션 키는 `.state/dashboard.sqlite3`에 보관하며 비밀번호는 해시로 저장합니다. 조합·이력·모의 계좌도 이 DB에 저장합니다. 원본 시장 데이터는 기본 `/data`에서 읽고 수정하지 않습니다.

## 데이터와 분석

원본 경로는 `/data/{exchange}/ob/{ticker}/{YYYY}/{MM}/{DD}/{HH}.parquet`입니다. 거래소·종목·날짜별 필요한 파일을 선택하고 DuckDB에서 집계합니다. 중간가는 `(bid + ask) / 2`이며 시간 간격별 유효 호가의 산술평균을 사용합니다. 누락 호가를 이전 가격으로 채우지 않습니다.

수식에는 사칙연산과 `MA`, `EMA`, `DIFF`, `RET`, `ZSCORE`, `ABS`를 사용할 수 있습니다.

```text
((A / B) / (C / D) - 1) * 100
MA(A / B, 60)
RET(A, 1)
ZSCORE(A / B, 60)
```

함수 기간은 유효한 공통 표본 수입니다. 누락이 있으면 60개 표본이 정확히 60분을 의미하지 않습니다. 준비 표본이 부족한 값은 비워 둡니다. EMA는 pandas의 `adjust=True` 방식이며 표준편차는 표본 표준편차입니다. 표시 단위는 계산을 자동 변환하지 않습니다.

한 번에 최대 31일·소스당 250,000구간을 요청할 수 있습니다. 이 값은 조회 요청의 상한입니다. 현재 로더는 `.parquet`를 읽으며, 기존 수집기의 `.parquet.gz`로 보관한 오래된 자료나 체결 `tick` 파일은 이 초기 버전의 조회 대상에 포함하지 않습니다. 실제 조회 가능 범위는 종목별 저장 시각을 확인해야 합니다.

실시간 분석은 서버가 거래소 공개 WebSocket에 직접 연결합니다. 빗썸·업비트 KRW 호가와 OKX USDT 무기한 호가를 지원하며 소스/수신 시각이 10초 이상 오래된 호가는 계산에서 제외합니다. 기존 Redis·Parquet 수집기와 별도 경로이므로 실시간 차트가 움직인다는 사실만으로 아카이브 수집 상태를 판단할 수는 없습니다.

## 백테스트 모형

초기 전략은 단기/장기 이동평균 비교(`ma_cross`)이며 API에서는 매수 후 보유(`buy_hold`)도 제공합니다. 이전 완료 구간의 중간가로 신호를 결정하고 **다음 구간 평균 ask로 매수, 평균 bid로 매도**합니다. 주문 시각은 구간 종료 시각입니다. 시작·종료의 미완료 구간은 제외합니다.

현금 범위에서 전액 매수하며 수수료·불리한 방향의 슬리피지를 각 매매에 반영합니다. 기본값은 초기 자금 1,000,000, 수수료 5bp, 슬리피지 2bp이며 입력해서 바꿀 수 있습니다. 이는 거래소별 실제 요율을 자동 조회한 값이 아닙니다.

구간 평균 체결은 연구용 가정이며 실제 주문장 체결을 재현하지 않습니다. 호가 잔량 소진·시장 충격·최소 주문·수량 단위·공매도·레버리지·펀딩은 반영하지 않습니다. 파생상품 호가를 선택한 경우에도 전액 현금 매수 보유의 가격 참고 모형이며 실제 선물 손익이 아닙니다. 화면의 계산 가정을 함께 확인하세요.

지표와 거래 내역은 전체 완료 구간으로 계산하고 차트만 최대 6,000포인트로 줄입니다. 체결은 작업당 최대 20,000회입니다. 체결 CSV는 차트 축소 전의 전체 거래 내역을 제공합니다.

## 모의투자 모형

현재 모의 계좌는 **KRW 현금·매수 보유**를 지원합니다. 빗썸·업비트 종목을 대상으로 매수는 최신 ask, 매도는 최신 bid에 계좌의 슬리피지·수수료를 적용합니다. 주문 가격을 브라우저에서 지정할 수 없으며 서버가 실시간 호가를 확인합니다.

오래되거나 뒤집힌 호가, 현금 부족, 보유 수량 초과 매도는 거부합니다. 매수 수수료는 평균 매입 원가에 포함합니다. 평가에는 bid를 사용하며 신선한 시세를 받지 못한 보유분은 평가액을 추정하지 않습니다. 잔고와 포지션·체결을 하나의 SQLite 트랜잭션으로 갱신합니다. 선택적 `client_order_id`를 사용하면 같은 요청의 중복 체결을 막을 수 있습니다.

최대 12개 포지션, 시세 구독 최대 32개를 지원합니다. 종목별 시세 구독을 격리하므로 한 종목의 구독 오류가 다른 종목의 시세를 지우지 않습니다. 초기화는 모의 장부만 비우고 설정한 초기 현금으로 되돌립니다. 모의투자 데이터는 서버 재시작 후에도 유지합니다.

## API와 기능 확장

로그인을 제외한 기능 API는 인증이 필요하며 변경 요청은 세션의 CSRF 토큰을 `X-CSRF-Token`으로 전달합니다.

| 영역 | 주요 경로 |
|---|---|
| 메뉴·버전 | `GET /api/modules`, `GET /api/version` |
| 데이터·분석 | `GET /api/catalog`, `GET /api/bounds`, `POST /api/analyses`, 결과·CSV 조회 |
| 백테스트 | `POST /api/backtests`, `GET /api/backtests/{id}`, `GET /api/backtests/{id}/csv` |
| 모의투자 | `GET /api/paper`, `POST /api/paper/orders`, `POST /api/paper/reset` |
| OMS | `GET /api/oms/status` — 현재 연결 예정 상태 |

OMS 제어를 추가할 때는 계좌·실주문·전략 제어 어댑터를 `oms_client/oms/`에 두고 `api/routes/oms.py`와 `frontend/features/oms/`에 연결합니다. 모의 장부와 실계좌의 데이터·주문 경로를 구분하고, UI에 실제 연결 상태를 표시하는 구조를 유지합니다. 현재 백테스트 전략은 `backtesting/engine.py`가 담당하며 이후 공통 전략 실행기가 필요해지면 계산 부분을 추출해 공유할 수 있습니다.

## 배포와 외부 접속

현재 방식은 이 PC에서 Waitress와 Cloudflare Tunnel을 사용자 서비스로 실행하는 구성입니다. 원본 시장 데이터를 별도 호스팅으로 복사하지 않습니다.

```bash
cd /home/minsung1/dev/OMS_CLIENT
OMS_PYTHON=/home/minsung1/dev/.venv/bin/python bash deploy/install_user_services.sh --with-tunnel
systemctl --user status oms-client.service oms-client-route.service
/home/minsung1/dev/.venv/bin/python routing.py status
```

서비스 설치 전에 로그인 비밀번호와 `.tools/cloudflared` 실행 파일이 준비되어 있어야 합니다. `cloudflared`는 [Cloudflare 공식 배포](https://developers.cloudflare.com/tunnel/downloads/)에서 설치합니다. 실행 파일과 터널 자격증명은 Git에 넣지 않습니다.

Quick Tunnel 주소는 연결 프로세스를 새로 시작할 때 변경되며 가용성을 보장하는 고정 주소가 아닙니다. 최신 주소는 `routing.py status`로 확인합니다. [공식 안내](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)

사용자 로그인 시 서비스가 시작되도록 설정하며 로그아웃 이후까지 유지하는 시스템 설정은 별도로 변경하지 않습니다. 대시보드와 터널을 종료하려면 다음 명령을 사용합니다.

```bash
systemctl --user stop oms-client-route.service oms-client.service
```

주요 환경 변수는 `.env.example`에 있습니다. 이 파일은 예시이며 서버가 `.env`를 자동으로 읽지는 않습니다. 서비스 설치 스크립트는 `.state/service.env`에 실제 경로·허용 호스트·배포 커밋을 기록합니다. `OMS_TRUSTED_HOSTS`로 실제 접속 호스트 목록을 지정할 수 있습니다. HTTPS 전달 헤더는 로컬 프록시에서 온 값만 신뢰하며, 직접 외부 요청이 전달한 헤더는 신뢰하지 않습니다.

## 검증

```bash
cd /home/minsung1/dev/OMS_CLIENT
/home/minsung1/dev/.venv/bin/python -W ignore::DeprecationWarning -m unittest discover -s tests -p 'test_*.py'
npm --prefix frontend run check
```

인증·CSRF·경로 제한·조회 정렬·안전한 수식·작업 큐·실시간 시세·백테스트의 다음 구간 체결·수수료·모의 장부 원자성·시세 유효성 등을 검증합니다. 원본 수집기나 운영 OMS를 재시작하거나 변경하는 테스트는 실행하지 않습니다.
