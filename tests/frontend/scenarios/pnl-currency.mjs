import { JSDOM, ROOT, FRONTEND, loadStrategyCatalog } from '../support.mjs';
import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
const root = FRONTEND;
const { window } = new JSDOM(await readFile(root + 'index.html', 'utf8'), {
    url: 'http://localhost/',
    pretendToBeVisual: true,
});
for (const name of ['window', 'document', 'Option', 'Event', 'localStorage'])
    globalThis[name] = window[name];
const $ = (id) => document.getElementById(id),
    flush = () => new Promise((r) => setTimeout(r, 20));
const ids = [...document.querySelectorAll('[id]')].map((e) => e.id);
assert.equal(new Set(ids).size, ids.length);
const start = 1792983600000;
const fx = { krw_per_usdt: 1000, observed_at_ms: start, server_time_ms: start, source_id: 'test' };
const account = {
    cash: 8000,
    free_cash: 8000,
    initial_cash: 10000,
    equity: 10000,
    realized_pnl: 0,
    unrealized_pnl: 0,
    theory_unrealized_pnl: 200,
    total_pnl: 0,
    theory_total_pnl: 200,
    coin: 2,
    free_coin: 2,
    quote_currency: 'KRW',
    fee_bps: 0,
    slippage_bps: 0,
};
const uaccount = {
    cash: 8,
    free_cash: 8,
    initial_cash: 10,
    equity: 10,
    realized_pnl: 0,
    unrealized_pnl: 0,
    theory_unrealized_pnl: 0.2,
    total_pnl: 0,
    theory_total_pnl: 0.2,
    cash_fx_pnl: 0,
    equity_change_pnl: 0,
};
const position = {
    exchange: 'bithumb',
    ticker: 'XYZ',
    quantity: 2,
    avg_price: 1000,
    mark_price: 1000,
    market_value: 2000,
    unrealized_pnl: 0,
    theory_price: 1100,
    theory_unrealized_pnl: 200,
    quote_time: start,
};
const uposition = {
    ...position,
    avg_price: 1,
    mark_price: 1,
    market_value: 2,
    theory_price: 1.1,
    theory_unrealized_pnl: 0.2,
};
const fill = {
    timestamp: start,
    order_id: 'O1',
    id: 'F1',
    exchange: 'bithumb',
    ticker: 'XYZ',
    side: 'buy',
    price: 1000,
    quantity: 2,
    fee: 0,
    realized_pnl: 0,
};
const usdt = {
    account: uaccount,
    positions: [uposition],
    fx,
    status: 'complete',
    fill: { price: 1, fee: 0, realized_pnl: 0, fx },
};
const paperReport = {
    sequence: 2,
    fill,
    account,
    positions: [position],
    usdt,
    execution_theory: { price: 1100 },
    valuation_status: 'recorded',
};
let paper = {
    account,
    positions: [position],
    orders: [fill],
    fills: [paperReport],
    fill_count: 2,
    next_fill_before: 2,
    usdt,
};
let replay = {
    id: 'run1',
    revision: 1,
    status: 'finished',
    config: {
        ticker: 'XYZ',
        start_ms: start,
        end_ms: start + 200,
        venues: ['bithumb'],
        auto_mm: false,
    },
    virtual_time_ms: start,
    speed: 1,
    fast: false,
    paused: false,
    total_events: 4,
    skipped_events: 0,
    event_count: 4,
    fill_count: 1,
    books: {
        bithumb: {
            time: start,
            bids: [{ price: 1000, quantity: 3 }],
            asks: [{ price: 1002, quantity: 4 }],
        },
    },
    orders: [],
    order_events: [],
    ticks: [],
    account,
    usdt,
    fills: [
        {
            ...fill,
            sequence: 1,
            account,
            positions: [position],
            usdt,
            theory: { price: 1100, available_at_ms: start },
        },
    ],
};
const calls = [];
async function api(path) {
    calls.push(path);
    if (path === '/api/paper') return structuredClone(paper);
    if (path === '/api/paper/fills?before=2')
        return {
            items: [
                {
                    sequence: 1,
                    fill: { ...fill, id: 'old' },
                    account: null,
                    valuation_status: 'legacy_unavailable',
                },
            ],
            next_before: null,
        };
    if (path === '/api/replays') return { items: [structuredClone(replay)] };
    if (path === '/api/replays/run1') return structuredClone(replay);
    if (path === '/api/strategies') return { items: [] };
    throw Error(path);
}
const { initPaperTrading } = await import(root + 'features/paper_trading/controller.js');
const { initReplay } = await import(root + 'features/replay/controller.js');
const p = initPaperTrading({
    api,
    getCatalog: () => ({
        exchanges: { bithumb: ['XYZ'] },
        defaults: { legs: [{ exchange: 'bithumb', ticker: 'XYZ' }] },
    }),
});
const r = initReplay({ api, getCatalog: () => ({ exchanges: { bithumb: ['XYZ'] } }) });
const choose = (id, value) => {
    $(id).value = value;
    $(id).dispatchEvent(new Event('change'));
};
try {
    p.onEnter();
    r.onEnter();
    await flush();
    assert.equal($('paper-orders').rows[0].cells.length, 19);
    assert.equal($('replay-fills').rows[0].cells.length, 17);
    assert.match(
        $('replay-fills').closest('table').querySelector('thead').textContent,
        /이번 거래대금/,
    );
    assert.equal($('paper-orders').rows[0].cells[4].textContent, '1,000');
    choose('paper-pnl-currency', 'USDT');
    choose('replay-pnl-currency', 'USDT');
    await flush();
    assert.equal($('paper-equity').textContent, '10');
    assert.equal($('paper-reset-currency').value, 'KRW');
    assert.equal($('paper-orders').rows[0].cells[4].textContent, '1');
    assert.equal($('paper-orders').rows[0].cells[15].textContent, '1.1');
    assert.equal($('replay-fills').rows[0].cells[3].textContent, '1');
    assert.equal($('replay-fills').rows[0].cells[12].textContent, '1.1');
    assert.equal($('replay-fills').rows[0].cells[15].textContent, '1,000');
    assert.match($('replay-history').textContent, /10 USDT/);
    assert.match($('replay-book').textContent, /1,000/);
    assert.equal(localStorage.getItem('oms-pnl-currency-replay'), 'USDT');
    // Changing current FX must not alter frozen fills, even on a same-count rerender.
    paper.usdt = {
        ...usdt,
        fx: { ...fx, krw_per_usdt: 2000 },
        account: { ...uaccount, equity: 5 },
    };
    replay = { ...replay, revision: 2, usdt: paper.usdt };
    $('paper-refresh').click();
    $('replay-history-refresh').click();
    await flush();
    $('replay-history').querySelector('button').click();
    await flush();
    assert.equal($('paper-equity').textContent, '5');
    assert.equal($('paper-orders').rows[0].cells[4].textContent, '1');
    assert.equal($('replay-fills').rows[0].cells[3].textContent, '1');
    $('paper-fills-more').click();
    await flush();
    assert.equal($('paper-orders').rows.length, 2);
    assert.equal($('paper-orders').rows[1].cells[4].textContent, '—');
    choose('paper-pnl-currency', 'KRW');
    choose('replay-pnl-currency', 'KRW');
    await flush();
    assert.equal($('paper-orders').rows.length, 2, 'currency switch preserves paged records');
    assert.equal($('paper-orders').rows[0].cells[4].textContent, '1,000');
    assert.equal(
        $('replay-fills').rows[0].cells[3].textContent,
        '1,000',
        'fill cache includes display currency',
    );
    // Old server/record has no USDT data. Never silently show KRW under USDT labels.
    delete paper.usdt;
    delete replay.usdt;
    delete replay.fills[0].usdt;
    paper.fills = [{ ...paperReport, usdt: undefined }];
    choose('paper-pnl-currency', 'USDT');
    choose('replay-pnl-currency', 'USDT');
    $('paper-refresh').click();
    $('replay-history-refresh').click();
    await flush();
    $('replay-history').querySelector('button').click();
    await flush();
    assert.equal($('paper-equity').textContent, '—');
    assert.equal($('paper-orders').rows[0].cells[4].textContent, '—');
    assert.equal($('replay-fills').rows[0].cells[3].textContent, '—');
    assert.match($('paper-fx-info').textContent, /환율 없음/);
    assert.equal($('paper-error').hidden, true, $('paper-error').textContent);
    assert.equal($('replay-error').hidden, true, $('replay-error').textContent);
    console.log(
        'Currency DOM checks passed: KRW/USDT switching, fill cache, saved preference, historical FX, current valuation, pagination, legacy fallback, KRW book/reset, column counts.',
    );
} finally {
    p.onLeave();
    r.onLeave();
    window.close();
}
