import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { JSDOM, FRONTEND } from '../support.mjs';
const dom = new JSDOM(await readFile(FRONTEND + 'index.html', 'utf8'), {
    url: 'http://localhost/',
    pretendToBeVisual: true,
});
for (const name of ['window', 'document', 'Event']) globalThis[name] = dom.window[name];
const { initStorage } = await import(FRONTEND + 'features/files/controller.js');
const $ = (id) => document.getElementById('storage-' + id),
    pause = () => new Promise((resolve) => setTimeout(resolve, 10));
const realTimeout = globalThis.setTimeout,
    timers = [];
globalThis.setTimeout = (fn, ms, ...args) => {
    const timer = realTimeout(fn, ms, ...args);
    if (ms >= 2000) {
        timers.push(timer);
        timer.unref();
    }
    return timer;
};
const policy = { enabled: false, days: 7, scopes: ['legacy', 'collector'] };
const disk = (scope, device, path) => ({
    scope,
    device,
    path,
    total: 100 * 1024 ** 3,
    free: 60 * 1024 ** 3,
    used_percent: 40,
    exists: true,
});
let state = {
    policy,
    disks: [disk('system', 1, '/'), disk('state', 1, '/state'), disk('legacy', 2, '/market')],
    busy: false,
    last: null,
};
const calls = [];
let refreshes = 0;
async function api(path, options = {}) {
    calls.push({ path, ...options });
    if (path === '/api/storage/policy') state = { ...state, policy: { ...options.body } };
    if (path === '/api/storage/preview')
        state = {
            ...state,
            last: {
                mode: 'preview',
                finished_ms: 1,
                policy: { ...options.body },
                count: 2,
                bytes: 2048,
                samples: [{ scope: 'legacy', path: 'old.parquet', bytes: 2048 }],
            },
        };
    return structuredClone(state);
}
try {
    initStorage({
        api,
        onCleaned: () => {
            refreshes++;
        },
    });
    await pause();
    assert.equal($('enabled').checked, false);
    assert.equal($('days').value, '7');
    assert.equal($('disks').children.length, 2, 'same disk is shown once');
    assert.match($('summary').textContent, /남음 60 GiB/);
    assert.equal(calls.length, 1);
    $('days').value = '14';
    $('collector').checked = false;
    $('preview').click();
    await pause();
    assert.deepEqual(calls.at(-1).body, { enabled: false, days: 14, scopes: ['legacy'] });
    assert.equal(calls.at(-1).path, '/api/storage/preview');
    assert.equal(state.policy.days, 7, 'preview must not save policy or delete');
    assert.match($('sample-list').textContent, /old.parquet/);
    $('enabled').checked = true;
    $('form').dispatchEvent(new Event('submit', { cancelable: true }));
    await pause();
    assert.equal(calls.at(-1).path, '/api/storage/policy');
    assert.equal(state.policy.days, 14);
    assert.equal(state.policy.enabled, true);
    state = {
        ...state,
        last: {
            ...state.last,
            mode: 'cleanup',
            finished_ms: 2,
            deleted: 1,
            deleted_bytes: 1024,
            skipped: 1,
            deferred: 'data_in_use',
        },
    };
    $('refresh').click();
    await pause();
    assert.equal(refreshes, 1);
    assert.match($('status').textContent, /다음 주기로/);
    $('refresh').click();
    await pause();
    assert.equal(refreshes, 1, 'same completed cleanup refreshes files once');
    const before = calls.length;
    $('legacy').checked = false;
    $('collector').checked = false;
    $('preview').click();
    await pause();
    assert.equal(calls.length, before);
    assert.match($('error').textContent, /하나 이상/);
    console.log(
        'Storage DOM: disk grouping/free space, disabled default, preview isolation, policy save, active-data deferral and empty-scope validation passed.',
    );
} finally {
    for (const timer of timers) clearTimeout(timer);
    globalThis.setTimeout = realTimeout;
    dom.window.close();
}
