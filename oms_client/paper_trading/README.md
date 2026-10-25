# 모의투자

이 폴더는 서버의 공개 시세로 가상 주문을 체결하고 모의 계좌를 유지하는 부분입니다.

- `service.py`의 `PublicQuoteProvider`: 종목별 공개 호가 구독과 신선한 시세 조회.
- `service.py`의 `PaperTradingService`: 가상 현금·포지션·주문 내역, 수수료·슬리피지, 중복 주문 방지와 계좌 초기화.
- `__init__.py`: 외부에서 사용할 서비스 진입점.

시세 연결은 `../market_data/quotes.py`, SQLite 연결은 `../storage/state.py`를 사용합니다. 실제 계좌 인증이나 실주문 호출은 포함하지 않습니다. 향후 OMS 연결은 별도 `../oms/`의 책임이며 모의 원장과 구분합니다.
