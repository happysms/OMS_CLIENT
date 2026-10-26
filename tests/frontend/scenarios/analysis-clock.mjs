import { JSDOM, ROOT, FRONTEND, loadStrategyCatalog } from '../support.mjs';
import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
const root = FRONTEND;
const { window } = new JSDOM(await fs.readFile(root + 'index.html', 'utf8'), {
    url: 'http://localhost/',
    pretendToBeVisual: true,
});
for (const name of ['window', 'document', 'Option', 'localStorage', 'Event'])
    globalThis[name] = window[name];
globalThis.ResizeObserver = class {
    observe() {}
    disconnect() {}
};
window.HTMLCanvasElement.prototype.getContext = () =>
    new Proxy({}, { get: (t, k) => t[k] || (() => {}), set: (t, k, v) => ((t[k] = v), true) });
window.HTMLCanvasElement.prototype.getBoundingClientRect = () => ({
    width: 700,
    height: 230,
    left: 0,
    top: 0,
});
window.HTMLCanvasElement.prototype.setPointerCapture = () => {};
const { TimeChart } = await import(root + 'shared/charts.js');
const charts = new Map(),
    original = TimeChart.prototype.setData;
TimeChart.prototype.setData = function (...args) {
    charts.set(this.canvas.id || this.canvas.closest('article')?.dataset.id, this);
    return original.apply(this, args);
};
const $ = (id) => document.getElementById(id),
    now = 1792886400000,
    times = [now, now + 60000, now + 120000];
const legs = [
    { id: 'A', exchange: 'bithumb', ticker: 'BTC' },
    { id: 'B', exchange: 'okx', ticker: 'BTC' },
];
const defaults = {
    legs,
    formula: 'A / B',
    unit: 'ratio',
    start: new Date(now).toISOString(),
    end: new Date(now + 180000).toISOString(),
    interval: '1min',
    price_mode: 'last',
    ewm_span: 1,
};
const catalog = { exchanges: { bithumb: ['BTC'], okx: ['BTC'] }, defaults };
let mode = 'last',
    variant = 'normal';
const result = () => ({
    times,
    result: [1, 1, 1],
    ewm: [1, 1, 1],
    mean: 1,
    stats: { count: 3, latest: 1, mean: 1, min: 1, max: 1, std: 0 },
    legs: legs.map((leg, i) => ({
        ...leg,
        mid: [101, 101, 101],
        bid: [100, 100, 100],
        ask: [102, 102, 102],
        quote_times: times,
        ...(variant === 'old'
            ? {}
            : {
                  timestamp_delta_ms:
                      variant === 'empty' ? [null, null, null] : i ? [50, 40, 30] : [10, null, -20],
              }),
    })),
    meta: {
        formula: 'A / B',
        interval: '1min',
        price_mode: mode,
        aligned_points: 3,
        rows_read: 6,
        warnings: [],
    },
});
async function api(path, options = {}) {
    if (path === '/api/presets' || path === '/api/history') return { items: [] };
    if (path.startsWith('/api/bounds?')) return { start: defaults.start, end: defaults.end };
    if (path === '/api/analyses') {
        mode = options.body.price_mode;
        return { id: 'test' };
    }
    if (path === '/api/analyses/test') return { status: 'complete', result: result() };
    throw new Error(path);
}
const { initAnalysis } = await import(root + 'features/analysis/controller.js');
const controller = initAnalysis({ api, session: {}, catalog, onCatalog() {} });
await controller.prepare();
await controller.runHistory();
assert.equal($('error').hidden, true, $('error').textContent);
const clock = charts.get('analysis-clock-chart');
assert.deepEqual(
    clock.series.map((l) => l.values),
    [
        [10, null, -20],
        [50, 40, 30],
    ],
);
assert.equal($('analysis-clock-empty').hidden, true);
assert.match($('analysis-clock-legend').textContent, /-20 ms/);
assert.match($('analysis-clock-mode').textContent, /마지막 원본/);
clock.onZoom([times[0], times[1]]);
assert.deepEqual(charts.get('result-chart').zoom, clock.zoom);
assert.deepEqual(charts.get('A').zoom, clock.zoom);
clock.onZoom(null);
clock.onHover(times[2]);
assert.equal(charts.get('result-chart').cursor, times[2]);
assert.match(clock.tooltip.textContent, /-20 ms/);
$('table-details').open = true;
$('table-details').dispatchEvent(new window.Event('toggle'));
assert.match($('table-head').textContent, /desk − server \(ms\)/);
assert.match($('table-body').textContent, /-20/);
$('analysis-price-mode').value = 'mean';
$('analysis-price-mode').dispatchEvent(new window.Event('change'));
assert.equal(clock.times.length, 0);
assert.equal($('analysis-clock-empty').hidden, false);
await controller.runHistory();
assert.match($('analysis-clock-mode').textContent, /시간차 평균/);
variant = 'empty';
await controller.runHistory();
assert.equal($('analysis-clock-empty').hidden, false);
variant = 'old';
await controller.runHistory();
assert.match($('analysis-clock-legend').textContent, /다시 실행/);
assert.equal($('error').hidden, true, $('error').textContent);
console.log(
    'Analysis clock DOM checks passed: signed/null values, legends, linked cursor/zoom, table, mean mode, reset, old results.',
);
window.close();
