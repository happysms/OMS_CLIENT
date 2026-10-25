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

def _kst_csv_time(timestamp):
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(timestamp / 1000, timezone(timedelta(hours=9))).isoformat(timespec="milliseconds")


def _optional_value(leg, key, index):
    values = leg.get(key)
    return values[index] if values is not None else None


def _write_csv(path: Path, result: dict, unit: str):
    complete = result.get("full") or result
    last_quotes = result.get("meta", {}).get("price_mode") == "last"
    price_legs = [leg for leg in complete["legs"] if leg.get("data_type", "price") == "price"]
    include_mode = last_quotes or any(leg.get("data_type") == "funding" or leg.get("quantity_unit") for leg in complete["legs"])
    # Labels may be supplied through the API. Keep spreadsheet applications from
    # interpreting a display label as a formula; numeric result columns stay numeric.
    if unit.lstrip().startswith(("=", "+", "-", "@")):
        unit = "'" + unit
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
            os.chmod(temporary, 0o600)
            writer = csv.writer(stream)
            header = ["time_kst", "result", "ema", "unit"]
            for leg in complete["legs"]:
                identifier = leg["id"]
                if leg.get("data_type") == "funding":
                    header.extend([f"{identifier}_{leg['exchange']}_{leg['ticker']}_funding_rate_pct", f"{identifier}_funding_rate_ratio", f"{identifier}_source_time_kst", f"{identifier}_server_time_kst", f"{identifier}_next_funding_in_seconds"])
                elif leg.get('data_type') == 'candle':
                    header.extend([f"{identifier}_{leg['exchange']}_{leg['ticker']}_candle_value_krw",
                                   f'{identifier}_last_candle_open', f'{identifier}_last_candle_high',
                                   f'{identifier}_last_candle_low', f'{identifier}_last_candle_close',
                                   f'{identifier}_last_candle_volume_usdt', f'{identifier}_last_candle_trade_value_krw',
                                   f'{identifier}_candle_open_time_kst', f'{identifier}_available_time_kst',
                                   f'{identifier}_source_kind'])
                else:
                    header.extend([f"{identifier}_{leg['exchange']}_{leg['ticker']}_mid", f"{identifier}_bid", f"{identifier}_ask"])
                    if leg.get("quantity_unit") and "bid_size" in leg and "ask_size" in leg:
                        header.extend([f"{identifier}_bid_size", f"{identifier}_ask_size", f"{identifier}_quantity_unit"])
            if include_mode:
                header.append("price_mode")
            if last_quotes:
                header.extend([f"{leg['id']}_quote_time_kst" for leg in price_legs])
            writer.writerow(header)
            for index, timestamp in enumerate(complete["times"]):
                label = datetime.fromtimestamp(timestamp / 1000, timezone(timedelta(hours=9))).isoformat()
                row = [label, complete["result"][index], complete["ewm"][index], unit]
                for leg in complete["legs"]:
                    if leg.get("data_type") == "funding":
                        row.extend([leg["values"][index], leg["funding_rate"][index], _kst_csv_time(leg["source_times"][index]), _kst_csv_time(_optional_value(leg, "server_times", index)), _optional_value(leg, "next_funding_in_seconds", index)])
                    elif leg.get('data_type') == 'candle':
                        row.extend([leg['values'][index], *[leg[field][index] for field in ('open', 'high', 'low', 'close', 'volume', 'trade_value')],
                                    _kst_csv_time(leg['candle_times'][index]), _kst_csv_time(leg['available_times'][index]), leg['source_kind']])
                    else:
                        row.extend([leg["mid"][index], leg["bid"][index], leg["ask"][index]])
                        if leg.get("quantity_unit") and "bid_size" in leg and "ask_size" in leg:
                            row.extend([leg["bid_size"][index], leg["ask_size"][index], leg["quantity_unit"]])
                if include_mode:
                    row.append("last" if last_quotes else "mean")
                if last_quotes:
                    for leg in price_legs:
                        quote_time = leg["quote_times"][index]
                        row.append(_kst_csv_time(quote_time))
                writer.writerow(row)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
