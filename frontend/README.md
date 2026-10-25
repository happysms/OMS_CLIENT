# 화면 코드의 역할

`app.js`가 한 번 실행되고, `shell/`이 로그인 세션과 종목 목록을 읽은 다음 각 기능을 연결합니다. 가격 조회·수식 계산·백테스트·모의 주문 체결은 서버 API가 담당하고, 이 폴더는 입력·진행 상태·결과 표시를 담당합니다.

```text
frontend/
├── app.js                       # 브라우저 진입점
├── index.html                   # 5개 탭의 화면 구조와 접근성 속성
├── styles.css                   # 공통 레이아웃·색상·반응형 스타일
├── shell/                       # 앱 시작, 기능 연결, 탭 이동
│   ├── bootstrap.js             # 세션·카탈로그 로딩과 초기화 순서
│   ├── workspace.js             # 기능 등록과 카탈로그 갱신 전달
│   └── tabs.js                  # 탭 선택, 키보드 이동, 분석 탭 이탈 시 실시간 종료
├── features/                    # 기능별 화면 로직과 해당 화면의 상태
│   ├── analysis/controller.js   # 조합식, 과거·실시간 분석, 프리셋·기록·CSV
│   ├── data/controller.js       # 단일 소스 호가 조회, 스프레드·표·CSV
│   ├── backtesting/controller.js# 백테스트 조건, 자산 곡선·성과·체결 표시
│   ├── paper_trading/controller.js # 모의 잔고·포지션·주문·계좌 초기화
│   └── oms/                    # 추후 OMS 운영 화면이 들어갈 위치
├── shared/                     # 여러 기능이 함께 쓰는 화면 도구
│   ├── api.js                  # 쿠키·CSRF·로그인 만료·API 오류 처리
│   ├── charts.js               # 공통 캔버스 차트, 확대·커서
│   ├── dom.js                  # 화면 요소, 숫자·KST 표시, 입력·표 도구
│   ├── source-controls.js      # 거래소·종목 선택과 과거 데이터 범위 입력
│   └── jobs.js                 # 서버 작업 상태 조회와 오래된 응답 배제
└── scripts/check.mjs           # 모든 JavaScript 파일 구문 검사
```

## 기능을 추가하거나 수정할 때

- 분석식을 입력하고 결과를 보여주는 흐름은 `features/analysis/`에서 수정합니다. 실제 계산 규칙은 Python 서버 쪽에 둡니다.
- 특정 탭의 상태는 해당 `controller.js` 안에서 관리합니다. 데이터 조회와 백테스트의 실행 번호도 각각 관리하여, 조건 변경 전 응답이 새 화면을 덮지 않게 합니다.
- 탭에 공통으로 필요한 API·표시·차트 도구만 `shared/`에 둡니다. `shared/`는 특정 기능의 컨트롤러를 가져오지 않습니다.
- 새 탭은 `index.html`에 화면 구조를 만들고, `features/<기능>/`에 컨트롤러를 구현한 뒤 `shell/workspace.js`에서 연결합니다. 탭 전환·키보드 처리는 `shell/tabs.js`가 담당합니다.
- 서버 카탈로그가 바뀌면 `oms:catalog` 이벤트로 다른 화면의 선택 목록과 저장 범위를 갱신합니다.

현재 실행에는 번들러나 프런트엔드 서버가 필요하지 않습니다. Flask가 로그인된 사용자에게 JavaScript 모듈과 CSS를 제공합니다. 새 공개 파일을 추가하면 서버의 정적 파일 허용 목록도 함께 갱신해야 합니다. 문서와 검사 스크립트는 웹 공개 대상이 아닙니다.

검사: `npm --prefix frontend run check` (저장소 루트에서 실행).
