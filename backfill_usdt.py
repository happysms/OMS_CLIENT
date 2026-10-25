#!/usr/bin/env python3
"""Download completed public KRW-USDT 1-minute candles without changing books."""
from oms_client.collectors.usdt_candles import main


if __name__ == "__main__":
    raise SystemExit(main())
