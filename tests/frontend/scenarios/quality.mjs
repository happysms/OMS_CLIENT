import { JSDOM, ROOT, FRONTEND, loadStrategyCatalog } from '../support.mjs';
import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
const root = FRONTEND;
const dom = new JSDOM(await fs.readFile(root + 'index.html', 'utf8'), {
    url: 'http://127.0.0.1:8765/',
    pretendToBeVisual: true,
});
const { window } = dom;
for (const name of ['window', 'document', 'Event', 'Option'])
    Object.defineProperty(globalThis, name, { configurable: true, value: window[name] });
const errors = [];
process.on('unhandledRejection', (error) => errors.push(error));
window.addEventListener('error', (event) => errors.push(event.error || event.message));
let markers = 0;
window.HTMLCanvasElement.prototype.getContext = () =>
    new Proxy(
        { measureText: (text) => ({ width: text.length * 6 }), arc: () => markers++ },
        {
            get: (target, key) => target[key] || (() => {}),
            set: (target, key, value) => ((target[key] = value), true),
        },
    );
window.HTMLCanvasElement.prototype.getBoundingClientRect = () => ({
    width: 700,
    height: 240,
    left: 0,
    top: 0,
});
globalThis.ResizeObserver = class {
    observe() {}
    disconnect() {}
};
const realTimeout = globalThis.setTimeout,
    realClear = globalThis.clearTimeout,
    timers = new Map();
let timerId = 0;
globalThis.setTimeout = (fn, ms, ...args) =>
    ms === 2000
        ? (timers.set(++timerId, () => fn(...args)), timerId)
        : realTimeout(fn, ms, ...args);
globalThis.clearTimeout = (id) => (timers.has(id) ? timers.delete(id) : realClear(id));
const $ = (id) => document.getElementById(id),
    change = (id) => $(id).dispatchEvent(new Event('change', { bubbles: true })),
    input = (id) => $(id).dispatchEvent(new Event('input', { bubbles: true })),
    submit = () =>
        $('quality-form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
async function until(check, description) {
    for (let i = 0; i < 500; i++) {
        if (check()) return;
        await new Promise((resolve) => realTimeout(resolve, 5));
    }
    throw new Error('Timeout ' + description + ' / ' + $('quality-error').textContent);
}
const settle = () => new Promise((resolve) => realTimeout(resolve, 10));
async function tick() {
    assert.equal(timers.size, 1, 'one active poll only');
    const [[id, callback]] = timers;
    timers.delete(id);
    await callback();
}
const chartSnapshots = new Map(),
    { TimeChart } = await import('file://' + root + 'shared/charts.js'),
    originalSetData = TimeChart.prototype.setData;
TimeChart.prototype.setData = function (times, series, ...rest) {
    chartSnapshots.set(this.canvas.id, { times: [...times], series });
    return originalSetData.call(this, times, series, ...rest);
};
const origin = Date.parse('2026-10-25T00:00:00Z'),
    exchangeNames = [
        'bithumb',
        'upbit',
        'okx',
        'binance',
        'hyperliquid',
        'ls',
        'extra1',
        'extra2',
        'extra3',
    ];
const catalog = {
    kinds: [
        { id: 'ob', label: '호가', live_supported: true },
        { id: 'tick', label: '체결 틱', live_supported: false },
        { id: 'funding', label: '펀딩비율', live_supported: true },
        { id: 'ob_1s', label: 'USDT 1초 호가', live_supported: true },
    ],
    exchanges: {
        ob: Object.fromEntries(exchangeNames.map((ex) => [ex, ['BTC', 'USDT']])),
        tick: { bithumb: ['BTC'], upbit: ['BTC'] },
        funding: { binance: ['BTC'], okx: ['BTC'] },
        ob_1s: { bithumb: ['USDT'], upbit: ['USDT'] },
    },
};
let catalogFail = true,
    holdHistory = false,
    resolveHistory = null,
    holdLive = false,
    resolveLive = null,
    liveStep = 0,
    livePhase = 'normal',
    advance = false;
const calls = [];
const stats = {
    rows: 12,
    valid_pairs: 10,
    invalid_server_rows: 2,
    unplaced_desktop_rows: 1,
    negative_count: 3,
    above_threshold_count: 2,
    mean_ms: 700,
    p50_ms: 50,
    p95_ms: 2000,
    min_ms: -100,
    max_ms: 3000,
    latest_ms: 1500,
    latest_desktop_ms: origin + 121000,
    latest_server_ms: origin + 119500,
};
function historyResult(body) {
    return {
        mode: 'history',
        data_kind: body.data_kind,
        threshold_ms: body.threshold_ms,
        series: body.exchanges.map((ex, index) =>
            index < 2
                ? {
                      exchange: ex,
                      ticker: body.ticker || (index === 0 ? 'BTC' : 'ETH'),
                      status: 'ok',
                      times: index === 0 ? [origin, origin + 120000] : [origin + 60000],
                      mean_ms: index === 0 ? [-100, 1500] : [20],
                      p95_ms: index === 0 ? [-50, 2500] : [30],
                      max_ms: index === 0 ? [0, 3000] : [40],
                      min_ms: index === 0 ? [-150, 500] : [10],
                      count: index === 0 ? [4, 6] : [10],
                      negative_count: [3, 0],
                      above_threshold_count: [0, 2],
                      stats,
                  }
                : {
                      exchange: ex,
                      ticker: 'BTC',
                      status: 'no_data',
                      error: 'serverTime이 수집기에서 합성되어 측정에서 제외합니다.',
                      times: [origin],
                      mean_ms: [0],
                      p95_ms: [0],
                      max_ms: [0],
                      min_ms: [0],
                      stats: { ...stats, valid_pairs: 0, mean_ms: 0 },
                  },
        ),
        meta: {
            start: body.start,
            end: body.end,
            interval: body.interval,
            unit: 'ms',
            definition: 'desktopTime - serverTime',
            warnings: ['시계 차이와 처리 시간이 포함됩니다.'],
        },
    };
}
function liveResult(body) {
    if (advance) liveStep++;
    return {
        mode: 'live',
        data_kind: body.data_kind,
        observed_at_ms: origin + liveStep * 1000 + 200,
        threshold_ms: body.threshold_ms,
        series: body.exchanges.map((ex, index) => ({
            exchange: ex,
            ticker: body.ticker || 'BTC',
            status: livePhase === 'missing' ? 'no_data' : 'ok',
            ...(livePhase === 'missing' ? { error: '최신 자료가 없습니다.' } : {}),
            desktop_ms: origin + liveStep * 1000,
            server_ms: origin + liveStep * 1000 - 5,
            latency_ms: livePhase === 'missing' ? null : index === 0 ? -25 : 1200,
            age_ms: livePhase === 'future' ? -500 : livePhase === 'old' ? 15000 : 200,
            future_desktop: livePhase === 'future',
            above_threshold: index === 1,
            negative: index === 0,
            source: body.data_kind === 'ob_1s' ? 'parquet' : 'redis',
            ...(body.data_kind === 'ob_1s' ? { received_latency_ms: 40 } : {}),
        })),
        meta: { unit: 'ms', warnings: [] },
    };
}
async function api(path, options = {}) {
    calls.push({ path, ...options });
    if (path === '/api/quality/catalog') {
        if (catalogFail) {
            catalogFail = false;
            throw new Error('목록 연결 오류');
        }
        return catalog;
    }
    assert.equal(options.method, 'POST');
    if (path === '/api/quality/history') {
        const result = historyResult(options.body);
        if (holdHistory) {
            holdHistory = false;
            await new Promise((resolve) => {
                resolveHistory = resolve;
            });
        }
        return result;
    }
    if (path === '/api/quality/live') {
        const result = liveResult(options.body);
        if (holdLive) {
            holdLive = false;
            await new Promise((resolve) => {
                resolveLive = resolve;
            });
        }
        return result;
    }
    throw new Error(path);
}
const { initQuality } = await import('file://' + root + 'features/quality/controller.js');
const quality = initQuality({ api, getCatalog: () => ({}) });
await quality.ready;
await quality.refresh();
assert.equal(calls.length, 0, 'quality is lazy during boot and hidden refresh');
await quality.onEnter();
assert.equal($('quality-catalog-retry').hidden, false);
assert.ok($('quality-error').textContent.includes('목록 연결'));
$('quality-catalog-retry').click();
await until(() => !$('quality-run').disabled, 'catalog retry');
assert.equal(calls.filter((call) => call.path.endsWith('/catalog')).length, 2);
assert.equal($('quality-exchanges').querySelectorAll('input:checked').length, 8);
const ninth = $('quality-exchanges').querySelector('input[value="extra3"]');
ninth.checked = true;
ninth.dispatchEvent(new Event('change', { bubbles: true }));
assert.equal(ninth.checked, false);
assert.ok($('quality-error').textContent.includes('최대 8개'));
submit();
await until(
    () => document.querySelectorAll('.quality-source').length === 8 && !$('quality-run').disabled,
    'history result',
);
const posted = calls.find((call) => call.path.endsWith('/history')).body;
assert.equal(posted.ticker, '');
assert.equal(posted.max_points, 2000);
assert.ok(posted.start.endsWith('Z'));
assert.equal(posted.threshold_ms, 1000);
const compare = chartSnapshots.get('quality-chart');
assert.deepEqual(compare.times, [origin, origin + 60000, origin + 120000]);
assert.deepEqual(compare.series[0].values, [-100, null, 1500]);
assert.deepEqual(compare.series[1].values, [null, 20, null]);
assert.ok(compare.series[2].values.every((value) => value === null));
assert.ok($('quality-series').textContent.includes('Bithumb · BTC'));
assert.ok($('quality-series').textContent.includes('Upbit · ETH'));
assert.ok($('quality-series').textContent.includes('-100 ms'));
assert.ok($('quality-series').textContent.includes('합성'));
assert.ok(markers > 0, 'isolated source points are drawn without filling gaps');
assert.equal($('quality-warnings').hidden, false);
const excluded = document.querySelector('.quality-source[data-exchange="okx"]');
assert.ok(
    [...excluded.querySelectorAll('dt')].find((node) => node.textContent === '평균')
        .nextElementSibling.textContent === '—',
);
holdHistory = true;
submit();
await until(() => resolveHistory, 'held history');
$('quality-ticker').value = 'USDT';
input('quality-ticker');
resolveHistory();
resolveHistory = null;
await settle();
assert.equal(document.querySelectorAll('.quality-source').length, 0);
assert.equal(chartSnapshots.get('quality-chart').times.length, 0);
console.log(
    'PASS lazy catalog/retry, max eight exchanges, explicit KST→UTC request, per-source symbols/errors, signed means and sparse no-fill comparison, excluded clocks and late history suppression',
);

$('quality-mode').value = 'live';
change('quality-mode');
$('quality-kind').value = 'tick';
change('quality-kind');
assert.equal($('quality-mode').value, 'history');
assert.equal($('quality-mode').querySelector('[value="live"]').disabled, true);
$('quality-kind').value = 'ob_1s';
change('quality-kind');
$('quality-mode').value = 'live';
change('quality-mode');
assert.ok($('quality-definition').textContent.includes('약 5초'));
assert.ok($('quality-definition').textContent.includes('RTT'));
$('quality-ticker').value = '';
input('quality-ticker');
submit();
await until(
    () => timers.size === 1 && document.querySelectorAll('.quality-source').length === 2,
    'live first',
);
assert.equal(chartSnapshots.get('quality-chart').times.length, 1);
assert.ok(
    $('quality-series').textContent.includes('-25 ms'),
    'server latency is displayed unchanged rather than subtracting raw timestamps',
);
assert.ok(
    document.querySelector('.quality-source[data-exchange="upbit"]').classList.contains('above'),
);
assert.ok(
    document
        .querySelector('.quality-source[data-exchange="bithumb"]')
        .classList.contains('negative'),
);
assert.ok($('quality-series').textContent.includes('1초 수집 저장분'));
await tick();
assert.equal(
    chartSnapshots.get('quality-chart').times.length,
    1,
    'repeated original timestamp is not appended',
);
advance = true;
await tick();
assert.equal(chartSnapshots.get('quality-chart').times.length, 2);
livePhase = 'future';
await tick();
assert.ok($('quality-series').textContent.includes('미래 기록 · 시계 확인'));
livePhase = 'old';
await tick();
assert.ok($('quality-series').textContent.includes('10초 이상 오래'));
livePhase = 'missing';
await tick();
assert.ok($('quality-series').textContent.includes('최신 자료가 없습니다.'));
const missingCard = document.querySelector('.quality-source[data-exchange="bithumb"]');
assert.equal(
    [...missingCard.querySelectorAll('dt')].find((node) => node.textContent === '이번 응답의 차이')
        .nextElementSibling.textContent,
    '—',
);
livePhase = 'normal';
for (let i = 0; i < 905; i++) await tick();
assert.equal(chartSnapshots.get('quality-chart').times.length, 900);
liveStep += 1900;
await tick();
assert.equal(
    chartSnapshots.get('quality-chart').times.length,
    1,
    '30-minute window removes earlier samples',
);
holdLive = true;
const pending = tick();
await until(() => resolveLive, 'held live');
quality.onLeave();
resolveLive();
resolveLive = null;
await pending;
assert.equal(timers.size, 0);
assert.equal($('quality-stop').hidden, true);
const callsAfterLeave = calls.length;
await settle();
assert.equal(calls.length, callsAfterLeave);
await quality.onEnter();
assert.equal(timers.size, 0, 'tab return never automatically restarts monitor');
submit();
await until(() => timers.size === 1, 'restart');
$('quality-threshold').value = '500';
input('quality-threshold');
assert.equal(timers.size, 0);
assert.equal(chartSnapshots.get('quality-chart').times.length, 0);
submit();
await until(() => timers.size === 1, 'monitor before hidden');
Object.defineProperty(document, 'hidden', { configurable: true, value: true });
document.dispatchEvent(new Event('visibilitychange'));
assert.equal(timers.size, 0);
assert.ok($('quality-message').textContent.includes('숨겨져'));
quality.onLeave();
assert.deepEqual(errors, []);
console.log(
    'PASS live 2s polling, original timestamp dedup, server values and signed future/old ages, parquet labels, no-data current value, 900-point/30-minute bounds, setting/tab/document stop and late live suppression',
);
console.log('FINDINGS []');
process.exit(0);
