# OMS 운영 화면

OMS 주문·체결 모니터링, 포지션·리스크 조회, 전략 실행 제어의 화면 로직을 추가할 위치입니다.

현재는 `frontend/index.html`의 `module-oms` 패널에 연결 예정 안내를 표시합니다. 연결된 OMS나 실주문 제어가 없으므로 별도 컨트롤러는 아직 없습니다.

연동 시 이 폴더에 `controller.js`를 만들고 `frontend/shell/workspace.js`에서 등록합니다. OMS 통신·주문 검증은 서버의 OMS 영역에서 담당하고, 이 폴더에서는 서버가 제공하는 상태와 허용된 동작을 표시합니다.
