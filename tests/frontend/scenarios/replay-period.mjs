import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { JSDOM, ROOT, FRONTEND, loadStrategyCatalog } from '../support.mjs';
const root = ROOT;
const dom = new JSDOM(await readFile(root + '/frontend/index.html', 'utf8'), {
    url: 'http://localhost/#replay',
});
for (const name of ['window', 'document', 'Option', 'Event', 'CustomEvent', 'HTMLElement'])
    globalThis[name] = dom.window[name];
const { initReplay } = await import(root + '/frontend/features/replay/controller.js');
const catalog = await loadStrategyCatalog();
const calls = [];
let resolveBounds;
async function api(path, options = {}) {
    calls.push({ path, ...options });
    if (path === '/api/strategies') return structuredClone(catalog);
    if (path === '/api/replays' && options.method !== 'POST') return { items: [] };
    if (path.startsWith('/api/bounds?'))
        return new Promise((resolve) => {
            resolveBounds = resolve;
        });
    if (path === '/api/replays' && options.method === 'POST')
        throw new Error('Captured request only');
    throw new Error('Unexpected ' + path);
}
const controller = initReplay({
    api,
    getCatalog: () => ({ exchanges: { bithumb: ['BTC', 'ETH'] } }),
});
const $ = (id) => document.getElementById('replay-' + id),
    flush = () => new Promise((resolve) => setTimeout(resolve, 10));
const bounds = { start: '2026-10-20T00:00:00+09:00', end: '2026-10-27T12:30:00+09:00' };
const input = (id, value) => {
    $(id).value = value;
    $(id).dispatchEvent(new Event('input', { bubbles: true }));
};
const submit = () =>
    $('form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
try {
    controller.onEnter();
    await flush();
    assert.match($('period-preview').textContent, /모두 입력/);
    input('start', '2026-10-21T01:00');
    input('end', '2026-10-21T03:00');
    assert.match($('period-preview').textContent, /총 7,200초/);
    $('ticker').value = 'BTC';
    $('ticker').dispatchEvent(new Event('change'));
    resolveBounds(bounds);
    await flush();
    assert.equal($('start').value, '2026-10-21T01:00');
    assert.equal($('end').value, '2026-10-21T03:00');
    $('latest').click();
    resolveBounds(bounds);
    await flush();
    assert.equal($('start').value, '2026-10-27T11:59');
    assert.equal($('end').value, '2026-10-27T12:00');
    assert.match($('period-preview').textContent, /총 60초/);
    $('latest').click();
    input('start', '2026-10-22T01:00');
    input('end', '2026-10-22T02:00');
    resolveBounds(bounds);
    await flush();
    assert.equal($('start').value, '2026-10-22T01:00', 'late bounds must not replace manual edit');
    input('end', '2026-10-22T00:00');
    submit();
    await flush();
    assert.match($('error').textContent, /늦어야/);
    assert.equal(calls.filter((c) => c.method === 'POST').length, 0);
    input('end', '2026-10-30T01:00');
    submit();
    await flush();
    assert.match($('error').textContent, /7일/);
    input('end', '');
    submit();
    await flush();
    assert.match($('error').textContent, /모두 입력/);
    input('end', '2026-10-22T02:00');
    assert.equal($('end').validationMessage, '');
    submit();
    await flush();
    const request = calls.find((c) => c.method === 'POST');
    assert.equal(request.body.start, '2026-10-22T01:00');
    assert.equal(request.body.end, '2026-10-22T02:00');
    input('start', '');
    input('end', '');
    $('ticker').value = 'ETH';
    $('ticker').dispatchEvent(new Event('change'));
    resolveBounds(bounds);
    await flush();
    assert.equal($('start').value, '2026-10-27T11:59');
    assert.match($('period-preview').textContent, /총 60초/);
    console.log(
        'Period DOM checks passed: input, preview, validation, exact request, manual range preservation, explicit latest, async race, empty defaults.',
    );
} finally {
    controller.onLeave();
    dom.window.close();
}
