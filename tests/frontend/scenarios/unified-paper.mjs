import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { JSDOM, FRONTEND } from '../support.mjs';
const html = await readFile(FRONTEND + 'index.html', 'utf8');
const { initUnifiedPaper, initManualFeature } = await import(FRONTEND + 'shell/workspace.js');
const { initTabs } = await import(FRONTEND + 'shell/tabs.js');
for (const [hash, initial] of [
    ['#paper', 'historical'],
    ['#replay', 'historical'],
    ['#manual', 'live'],
    ['#paper-live', 'live'],
]) {
    const dom = new JSDOM(html, { url: 'http://localhost/' + hash, pretendToBeVisual: true });
    for (const name of [
        'window',
        'document',
        'Option',
        'Event',
        'CustomEvent',
        'location',
        'history',
    ])
        globalThis[name] = dom.window[name];
    const $ = (id) => document.getElementById(id),
        calls = [];
    const fake = (name) => ({
        onEnter() {
            calls.push(name + ':enter');
        },
        onLeave() {
            calls.push(name + ':leave');
        },
    });
    try {
        assert.equal(document.querySelectorAll('[role="tab"]').length, 7);
        assert.equal(document.querySelectorAll('[role="tabpanel"]').length, 7);
        assert.equal($('tab-replay'), null);
        assert.equal($('tab-manual'), null);
        assert.ok($('module-paper').contains($('module-replay')));
        assert.ok($('paper-live').contains($('module-manual')));
        assert.equal($('module-manual').closest('main').id, 'module-paper');
        const paper = initUnifiedPaper({
            replay: fake('past'),
            live: fake('live'),
            market: fake('market'),
        });
        initTabs({
            modules: { paper },
            stopLive: async () => {
                calls.push('analysis:stop');
            },
        });
        assert.equal($('tab-paper').getAttribute('aria-selected'), 'true');
        assert.equal($('module-paper').hidden, false);
        assert.equal($('paper-data-mode').value, initial);
        assert.equal($('module-replay').hidden, initial !== 'historical');
        assert.equal($('paper-live').hidden, initial !== 'live');
        assert.equal(location.hash, initial === 'live' ? '#paper-live' : '#paper');
        $('replay-cash').value = '123456';
        $('paper-quantity').value = '0.25';
        const next = initial === 'historical' ? 'live' : 'historical';
        calls.length = 0;
        $('paper-data-mode').value = next;
        $('paper-data-mode').dispatchEvent(new Event('change'));
        assert.deepEqual(
            calls,
            initial === 'historical'
                ? ['past:leave', 'live:enter', 'market:enter']
                : ['live:leave', 'market:leave', 'past:enter'],
        );
        assert.equal($('replay-cash').value, '123456');
        assert.equal($('paper-quantity').value, '0.25');
        calls.length = 0;
        $('tab-analysis').click();
        assert.deepEqual(
            calls,
            next === 'historical' ? ['past:leave'] : ['live:leave', 'market:leave'],
        );
        calls.length = 0;
        document.dispatchEvent(new CustomEvent('oms:paper-mode', { detail: 'historical' }));
        $('tab-paper').click();
        assert.equal($('paper-data-mode').value, 'historical');
        assert.equal($('module-replay').hidden, false);
        assert.deepEqual(calls, ['analysis:stop', 'past:enter']);
        calls.length = 0;
        $('tab-paper').click();
        assert.deepEqual(calls, [], 'same tab does not duplicate polling');
        // Even a legacy direct-trading capability must never enable private controls in paper UI.
        let requests = 0;
        await initManualFeature({
            session: { capabilities: { manual_spot_orders: true } },
            api: async () => {
                requests++;
            },
        });
        assert.equal(requests, 0);
        assert.equal($('manual-order-form').hidden, true);
        assert.equal($('manual-private-orders').hidden, true);
        assert.equal($('manual-submit').disabled, true);
        assert.equal(document.querySelector('#module-manual .manual-key-panel').hidden, true);
        paper.onLeave();
    } finally {
        dom.window.close();
    }
}
console.log(
    'Unified paper: one tab, historical/live modes, preserved inputs, legacy links, strategy navigation, polling lifecycle and no real-order controls passed.',
);
