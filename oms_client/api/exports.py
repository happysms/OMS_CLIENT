"""Private, atomic CSV exports for complete results and trade ledgers."""
import csv
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

def _csv_value(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value

def _write_trades_csv(path, result):
    trades = result.get("trades", [])
    fields = list(dict.fromkeys(key for trade in trades for key in trade))
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
            os.chmod(temporary, 0o600)
            writer = csv.writer(stream)
            writer.writerow(fields or ["time", "side", "price", "quantity", "fee", "pnl"])
            for trade in trades:
                writer.writerow([_csv_value(trade.get(field)) for field in fields])
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

def _write_csv(path: Path, result: dict, unit: str):
    complete = result.get("full") or result
    # Labels may be supplied through the API. Keep spreadsheet applications from
    # interpreting a display label as a formula; numeric result columns stay numeric.
    if unit.lstrip().startswith(("=", "+", "-", "@")):
        unit = "'" + unit
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
            os.chmod(temporary, 0o600)
            writer = csv.writer(stream)
            writer.writerow(["time_kst", "result", "ema", "unit", *[name for leg in complete["legs"] for name in (f"{leg['id']}_{leg['exchange']}_{leg['ticker']}_mid", f"{leg['id']}_bid", f"{leg['id']}_ask")]])
            for index, timestamp in enumerate(complete["times"]):
                label = datetime.fromtimestamp(timestamp / 1000, timezone(timedelta(hours=9))).isoformat()
                writer.writerow([label, complete["result"][index], complete["ewm"][index], unit, *[value for leg in complete["legs"] for value in (leg["mid"][index], leg["bid"][index], leg["ask"][index])]])
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
