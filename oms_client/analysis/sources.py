"""Shared source identifiers and bounds for historical/live analysis."""

from string import ascii_uppercase

from ..market_data.validation import DataError, MAX_BUCKETS

SOURCE_IDS = tuple(ascii_uppercase)
SOURCE_ID_SET = frozenset(SOURCE_IDS)
MAX_SOURCES = len(SOURCE_IDS)
# Bound output arrays as well as loading work, including aliased instruments.
MAX_TOTAL_SOURCE_BUCKETS = 4 * MAX_BUCKETS


def source_map(legs):
    """Validate IDs without loading or validating unreferenced instruments."""
    if not isinstance(legs, list) or not 1 <= len(legs) <= MAX_SOURCES:
        raise DataError(f"데이터 소스를 1~{MAX_SOURCES}개 선택해 주세요.")
    by_id = {}
    for leg in legs:
        if (
            not isinstance(leg, dict)
            or not isinstance(leg.get('id'), str)
            or leg['id'] not in SOURCE_ID_SET
            or leg['id'] in by_id
        ):
            raise DataError("데이터 소스 이름은 중복 없는 A~Z로 지정해 주세요.")
        by_id[leg['id']] = leg
    return by_id
