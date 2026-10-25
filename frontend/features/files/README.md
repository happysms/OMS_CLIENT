# 데이터 파일 탐색기

`controller.js`는 왼쪽 폴더 트리와 선택 파일의 메타데이터를 표시합니다. 폴더를 펼칠 때만 `/api/data/files`를 호출하고 파일 선택 시 `/api/data/files/info`를 호출합니다. 트리 탐색·검색·페이지·상세 정보 요청을 별도로 관리하여 이전 응답이 새 선택을 덮지 않게 합니다.

실제 디렉터리 접근과 파일 형식 검증은 서버의 `market_data/file_browser.py`가 담당합니다. 조회 가능한 파일의 조건은 `shell/workspace.js`의 `onOpenQuery`를 통해 데이터·펀딩 화면으로 전달하며, 이 모듈은 가격·펀딩을 계산하지 않습니다.
