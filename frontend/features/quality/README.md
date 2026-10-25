# 데이터 품질 화면

`controller.js`는 기록 시각 차이(`desktopTime - serverTime`, ms)의 조회 조건과 차트를 담당합니다. 서버가 계산한 차이를 표시하며 브라우저에서 원본 시각을 빼거나 음수를 보정하지 않습니다.

`initQuality({api, getCatalog})`는 `ready`, `refresh`, `onEnter`, `onLeave`를 반환합니다. 초기화는 API를 호출하지 않으며 탭에 처음 진입할 때 `/api/quality/catalog`를 읽습니다. 과거 비교는 사용자가 조회를 실행한 뒤 `/api/quality/history`를 요청합니다.

현재 모니터는 `/api/quality/live`를 2초마다 요청합니다. 거래소·종목별 원본 `desktop_ms`를 키로 중복을 제거하고 최대 900개·30분의 표본을 유지합니다. 과거 통계의 전체 행 수·P95와 현재 모니터의 관측 표본은 구분합니다. 일반 자료는 상위 수집 PC의 기록 시각이고 USDT 1초 자료는 최신 호가를 채택한 시각이므로, 둘 다 네트워크 왕복시간이 아닙니다.

설정 변경, 탭 이탈, 문서 숨김은 현재 모니터를 중지합니다. 요청 버전·조건·AbortSignal을 검사해 늦은 응답이 새 조건을 덮지 않도록 합니다. 한 거래소의 오류·누락은 다른 거래소의 결과를 막지 않습니다. 빈 구간은 이전 값으로 채우지 않으며 사용하지 못하는 서버 시각은 사유와 함께 표시합니다.

공통 API 등록·탭 생명주기 연결은 `frontend/shell/`에서 담당합니다. 화면 구조와 스타일은 `frontend/index.html`, `frontend/styles.css`에 있습니다.
