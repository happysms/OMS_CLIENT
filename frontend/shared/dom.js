export const $ = (id) => document.getElementById(id);
export const exchanges = {
    bithumb: 'Bithumb',
    upbit: 'Upbit',
    okx: 'OKX · 무기한',
    binance: 'Binance',
    hyperliquid: 'Hyperliquid',
    ls: 'LS',
};
export const milliseconds = {
    '1s': 1000,
    '10s': 10000,
    '1min': 60000,
    '5min': 300000,
    '1h': 3600000,
};
export const number = (v, digits = 4) =>
    Number.isFinite(v)
        ? new Intl.NumberFormat('en-US', { maximumFractionDigits: digits }).format(v)
        : '—';
export const currency = (exchange) =>
    ['bithumb', 'upbit', 'ls'].includes(exchange) ? 'KRW' : 'USDT';
export const price = (v) => number(v, Math.abs(v) < 10 ? 6 : 3);
export const stamp = (value) => (typeof value === 'number' ? value : Date.parse(value));
export const time = (value) =>
    Number.isFinite(stamp(value))
        ? new Intl.DateTimeFormat('ko-KR', {
              timeZone: 'Asia/Seoul',
              year: 'numeric',
              month: '2-digit',
              day: '2-digit',
              hour: '2-digit',
              minute: '2-digit',
              second: '2-digit',
              hour12: false,
          }).format(stamp(value))
        : '—';
export const local = (ms) => new Date(ms + 9 * 3600000).toISOString().slice(0, 16);
export const parseLocal = (value) =>
    Date.parse(/(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : value + '+09:00');
export const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function pnlCurrency(section, value) {
    const key = 'oms-pnl-currency-' + section;
    try {
        if (value !== undefined) localStorage.setItem(key, value === 'USDT' ? 'USDT' : 'KRW');
        return (value ?? localStorage.getItem(key)) === 'USDT' ? 'USDT' : 'KRW';
    } catch {
        return value === 'USDT' ? 'USDT' : 'KRW';
    }
}
export function pnlAccount(report, unit) {
    return (unit === 'USDT' ? report?.usdt?.account : report?.account) || {};
}
export function fillNotional(report, unit) {
    if (unit === 'USDT') return report?.usdt?.fill?.notional;
    const fill = report?.fill ?? report;
    return Number.isFinite(fill?.price) && Number.isFinite(fill?.quantity)
        ? fill.price * fill.quantity
        : null;
}
export function renderTradeActivity(prefix, report, unit) {
    const activity = report?.activity?.[unit] || {},
        digits = unit === 'USDT' ? 6 : 2;
    const metrics = [
        ['누적 거래대금', activity.turnover],
        ['매수 거래대금', activity.buy_turnover],
        ['매도 거래대금', activity.sell_turnover],
        ['누적 수수료', activity.fees_paid],
    ];
    $(prefix + '-activity-stats').replaceChildren(
        ...metrics.map(([title, value]) => {
            const article = document.createElement('article'),
                label = document.createElement('span'),
                strong = document.createElement('strong');
            label.textContent = title + ' · ' + unit;
            strong.textContent = number(value, digits);
            article.append(label, strong);
            return article;
        }),
    );
    $(prefix + '-activity-info').textContent =
        `실제 체결 합계 · 매수 ${number(activity.buy_fill_count, 0)}건 / 매도 ${number(activity.sell_fill_count, 0)}건 (부분체결 포함). 수수료 제외 거래대금이며 미체결·취소 주문과 초기 보유량은 제외합니다. 금액·평균 체결가: ${unit}.` +
        (!report?.activity
            ? ' 이전 결과 또는 서버에는 누적 집계가 없습니다.'
            : activity.status === 'missing_historical_fx'
              ? ' 일부 체결 환율이 없어 관련 USDT 합계는 미평가입니다.'
              : '');
    tableRows(
        prefix + '-activity-markets',
        (activity.markets || []).map((m) => [
            `${exchanges[m.exchange] || m.exchange} / ${m.ticker}`,
            number(m.buy_quantity, 8),
            number(m.sell_quantity, 8),
            number(m.traded_quantity, 8),
            price(m.buy_vwap),
            price(m.sell_vwap),
            number(m.buy_turnover, digits),
            number(m.sell_turnover, digits),
            number(m.turnover, digits),
            number(m.fees_paid, digits),
            number(m.fill_count, 0),
        ]),
        11,
        report?.activity
            ? '아직 체결된 거래가 없습니다.'
            : '이전 결과에는 누적 거래 집계가 없습니다.',
    );
}
export function pnlFill(report, unit) {
    return (unit === 'USDT' ? report?.usdt?.fill : (report?.fill ?? report)) || {};
}
export function pnlTheoryPrice(report, unit) {
    const value = report?.fill
        ? report.execution_theory?.price
        : (report?.theory?.price ?? report?.positions?.[0]?.theory_price);
    if (unit !== 'USDT') return value;
    const rate = report?.usdt?.fill?.fx?.krw_per_usdt;
    return Number.isFinite(value) && Number.isFinite(rate) && rate > 0 ? value / rate : null;
}
export function fxDescription(report, unit) {
    if (unit !== 'USDT')
        return '손익·포지션·체결 금액: KRW. 주문 입력과 원본 호가·틱은 거래소 기준 통화입니다.';
    const fx = report?.usdt?.fx;
    return (
        '손익·포지션·체결 금액: USDT. 체결 원가는 당시 환율, 보유자산 평가는 현재 환율을 적용합니다. ' +
        (fx
            ? `평가 환율 1 USDT = ${number(fx.krw_per_usdt, 4)} KRW · ${time(fx.observed_at_ms)} KST`
            : '평가 환율 없음·지연: 계산할 수 없는 금액은 —') +
        ' · 매매 총손익 + 원화 현금 환산손익 = 초기 대비 평가액 변화. 주문 입력·원본 호가·틱의 통화는 그대로입니다.'
    );
}

export function errorAt(prefix, error) {
    $(prefix + '-error').textContent = error.message || String(error);
    $(prefix + '-error').hidden = false;
}
export function clearError(prefix) {
    $(prefix + '-error').hidden = true;
}
export function tableRows(id, rows, columns, empty) {
    const body = $(id);
    body.replaceChildren();
    if (!rows.length) {
        const row = document.createElement('tr'),
            cell = document.createElement('td');
        cell.colSpan = columns;
        cell.className = 'empty-cell';
        cell.textContent = empty;
        row.append(cell);
        body.append(row);
        return;
    }
    for (const values of rows) {
        const row = document.createElement('tr');
        for (const value of values) {
            const cell = document.createElement('td');
            cell.textContent = value ?? '—';
            row.append(cell);
        }
        body.append(row);
    }
}
export function source(prefix) {
    const input = $(prefix + '-ticker'),
        ticker = input.value.trim();
    return {
        exchange: $(prefix + '-exchange').value,
        ticker: input.dataset.preserveCase === 'true' ? ticker : ticker.toUpperCase(),
    };
}
export function range(prefix) {
    const start = $(prefix + '-start').value,
        end = $(prefix + '-end').value;
    if (
        !Number.isFinite(parseLocal(start)) ||
        !Number.isFinite(parseLocal(end)) ||
        parseLocal(start) >= parseLocal(end)
    )
        throw new Error('시작보다 늦은 종료 시간을 입력해주세요. 시간은 KST 기준입니다.');
    return { start, end, interval: $(prefix + '-interval').value };
}
