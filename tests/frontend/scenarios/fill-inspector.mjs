import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { JSDOM, ROOT, FRONTEND, loadStrategyCatalog } from '../support.mjs';
const root = ROOT;
const dom = new JSDOM(await readFile(root + '/frontend/index.html', 'utf8'), {
    url: 'http://localhost/#replay',
    pretendToBeVisual: true,
});
for (const name of ['window', 'document', 'Option', 'Event', 'CustomEvent', 'HTMLElement'])
    globalThis[name] = dom.window[name];
globalThis.ResizeObserver = class {
    observe() {}
    disconnect() {}
};
dom.window.HTMLCanvasElement.prototype.getContext = () => null;
dom.window.HTMLDialogElement.prototype.showModal = function () {
    this.open = true;
};
dom.window.HTMLDialogElement.prototype.close = function () {
    this.open = false;
    this.dispatchEvent(new Event('close'));
};
const { FillInspector } = await import(root + '/frontend/features/replay/controller.js');
const T = Date.parse('2026-10-26T03:00:00Z');
const fill = (n) => ({
    sequence: n,
    fill_id: 'F' + n,
    timestamp: T,
    order_id: 'O' + n,
    side: 'buy',
    price: 100,
    limit_price: 100,
    quantity: 1,
    remaining: 2,
    fee: 0,
    realized_pnl: 5,
    source_tick: 'tick-1',
    account: { realized_pnl: 10, unrealized_pnl: 2, theory_unrealized_pnl: 3 },
    queue_position: { queue_external_ahead: 7, queue_external_behind: 4 },
});
const data = (n) => ({
    run_id: 'run',
    ticker: 'BTC',
    fill: fill(n),
    start_ms: T - 60000,
    end_ms: T + 1001,
    requested_end_ms: T + 60001,
    checkpoint_ms: T + 1000,
    run_start_ms: T - 30000,
    run_end_ms: T + 10000,
    future_hidden: true,
    max_quote_age_ms: 1000,
    fills: Array.from({ length: 150 }, (_, i) => fill(i + 1)),
    order_events: [
        {
            sequence: 1,
            id: 'O1',
            closed_at: T,
            status: 'cancelled',
            quantity_state: { filled: 1, cancelled: 2 },
        },
    ],
    markets: ['bithumb', 'okx'].map((venue) => ({
        venue,
        frames: [T - 1000, T, T + 1000].map((time) => ({
            time,
            book: { time, bids: [{ price: 99, quantity: 2 }], asks: [{ price: 101, quantity: 3 }] },
        })),
        ticks: Array.from({ length: 150 }, (_, i) => ({
            time: T + i,
            price: 100,
            qty: -1,
            source_id: 'tick-' + i,
            direction_known: venue === 'bithumb',
        })),
        streams: {
            book: { status: 'ok', event_count: 3, provenance: 'matched' },
            tick: { status: 'ok', event_count: 150, provenance: 'matched' },
        },
    })),
});
const requests = [];
let defer = false,
    fail = false;
async function api(path, options = {}) {
    assert.equal(options.method, undefined, 'inspection must not mutate replay');
    const seq = Number(path.split('/').at(-2));
    const call = { path, options };
    requests.push(call);
    if (fail) throw new Error('Not found (404)');
    if (defer)
        return new Promise((resolve) => {
            call.resolve = () => resolve(data(seq));
        });
    return data(seq);
}
const view = new FillInspector({ api });
const $ = (id) => document.getElementById('replay-context-' + id),
    flush = () => new Promise((resolve) => setTimeout(resolve, 10));
try {
    await view.open('run', fill(1));
    assert.equal($('dialog').open, true);
    assert.match($('summary').textContent, /F1/);
    assert.match($('stats').textContent, /누적 실현/);
    assert.match($('own-order').textContent, /앞 대기 7개/);
    assert.match($('status').textContent, /이후 구간은 숨김/);
    assert.equal($('position').value, '1');
    assert.equal($('book').rows.length, 2);
    assert.equal($('ticks').rows.length, 100);
    assert.equal($('fills').rows.length, 100);
    assert.equal($('ticks').querySelectorAll('.fill-context-selected').length, 1);
    $('position').value = '0';
    $('position').dispatchEvent(new Event('input'));
    assert.match($('book-time').textContent, /체결 대비 -1초/);
    $('center').click();
    assert.equal($('position').value, '1');
    $('tick-next').click();
    assert.equal($('ticks').rows.length, 50);
    assert.equal($('tick-next').disabled, true);
    $('fill-next').click();
    assert.equal($('fills').rows.length, 50);
    $('venue').value = 'okx';
    $('venue').dispatchEvent(new Event('change'));
    assert.match($('ticks').textContent, /방향 미확인/);
    assert.equal(view.chart.series.length, 2);
    $('fills').querySelector('button').click();
    await flush();
    assert.match(requests.at(-1).path, /fills\/101\/context/);
    defer = true;
    const first = view.open('run', fill(1));
    const a = requests.at(-1);
    const second = view.open('run', fill(2));
    const b = requests.at(-1);
    assert.equal(a.options.signal.aborted, true);
    b.resolve();
    await second;
    a.resolve();
    await first;
    assert.match($('summary').textContent, /F2/);
    assert.equal(view.data.fill.sequence, 2);
    const third = view.open('run', fill(3));
    const c = requests.at(-1);
    $('close').click();
    assert.equal(c.options.signal.aborted, true);
    c.resolve();
    await third;
    assert.equal($('dialog').open, false);
    assert.equal(view.data, null);
    defer = false;
    fail = true;
    await view.open('run', fill(4));
    assert.equal($('error').hidden, false);
    assert.match($('summary').textContent, /F4/);
    assert.equal($('position').disabled, true);
    fail = false;
    $('reload').click();
    await flush();
    assert.equal($('error').hidden, true);
    assert.equal(view.data.fill.sequence, 4);
    console.log(
        'Fill inspector DOM passed: modal, frozen PnL/queue, causal slider, market switching, paging, selected tick, neighbouring fill recenter, stale response rejection, abort/close, old-server error and retry.',
    );
} finally {
    view.close();
    dom.window.close();
}
