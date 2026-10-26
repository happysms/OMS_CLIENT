"""Swap book quantities require verified base-coin contract values."""

import io
import json
import traceback
import unittest
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from oms_client.market_data.instruments import (
    LOOKUP_ERROR,
    MAX_RESPONSE_BYTES,
    okx_contract_value,
)
from oms_client.market_data.validation import DataError


def instrument(**changes):
    record = {
        "instId": "BTC-USDT-SWAP",
        "instType": "SWAP",
        "ctType": "linear",
        "ctValCcy": "BTC",
        "settleCcy": "USDT",
        "ctVal": "0.01",
        "ctMult": "1",
    }
    record.update(changes)
    return record


class PublicResponse(io.BytesIO):
    def __init__(self, body):
        super().__init__(body)
        self.read_limits = []

    def read(self, maximum=-1):
        self.read_limits.append(maximum)
        return super().read(maximum)


class FakePublicAPI:
    def __init__(self, payload=None, *, body=None, error=None):
        if payload is None:
            payload = {"code": "0", "data": [instrument()]}
        self.body = json.dumps(payload).encode() if body is None else body
        self.error = error
        self.calls = []
        self.response = None

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        self.response = PublicResponse(self.body)
        return self.response


class InstrumentTests(unittest.TestCase):
    def test_verified_linear_swap_returns_base_coins_and_fixed_bounded_public_request(self):
        api = FakePublicAPI()
        self.assertEqual(okx_contract_value("BTC", opener=api), 0.01)
        request, timeout = api.calls[0]
        url = urlsplit(request.full_url)
        self.assertEqual(
            (url.scheme, url.netloc, url.path),
            ("https", "www.okx.com", "/api/v5/public/instruments"),
        )
        self.assertEqual(parse_qs(url.query), {"instType": ["SWAP"], "instId": ["BTC-USDT-SWAP"]})
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, 8)
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(api.response.read_limits, [MAX_RESPONSE_BYTES + 1])
        self.assertTrue(api.response.closed)

    def test_other_contract_value_is_not_replaced_with_one(self):
        api = FakePublicAPI(
            {"code": "0", "data": [instrument(instId="ETH-USDT-SWAP", ctValCcy="ETH", ctVal="0.1")]}
        )
        self.assertEqual(okx_contract_value("ETH", opener=api), 0.1)

    def test_invalid_tickers_fail_before_any_network_call(self):
        for ticker in (
            None,
            True,
            123,
            [],
            "",
            "btc",
            " BTC",
            "BTC ",
            "BTC-USDT-SWAP",
            "BTC/USDT",
            "BTC?x=1",
            "BTC&instId=ETH",
            "BTC\n",
            "xyz:BTC",
            "БTC",
            "A" * 36,
        ):
            with self.subTest(ticker=ticker):
                api = FakePublicAPI()
                with self.assertRaises(DataError):
                    okx_contract_value(ticker, opener=api)
                self.assertEqual(api.calls, [])

    def test_maximum_ticker_length_and_numeric_prefix_are_accepted(self):
        for ticker in ("1000SATS", "A" * 35):
            with self.subTest(ticker=ticker):
                api = FakePublicAPI(
                    {
                        "code": "0",
                        "data": [instrument(instId=f"{ticker}-USDT-SWAP", ctValCcy=ticker)],
                    }
                )
                self.assertEqual(okx_contract_value(ticker, opener=api), 0.01)

    def test_failed_or_ambiguous_response_is_not_a_contract_value(self):
        for payload in (
            [],
            None,
            {"code": "1", "msg": "upstream-secret", "data": [instrument()]},
            {"code": 0, "data": [instrument()]},
            {"code": "0"},
            {"code": "0", "data": []},
            {"code": "0", "data": {}},
            {"code": "0", "data": [instrument(), instrument()]},
            {"code": "0", "data": ["invalid"]},
        ):
            with self.subTest(payload=payload):
                api = FakePublicAPI(body=json.dumps(payload).encode())
                with self.assertRaisesRegex(DataError, "계약 단위") as caught:
                    okx_contract_value("BTC", opener=api)
                self.assertNotIn("upstream-secret", str(caught.exception))

    def test_exact_instrument_currency_and_linear_swap_are_required(self):
        for field, value in (
            ("instId", "ETH-USDT-SWAP"),
            ("instType", "FUTURES"),
            ("ctType", "inverse"),
            ("ctValCcy", "USD"),
            ("ctValCcy", "ETH"),
            ("settleCcy", "USDC"),
        ):
            with self.subTest(field=field, value=value):
                api = FakePublicAPI({"code": "0", "data": [instrument(**{field: value})]})
                with self.assertRaises(DataError):
                    okx_contract_value("BTC", opener=api)

    def test_missing_contract_fields_are_rejected(self):
        for field in ("instId", "instType", "ctType", "ctValCcy", "settleCcy", "ctVal", "ctMult"):
            with self.subTest(field=field):
                record = instrument()
                del record[field]
                api = FakePublicAPI({"code": "0", "data": [record]})
                with self.assertRaises(DataError):
                    okx_contract_value("BTC", opener=api)

    def test_nonpositive_nonfinite_or_boolean_values_never_fall_back_to_one(self):
        for value in (
            None,
            "",
            0,
            -1,
            True,
            False,
            "NaN",
            "Infinity",
            "-Infinity",
            "1e999",
            "1e-9999",
            [],
            {},
        ):
            with self.subTest(value=value):
                api = FakePublicAPI({"code": "0", "data": [instrument(ctVal=value)]})
                with self.assertRaises(DataError):
                    okx_contract_value("BTC", opener=api)

    def test_unverified_or_nonunit_multiplier_is_rejected(self):
        for value in (None, "", 0, 2, "0.5", True, "NaN", "Infinity"):
            with self.subTest(value=value):
                api = FakePublicAPI({"code": "0", "data": [instrument(ctMult=value)]})
                with self.assertRaises(DataError):
                    okx_contract_value("BTC", opener=api)

    def test_response_size_and_json_decode_failures_are_bounded_and_sanitized(self):
        body = json.dumps({"code": "0", "data": [instrument()]}).encode()
        maximum = body + b" " * (MAX_RESPONSE_BYTES - len(body))
        self.assertEqual(okx_contract_value("BTC", opener=FakePublicAPI(body=maximum)), 0.01)
        for raw in (maximum + b" ", b'{"secret":', b"\xff"):
            with self.subTest(length=len(raw)):
                api = FakePublicAPI(body=raw)
                with self.assertRaises(DataError) as caught:
                    okx_contract_value("BTC", opener=api)
                self.assertEqual(str(caught.exception), LOOKUP_ERROR)
                self.assertTrue(api.response.closed)

    def test_network_and_http_errors_never_expose_urls_or_proxy_credentials(self):
        secret = "https://proxy-user:proxy-secret@private.example.invalid"
        for error in (
            URLError(secret),
            TimeoutError(secret),
            OSError(secret),
            HTTPError(secret, 429, secret, {}, io.BytesIO(b"upstream-secret")),
        ):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(DataError) as caught:
                    okx_contract_value("BTC", opener=FakePublicAPI(error=error))
                self.assertEqual(str(caught.exception), LOOKUP_ERROR)
                rendered = "".join(traceback.format_exception(caught.exception))
                self.assertNotIn("proxy-secret", rendered)
                self.assertNotIn("upstream-secret", rendered)
        self.assertTrue(error.closed)


if __name__ == "__main__":
    unittest.main()
