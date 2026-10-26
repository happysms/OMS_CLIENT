# 웹 API

브라우저의 요청을 인증·검증하고 담당 도메인 서비스에 전달합니다. 가격 계산이나 모의 체결 규칙은 해당 기능 패키지에 둡니다.

`routes/storage.py`의 `GET /api/storage`, `POST /api/storage/policy`, `POST /api/storage/preview`는 서버 디스크 용량·보관 설정·삭제 후보 미리보기를 제공합니다. 자동 삭제는 기본 꺼짐이며 로그인·CSRF 정책을 그대로 적용합니다. 시세를 사용하는 동기 API 요청에는 읽기 사용권을 부여하고 요청 종료 시 반환합니다. 백그라운드 작업은 각 작업 전체에 별도 읽기 사용권을 유지합니다. 상세 범위는 [저장 공간 문서](../../docs/storage_retention.ko.md)를 따릅니다.

전체 공통 종목의 영구 괴리 집계는 `routes/premiums.py`에서 제공합니다. `GET /api/premiums`는 최근 3일·7일 평균과 충족률·작업 상태, `GET /api/premiums/hourly?ticker=BTC`는 최근 168시간 결과, `GET /api/premiums/csv`는 전체 종목 요약 CSV입니다. `POST /api/premiums/refresh`는 증분 갱신을 요청하고 `POST /api/premiums/settings`의 `{"enabled":true}`는 자동 누적 설정을 저장합니다. 모두 기존 로그인·CSRF 정책을 따릅니다. 원본이 갱신된 시간만 교체 저장하며 일반 분석 결과 캐시와 분리됩니다.

| 파일 / 폴더 | 담당 |
|---|---|
| `app.py` | Flask 생성, 설정, 저장소·작업 큐·라우트 연결 |
| `auth.py`, `templates/` | 로그인, 세션, CSRF, 출처 검사, 로그인 화면 |
| `jobs.py` | 분석·백테스트 작업 실행, 실행 이력, 결과 보관 한도 |
| `exports.py` | 전체 분석 결과·거래 내역 CSV 생성. 가격의 원본 호가 시각과 펀딩의 퍼센트·비율 원단위·관측 메타데이터를 유형에 맞게 보존 |
| `data_exports.py` | 원본·집계 CSV 비동기 변환, 전용 작업 한도·다운로드 파일 보관 |
| `validation.py`, `errors.py` | HTTP 입력 검증과 오류 응답 |
| `services.py` | 실시간 분석·모의 계좌의 지연 생성과 종료 |
| `routes/data.py` | 가격·펀딩·틱 종목 목록과 저장 범위, 틱 차트·원본 페이지·전체 CSV, 가격 CSV 생성·진행 상태·다운로드 |
| `routes/analysis.py` | 과거·실시간 분석, 조합 저장, 이력·CSV |
| `GET /api/replays/<id>/fills/<sequence>/context` | 체결 전후 각 60초의 저장 체결·주문 종료와 원본 호가·틱. 진행 중 미래는 숨기며 조회만 수행. 동시 1건, 초과 429. |
| `routes/backtesting.py` | 백테스트 요청·결과·거래 CSV |
| `routes/paper_trading.py` | 모의 계좌·주문·초기화 |
| `routes/oms.py` | OMS 연결 상태 |
| `routes/workspace.py` | 탭 목록, 세션 정보, 상태 확인, 화면 파일 제공 |

새 계산은 도메인에, HTTP 경로는 `routes/`의 해당 기능에 추가합니다. 화면 파일을 추가하면 `routes/workspace.py`의 공개 파일 목록에도 등록합니다. 문서·검사 도구·상태 파일은 웹으로 제공하지 않습니다.

모의 체결 보고는 `GET /api/paper/fills?before=<sequence>&limit=100`으로 순번 기준 조회합니다(최대 500건). `GET /api/paper/fills/csv`는 전체 체결과 고정 평가 근거를 내보냅니다. 주문 응답의 `fill_report`는 이번 체결 당시 상태이고 일반 `account`는 조회 시점의 상태입니다. `/api/paper`에는 최근 100개 `fills`, 전체 `fill_count`, `next_fill_before`가 포함됩니다.

과거 MM 시뮬레이션은 `routes/replays.py`에서 제공합니다. `GET/POST /api/replays`는 최근 실행/새 실행, `GET /api/replays/<id>`는 저장 스냅샷, `POST /api/replays/<id>/control`은 일시정지·재개·배속·빠른 실행·수동 주문·취소·MM 제어입니다. `GET /api/replays/<id>/fills?before=<sequence>`는 과거 체결 100건, `/fills.csv`는 요청 시점까지의 체결 CSV입니다. 모두 기존 로그인과 변경 요청 CSRF를 적용합니다. 세부 계약은 [재생 문서](../replay/README.md)를 참고하세요.

`POST /api/replays`는 선택적인 `account_name`(80자 이내)을 받고 매번 새로운 `virtual_account`를 생성합니다. `GET /api/replays?before=N`은 생성 순번 기준 계좌별 결과 20개와 `next_before`를 반환합니다. `GET /api/replays/<id>/result.json`은 계좌·초기조건·저장 손익·전체 체결을 한 시점 기준으로 내보내며, 기존 로그인 정책을 적용합니다.

`routes/strategies.py`는 `/api/strategies` 목록/등록, `/<id>?revision=N` 버전 조회, `/<id>/revisions` 새 버전, `/<id>/archive` 보관/복원을 제공합니다. 모의 실행 생성은 `strategy_id`·`strategy_revision`으로 정확한 등록 설정을 선택하며 Point 실행은 아직 거절합니다. 신규 실행의 이론가는 전략 출력으로 받고 `theory_price` 고정 값은 거절합니다.

전략 유형 `minmaxma`는 `point_st` 기반 별도 등록 프로필입니다. `reference_exchange`에 따라 빗썸+Binance 또는 빗썸+OKX를 요구하며, 현재 실행 연결 전 상태를 반환합니다. `builtin_minmaxma`는 기존 기본 전략·버전을 변경하지 않고 추가됩니다.
