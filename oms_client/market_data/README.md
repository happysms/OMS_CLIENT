# 원천 시세 데이터

이 폴더는 저장된 호가를 읽고 거래소의 공개 시세를 받는 부분입니다.

- `parquet.py`: `/data/{거래소}/ob/{종목}/...` 탐색, 저장 범위 확인, DuckDB 호가 집계와 단일 종목 내보내기. 파일은 읽기만 합니다.
- `quotes.py`: 빗썸·업비트·OKX 공개 WebSocket 연결, 재연결과 최우선 매수·매도 호가 정규화.
- `validation.py`: 시세 식별자, 시간·정수 검증, 조회 한도와 공통 오류 타입.
- `export.py`: 실제 저장 호가를 JSON 스냅샷으로 내보내는 CLI. `python -m oms_client.market_data.export --help`로 실행 방법을 확인합니다.

새 데이터 형식이나 거래소 시세 연결은 여기에 추가합니다. 가격 조합식과 통계는 `../analysis/`, 체결·잔고 계산은 `../backtesting/` 또는 `../paper_trading/`에서 담당합니다. 실제 주문 API는 연결하지 않습니다.
