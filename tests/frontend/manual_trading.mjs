// Run with jsdom installed, or OMS_JSDOM_MODULE=/absolute/path/to/jsdom/lib/api.js.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const { JSDOM } = await import(process.env.OMS_JSDOM_MODULE || 'jsdom');
const dom = new JSDOM(
    await readFile(new URL('../../frontend/index.html', import.meta.url), 'utf8'),
    {
        url: 'http://localhost/#manual',
        pretendToBeVisual: true,
    },
);
for (const name of ['window', 'document', 'Option', 'Event']) globalThis[name] = dom.window[name];
const { initManualTrading, comparePrice } = await import(
    '../../frontend/features/manual_trading/controller.js'
);
const $ = (id) => document.getElementById('manual-' + id);
const flush = () => new Promise((resolve) => setTimeout(resolve, 10));
const submit = (id) =>
    $(id).dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
const change = (id) => $(id).dispatchEvent(new Event('change'));
const requests = [];
const snapshot = {
    keys: [],
    orders: [],
    market: {
        exchange: 'bithumb',
        ticker: 'BTC',
        status: 'connected',
        can_order: true,
        age_ms: 50,
        ticks: [],
        book: { bids: [['100', '2']], asks: [['101', '3']] },
    },
};
let resultStatus = 'accepted',
    preflightFailure = false,
    networkFailure = false;
async function api(path, options = {}) {
    requests.push({ path, options });
    if (path.startsWith('/api/manual?')) return structuredClone(snapshot);
    if (path === '/api/manual/keys') {
        snapshot.keys.push({
            id: 'key1',
            exchange: 'bithumb',
            label: 'Test',
            key_hint: '••••1234',
            verified_ms: null,
        });
        return snapshot.keys[0];
    }
    if (path.endsWith('/verify')) {
        snapshot.keys[0].verified_ms = Date.now();
        return { balances: [{ currency: 'KRW', available: '1000', reserved: '0' }] };
    }
    if (path === '/api/manual/market') return { status: 'connecting' };
    if (path === '/api/manual/orders') {
        if (preflightFailure) {
            const error = new Error('시세 지연');
            error.data = { order_not_submitted: true };
            throw error;
        }
        if (networkFailure) throw new Error('network disconnected');
        const body = options.body;
        const record = {
            ...body,
            status: resultStatus,
            filled: null,
            remaining: body.quantity,
            created_ms: Date.now(),
            updated_ms: Date.now(),
        };
        if (!snapshot.orders.some((order) => order.client_order_id === body.client_order_id))
            snapshot.orders.unshift(record);
        return record;
    }
    const order = snapshot.orders.find((order) => path.includes(order.client_order_id));
    if (path.endsWith('/refresh'))
        Object.assign(order, { status: 'partial', filled: '0.2', remaining: '0.8' });
    if (path.endsWith('/cancel')) order.status = 'cancel_requested';
    return structuredClone(order);
}
const { initManualFeature } = await import('../../frontend/shell/workspace.js');
const view = initManualTrading({ api, allowDirectTrading: true });
try {
    assert.equal(requests.length, 0, 'creating hidden module must not connect or use credentials');
    assert.equal($('submit').disabled, true);
    assert.equal(comparePrice('999999999999999999.1', '999999999999999999.2'), -1);
    view.onEnter();
    await flush();
    assert.equal($('book').rows.length, 2);
    assert.equal($('submit').disabled, true, 'public feed does not unlock trading');
    submit('connect-form');
    await flush();
    assert.equal(requests.filter((r) => r.path === '/api/manual/market').length, 1);
    $('api-key').value = 'fake-access-key-1234';
    $('secret').value = 'fake-secret-1234';
    submit('key-form');
    await flush();
    assert.equal($('api-key').value, '');
    assert.equal($('secret').value, '');
    assert.equal(window.localStorage.length, 0);
    assert.equal(window.sessionStorage.length, 0);
    $('keys').querySelector('button').click();
    await flush();
    assert.equal($('submit').disabled, true, 'verified key still requires explicit confirmation');
    $('price').value = '100';
    $('quantity').value = '1';
    $('confirm').checked = true;
    change('confirm');
    assert.equal($('submit').disabled, false);
    $('book').querySelector('button').click();
    assert.equal($('price').value, '101');
    assert.equal($('confirm').checked, false);
    assert.equal(
        requests.filter((r) => r.path === '/api/manual/orders').length,
        0,
        'clicking price must not trade',
    );
    snapshot.market.can_order = false;
    $('refresh').click();
    await flush();
    $('confirm').checked = true;
    change('confirm');
    assert.equal($('submit').disabled, true);
    submit('order-form');
    assert.equal(requests.filter((r) => r.path === '/api/manual/orders').length, 0);
    snapshot.market.can_order = true;
    $('refresh').click();
    await flush();
    preflightFailure = true;
    $('confirm').checked = true;
    change('confirm');
    submit('order-form');
    await flush();
    assert.match($('error').textContent, /시세 지연/);
    preflightFailure = false;
    $('price').value = '99';
    $('confirm').checked = true;
    change('confirm');
    submit('order-form');
    submit('order-form');
    await flush();
    assert.equal(snapshot.orders.length, 1, 'double submit produces one order');
    assert.equal($('confirm').checked, false);
    assert.match($('orders').textContent, /접수·체결 확인 필요/);
    assert.equal($('outside').hidden, false);
    assert.match($('outside-rows').textContent, /99/);
    $('orders').querySelector('button').click();
    await flush();
    assert.match($('orders').textContent, /부분체결/);
    $('orders').querySelectorAll('button')[1].click();
    await flush();
    assert.match($('orders').textContent, /취소 요청·확인 필요/);
    // A lost response may only retry the identical intent with the identical ID.
    networkFailure = true;
    $('price').value = '98';
    $('confirm').checked = true;
    change('confirm');
    submit('order-form');
    await flush();
    const lost = requests.filter((r) => r.path === '/api/manual/orders').at(-1).options
        .body.client_order_id;
    networkFailure = false;
    resultStatus = 'unknown';
    $('confirm').checked = true;
    change('confirm');
    submit('order-form');
    await flush();
    assert.equal(snapshot.orders[0].client_order_id, lost);
    $('confirm').checked = true;
    change('confirm');
    assert.equal($('submit').disabled, true, 'unresolved acceptance blocks new orders');
    $('orders').querySelector('button').click();
    await flush();
    $('confirm').checked = true;
    change('confirm');
    assert.equal($('submit').disabled, false);
    $('key-exchange').value = 'okx';
    change('key-exchange');
    assert.equal($('passphrase').required, true);
    $('key-exchange').value = 'bithumb';
    change('key-exchange');
    assert.equal($('passphrase').disabled, true);
    view.onLeave();
    assert.equal($('confirm').checked, false);
    const before = requests.length;
    await initManualFeature({ api, session: { capabilities: {} } });
    assert.equal(requests.length, before, 'old server fallback performs no requests');
    assert.match($('error').textContent, /서버를 재시작/);
    assert.equal($('key-save').disabled, true);
    console.log(
        'Manual trading DOM: public-only default, credential clearing, explicit orders, stale gate, double-submit, unknown reconciliation, cancel ACK and out-of-depth overlay passed.',
    );
} finally {
    view.onLeave();
    dom.window.close();
}

// Default deployment can view public books but cannot enter credentials or trade.
const remoteDom = new JSDOM(
    await readFile(new URL('../../frontend/index.html', import.meta.url), 'utf8'),
    { url: 'http://localhost/#manual', pretendToBeVisual: true },
);
for (const name of ['window', 'document', 'Option', 'Event'])
    globalThis[name] = remoteDom.window[name];
const remoteRequests = [];
const remoteView = await initManualFeature({
    session: { capabilities: { manual_market_view: true, manual_spot_orders: false } },
    api: async (path, options = {}) => {
        remoteRequests.push({ path, options });
        return structuredClone(snapshot);
    },
});
try {
    remoteView.onEnter();
    await flush();
    assert.equal($('submit').disabled, true);
    assert.equal($('key-save').disabled, true);
    assert.equal(document.querySelector('#module-manual .manual-key-panel').hidden, true);
    assert.match($('order-status').textContent, /별도 실행 서버/);
    assert.equal($('book').rows.length, 2);
    submit('connect-form');
    await flush();
    assert.equal(remoteRequests.filter((r) => r.options.method === 'POST').length, 1);
    assert.equal(
        remoteRequests.find((r) => r.options.method === 'POST').path,
        '/api/manual/market',
    );
    assert.equal($('confirm').disabled, true);
    console.log(
        'Remote default DOM: public-only subscription, hidden credentials and disabled private controls passed.',
    );
} finally {
    remoteView.onLeave();
    remoteDom.window.close();
}
