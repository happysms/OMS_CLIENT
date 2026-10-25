# 실행 환경과 외부 연결

`tunnel.py`가 Cloudflare 터널 프로세스, HTTPS 주소, 상태 파일, 로그 순환을 관리합니다. 루트 `routing.py`에서 실행합니다.

프로세스 설치와 재시작은 루트의 `deploy/`가 담당합니다. 터널 실행 파일은 `.tools/cloudflared`, 상태·로그는 `.state/`에 둡니다. 기준 경로는 `oms_client/paths.py`를 사용합니다.
