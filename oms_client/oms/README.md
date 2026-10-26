# 원격 OMS 연동

실제 매매와 거래소 API 키는 별도 IP의 실행 서버에 둔다. 이 대시보드는 전략 파라미터 변경, 실시간 상태와 거래내역 표시를 담당한다.

[원격 전략 실행 서버 소켓 연동 정의서](../../docs/remote_execution_socket.ko.md)가 구현 계약이다. 세 전략의 입력·출력·설정 변경, WebSocket 연결, durable 명령과 체결, 재접속·snapshot·lease·단절 정책을 정의한다. [JSON Schema](../../docs/contracts/remote_execution.v1.schema.json)와 [예제](../../docs/contracts/remote_execution.v1.examples.json)를 상대 서버 개발자가 사용할 수 있다.

`status.py`와 `/api/oms/status`는 remote·planned·connected=false를 반환한다. 아직 WSS 게이트웨이나 원격 명령 실행이 구현된 것은 아니다. 로컬 직접 주문 경로는 기본 비활성화했다. 프로필 등록, 가상계좌, 모의투자, 과거 시뮬레이션은 기존 모듈에서 유지한다.
