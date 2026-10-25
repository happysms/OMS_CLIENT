"""Export actual local parquet history into a portable dashboard snapshot.

Examples (run with the workspace virtualenv Python):
    python -m oms_client.market_data.export
    python -m oms_client.market_data.export --days 3 --tickers CT BTC ETH
    python -m oms_client.market_data.export --output /private/export/path

Each symbol keeps its own latest available seven-day window. Missing source
intervals are omitted; quotes are never forward-filled. The one-minute bid and
ask values are arithmetic means of all valid quotes in that minute.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
import time

import pandas as pd

from . import parquet
from .validation import DataError, IDENTIFIER, iso_timestamp
from ..paths import ROOT


DEFAULT_TICKERS = ["CT", "BTC", "ETH", "SOL", "XRP", "DOGE"]
DEFAULT_EXCHANGES = ["bithumb", "okx", "upbit"]


def _atomic_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".export-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _names(values: list[str], uppercase: bool = False) -> list[str]:
    output = []
    for value in values:
        for item in value.split(","):
            item = item.strip().upper() if uppercase else item.strip().lower()
            if not IDENTIFIER.fullmatch(item):
                raise DataError(f"잘못된 거래소·종목 이름: {item}")
            if item not in output:
                output.append(item)
    return output


def _prices(values) -> list[float]:
    return [float(f"{value:.10g}") for value in values]


def export(output: Path, days: int = 7, tickers=None, exchanges=None) -> dict:
    if not 1 <= days <= 7:
        raise DataError("--days는 1~7 범위여야 합니다.")
    tickers = _names(tickers or DEFAULT_TICKERS, uppercase=True)
    exchanges = _names(exchanges or DEFAULT_EXCHANGES)
    available = parquet.list_symbols()
    output = output.resolve()
    generated_at = pd.Timestamp.now(tz="UTC").isoformat()
    catalog = {"version": 1, "generated_at": generated_at, "interval": "1min", "exchanges": {}, "series": {}}
    started = time.monotonic()
    for exchange in exchanges:
        for ticker in tickers:
            if ticker not in available.get(exchange, []):
                print(f"skip {exchange}:{ticker}: no source directory", flush=True)
                continue
            timer = time.monotonic()
            try:
                limits = parquet.bounds(exchange, ticker)
                end = pd.Timestamp(limits["end"]) + pd.Timedelta(milliseconds=1)
                start = max(pd.Timestamp(limits["start"]), end - pd.Timedelta(days=days))
                leg = parquet.export_leg(exchange, ticker, start.isoformat(), end.isoformat(), "1min")
            except DataError as exc:
                print(f"skip {exchange}:{ticker}: {exc}", flush=True)
                continue
            filename = f"{exchange}-{ticker}.json"
            series = {
                "exchange": exchange, "ticker": ticker, "interval": "1min",
                "generated_at": generated_at, "source": "parquet",
                "times": leg["times"], "bid": _prices(leg["bid"]), "ask": _prices(leg["ask"]),
            }
            _atomic_json(output / filename, series)
            catalog["exchanges"].setdefault(exchange, []).append(ticker)
            catalog["series"][f"{exchange}:{ticker}"] = {
                "file": f"data/{filename}", "start": leg["times"][0], "end": leg["times"][-1],
                "count": len(leg["times"]),
            }
            print(f"{exchange}:{ticker}: {len(leg['times']):,} points, "
                  f"{iso_timestamp(leg['times'][0])} .. {iso_timestamp(leg['times'][-1])}, "
                  f"{time.monotonic() - timer:.2f}s", flush=True)
    if not catalog["series"]:
        raise DataError("내보낼 수 있는 실제 호가 데이터가 없습니다.")

    legs = [
        {"id": "A", "exchange": "bithumb", "ticker": "CT"},
        {"id": "B", "exchange": "okx", "ticker": "CT"},
        {"id": "C", "exchange": "bithumb", "ticker": "BTC"},
        {"id": "D", "exchange": "okx", "ticker": "BTC"},
    ]
    if not all(f"{leg['exchange']}:{leg['ticker']}" in catalog["series"] for leg in legs):
        keys = list(catalog["series"])[:4]
        legs = [{"id": chr(65 + i), "exchange": key.split(":")[0], "ticker": key.split(":")[1]} for i, key in enumerate(keys)]
    selected = [catalog["series"][f"{leg['exchange']}:{leg['ticker']}"] for leg in legs]
    common_end = min(item["end"] for item in selected)
    common_start = max(item["start"] for item in selected)
    if common_start > common_end:
        # A custom selection may contain completely disjoint histories.
        legs, selected = legs[:1], selected[:1]
        common_start, common_end = selected[0]["start"], selected[0]["end"]
    catalog["defaults"] = {
        "legs": legs,
        "formula": "((A / B) / (C / D) - 1) * 100" if len(legs) == 4 else "A",
        "interval": "5min",
        "start": iso_timestamp(max(common_start, common_end - 24 * 60 * 60 * 1000)),
        "end": iso_timestamp(common_end),
    }
    _atomic_json(output / "catalog.json", catalog)
    total_bytes = sum((output / Path(item["file"]).name).stat().st_size for item in catalog["series"].values())
    total_bytes += (output / "catalog.json").stat().st_size
    print(f"Exported {len(catalog['series'])} series, {total_bytes / 1024 / 1024:.2f} MiB, {time.monotonic() - started:.2f}s", flush=True)
    return catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=ROOT / "exports")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS)
    parser.add_argument("--exchanges", nargs="+", default=DEFAULT_EXCHANGES)
    args = parser.parse_args()
    try:
        export(args.output, args.days, args.tickers, args.exchanges)
    except DataError as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
