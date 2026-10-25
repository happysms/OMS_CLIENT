"""Validate HTTP payloads before submitting work to domain services."""
import json

from ..analysis import history as data_service

CONFIG_KEYS = {"legs", "formula", "start", "end", "interval", "ewm_span", "max_points", "unit", "price_mode"}

def _query_config(payload):
    if not isinstance(payload, dict):
        raise data_service.DataError("분석 조건은 JSON 객체로 보내 주세요.")
    unknown = set(payload) - CONFIG_KEYS
    if unknown:
        raise data_service.DataError("지원하지 않는 분석 조건입니다. 화면의 조회 설정을 확인해 주세요.")
    if not isinstance(payload.get("legs"), list) or not 1 <= len(payload["legs"]) <= 4:
        raise data_service.DataError("데이터 소스를 1~4개 선택해 주세요.")
    for leg in payload["legs"]:
        if not isinstance(leg, dict):
            raise data_service.DataError("각 데이터 소스는 JSON 객체로 보내 주세요.")
        data_type = leg.get("data_type", "price")
        if not isinstance(data_type, str) or data_type not in {"price", "funding", "candle"}:
            raise data_service.DataError("소스 유형은 가격, 펀딩비율 또는 1분봉 종가로 선택해 주세요.")
    if not isinstance(payload.get("formula"), str) or not 1 <= len(payload["formula"]) <= 256:
        raise data_service.DataError("조합식을 256자 이내로 입력해 주세요.")
    # Copy and validate JSON numbers before handing work to a background thread.
    try:
        copied = json.loads(json.dumps(payload, allow_nan=False))
    except (ValueError, TypeError) as exc:
        raise data_service.DataError("분석 조건에 유효하지 않은 값이 있습니다.") from exc
    if "unit" in copied and (not isinstance(copied["unit"], str) or len(copied["unit"]) > 20):
        raise data_service.DataError("표시 단위를 확인해 주세요.")
    price_mode = copied.get("price_mode", "mean")
    if not isinstance(price_mode, str) or price_mode not in {"mean", "last"}:
        raise data_service.DataError("가격 방식은 구간 마지막 호가 또는 구간 평균으로 선택해 주세요.")
    return copied

def _json_object(payload):
    if not isinstance(payload, dict):
        raise data_service.DataError("요청은 JSON 객체로 보내 주세요.")
    try:
        return json.loads(json.dumps(payload, allow_nan=False))
    except (ValueError, TypeError) as exc:
        raise data_service.DataError("요청에 유효하지 않은 값이 있습니다.") from exc
