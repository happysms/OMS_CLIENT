"""Validation at the API boundary; replay domain objects receive normalized inputs."""

from decimal import Decimal as D

from ..market_data.validation import DataError, parse_date
from ..strategies.simple_mm import StartInputs, _amount


def config_from(payload) -> dict:
    allowed = {
        'ticker',
        'start',
        'end',
        'initial_krw',
        'initial_coin',
        'fee_bps',
        'quantity_step',
        'min_notional',
        'latency_ms',
        'speed',
        'fast',
        'include_okx',
        'auto_mm',
        'theory_price',
        'account_name',
        'queue_cancel_mode',
        'queue_cancel_bias',
        'queue_cancel_chunks',
        'queue_cancel_seed',
        'maker_fee_bps',
        'taker_fee_bps',
        'price_step',
        'end_order_policy',
        'cancel_ack_policy',
    }
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise DataError('시뮬레이션 설정을 확인해 주세요.')
    try:
        name = payload.get('account_name', '')
        if not isinstance(name, str) or len(name.strip()) > 80 or any((ord(c) < 32 for c in name)):
            raise ValueError('계좌 이름은 줄바꿈 없이 80자 이내로 입력하세요.')
        seed = StartInputs(
            payload.get('ticker'),
            payload.get('initial_krw', '1000000'),
            payload.get('initial_coin', '0'),
        )
        start, end = (
            parse_date(payload.get('start'), '시작'),
            parse_date(payload.get('end'), '종료'),
        )
        if not 0 < (end - start).total_seconds() <= 7 * 86400:
            raise ValueError('재생 기간은 0초 초과 7일 이하여야 합니다.')
        speed, latency = (payload.get('speed', 1), payload.get('latency_ms', 20))
        if (
            type(speed) is not int
            or not 1 <= speed <= 32
            or type(latency) is not int
            or (not 0 <= latency <= 10000)
        ):
            raise ValueError('배속은 1~32, 지연은 0~10000ms로 지정하세요.')
        for key in ('fast', 'include_okx', 'auto_mm'):
            if key in payload and type(payload[key]) is not bool:
                raise ValueError('모드 설정은 참/거짓이어야 합니다.')
        fee = _amount(payload.get('fee_bps', '5'), 'fee_bps')
        if fee > 1000:
            raise ValueError('수수료는 0~1000bp로 지정하세요.')
        role_fees = {
            key: _amount(payload.get(key, str(fee)), key)
            for key in ('maker_fee_bps', 'taker_fee_bps')
        }
        if any((value > 1000 for value in role_fees.values())):
            raise ValueError('수수료는 0~1000bp로 지정하세요.')
        price_step = _amount(payload.get('price_step', '0'), 'price_step')
        end_policy = payload.get('end_order_policy', 'cancel')
        if end_policy not in ('cancel', 'preserve'):
            raise ValueError('종료 주문 정책을 확인하세요.')
        cancel_ack_policy = payload.get('cancel_ack_policy', 'immediate')
        if cancel_ack_policy not in ('immediate', 'on_ack'):
            raise ValueError('접수 전 취소 정책을 확인하세요.')
        step = _amount(payload.get('quantity_step', '0.00000001'), 'quantity_step', positive=True)
        minimum = _amount(payload.get('min_notional', '5000'), 'min_notional', positive=True)
        theory = payload.get('theory_price')
        theory = (
            str(_amount(theory, 'theory_price', positive=True))
            if theory not in (None, '')
            else None
        )
        queue_mode = payload.get('queue_cancel_mode', 'probabilistic')
        if not isinstance(queue_mode, str) or queue_mode not in {'conservative', 'probabilistic'}:
            raise ValueError('호가 취소 추정 모형을 확인하세요.')
        bias = _amount(payload.get('queue_cancel_bias', '1'), 'queue_cancel_bias', positive=True)
        if not D('0.01') <= bias <= 100:
            raise ValueError('앞쪽 취소 가중치는 0.01~100입니다.')
        chunks, random_seed = (
            payload.get('queue_cancel_chunks', 32),
            payload.get('queue_cancel_seed', 1),
        )
        if type(chunks) is not int or not 1 <= chunks <= 256:
            raise ValueError('취소 분할 수는 1~256 정수입니다.')
        if type(random_seed) is not int or not 0 <= random_seed <= 4294967295:
            raise ValueError('난수 시드는 0~4294967295 정수입니다.')
        return dict(
            account_name=name.strip(),
            ticker=seed.ticker,
            start_ms=int(start.value // 1000000),
            end_ms=int(end.value // 1000000),
            initial_krw=str(seed.initial_krw),
            initial_coin=str(seed.initial_coin),
            fee_bps=str(fee),
            quantity_step=str(step),
            min_notional=str(minimum),
            latency_ms=latency,
            speed=speed,
            fast=payload.get('fast', False),
            auto_mm=payload.get('auto_mm', True),
            theory_price=theory,
            queue_cancel_mode=queue_mode,
            queue_cancel_bias=str(bias),
            queue_cancel_chunks=chunks,
            queue_cancel_seed=random_seed,
            **{k: str(v) for k, v in role_fees.items()},
            price_step=str(price_step),
            end_order_policy=end_policy,
            cancel_ack_policy=cancel_ack_policy,
            venues=['bithumb', 'okx'] if payload.get('include_okx', False) else ['bithumb'],
        )
    except ValueError as exc:
        raise DataError(str(exc)) from exc
