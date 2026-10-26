# 화면 회귀 검증

`tools/check_frontend.py`가 각 DOM 시나리오를 독립 Node 프로세스에서 실행한다. jsdom으로 실제 HTML과 화면 controller를 읽고 API 응답만 가짜 값으로 주입한다. 실제 거래소·운영 서버·운영 DB에 연결하지 않는다. 네트워크 preload가 실제 TCP/TLS/HTTP/fetch를 차단하며 Python 전략 카탈로그는 매 실행의 임시 DB에서 생성한다. 화면의 픽셀 배치·실제 브라우저 렌더링·실제 거래소 통합 검증은 별도다.

```bash
# 최초 설치가 필요한 환경에서만 실행
npm install --prefix tests/frontend
/home/minsung1/dev/.venv/bin/python tools/check_frontend.py
# 이미 설치한 jsdom 사용
/home/minsung1/dev/.venv/bin/python tools/check_frontend.py --jsdom /tmp/midprice-dom-qa/node_modules/jsdom/lib/api.js
# 수정한 시나리오만 재검증
/home/minsung1/dev/.venv/bin/python tools/check_frontend.py --scenarios pnl-currency storage --output /tmp/oms-dom-results.json
```

`manual_trading.mjs`와 `scenarios/*.mjs`를 자동으로 찾는다. 원래 기능별 작업에서 사용한 화면 테스트를 저장소로 옮겨 절대 경로와 공유 임시 카탈로그 의존성을 없앴다. 과거 테스트의 2종 전략·옛 Point 이름·손익 표 열 수는 현재 세 전략과 환율/거래대금 열에 맞췄다. 계좌 복사는 원래 전략 revision을 읽는 비동기 GET을 기다리되 매매 변경 요청이 없음을 검증한다. 가격·손익·실행 차단·헤지 지연의 기존 기대 결과는 유지한다.

`support.mjs`는 경로·jsdom·카탈로그 로딩만 담당한다. 스위트는 API와 DOM 상태를 공유하지 않으며 30초 제한을 넘기거나 assertion이 실패하면 전체 명령이 실패한다. `--output` 결과에는 성공 여부와 각 시나리오 출력이 저장된다.

`unified-paper`는 단일 모의투자 탭, 과거/실시간 시세 전환, 계좌 입력 보존, 이전 링크 호환, 전략 진입, 보이는 방식의 조회만 실행되는지와 실주문 입력 비노출을 검사합니다.
