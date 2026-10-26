import {
    $,
    exchanges,
    number,
    price,
    stamp,
    time,
    errorAt,
    clearError,
    tableRows,
    source,
    pnlCurrency,
    pnlAccount,
    pnlFill,
    fxDescription,
    pnlTheoryPrice,
    fillNotional,
    renderTradeActivity,
} from '../../shared/dom.js';
import { populateSource, populateTickers } from '../../shared/source-controls.js';

export function initPaperTrading({ api, getCatalog }) {
    const state = {
        paperLoaded: false,
        paperBusy: false,
        paperVersion: 0,
        fills: [],
        nextFill: null,
        snapshot: null,
        loading: 0,
        paging: false,
        visible: false,
        timer: null,
    };
    let unit = pnlCurrency('paper');
    $('paper-pnl-currency').value = unit;
    const paperExchanges = new Set(['bithumb', 'upbit']);
    function renderPaper(snapshot, preserveFills = false) {
        const oldCount = state.snapshot?.fill_count;
        state.snapshot = snapshot;
        const account = pnlAccount(snapshot, unit),
            base = snapshot.account || {},
            orders = snapshot.orders || [];
        const positions =
            unit === 'USDT'
                ? snapshot.usdt?.positions ||
                  (snapshot.positions || []).map((p) => ({
                      ...p,
                      avg_price: null,
                      mark_price: null,
                      market_value: null,
                      unrealized_pnl: null,
                      theory_price: null,
                      theory_unrealized_pnl: null,
                  }))
                : snapshot.positions || [];
        const allMarked = positions.every((position) => Number.isFinite(position.market_value));
        const equity =
            unit === 'USDT'
                ? account.equity
                : Object.hasOwn(snapshot, 'equity')
                  ? snapshot.equity
                  : Number.isFinite(account.equity)
                    ? account.equity
                    : allMarked && Number.isFinite(account.cash)
                      ? account.cash +
                        positions.reduce((sum, position) => sum + position.market_value, 0)
                      : null;
        const digits = unit === 'USDT' ? 6 : 2;
        const unrealized =
            unit === 'USDT'
                ? account.unrealized_pnl
                : positions.every((position) => Number.isFinite(position.unrealized_pnl))
                  ? positions.reduce((sum, position) => sum + position.unrealized_pnl, 0)
                  : null;
        $('paper-equity').textContent = number(equity, digits);
        $('paper-cash').textContent = number(account.cash, digits);
        $('paper-unit').textContent = unit + ' · 모의 자산';
        $('paper-initial').textContent =
            '초기 자금 ' + number(account.initial_cash, digits) + ' ' + unit;
        $('paper-realized').textContent = number(account.realized_pnl, digits);
        $('paper-unrealized').textContent = number(unrealized, digits);
        $('paper-theory-unrealized').textContent = number(account.theory_unrealized_pnl, digits);
        $('paper-total-pnl').textContent = number(account.total_pnl, digits);
        $('paper-theory-total').textContent = number(account.theory_total_pnl, digits);
        $('paper-position-count').textContent = number(positions.length, 0) + ' POSITIONS';
        $('paper-order-count').textContent =
            number(snapshot.fill_count ?? orders.length, 0) + ' FILLS';
        tableRows(
            'paper-positions',
            positions.map((position) => [
                `${exchanges[position.exchange] || position.exchange} / ${position.ticker}`,
                number(position.quantity, 8),
                price(position.avg_price),
                price(position.mark_price),
                number(position.market_value, digits),
                number(position.unrealized_pnl, digits),
                price(position.theory_price),
                number(position.theory_unrealized_pnl, digits),
                position.quote_time ? time(position.quote_time) + ' KST' : '시세 확인 불가',
            ]),
            9,
            '보유한 모의 포지션이 없습니다.',
        );
        if (!preserveFills || oldCount !== snapshot.fill_count) {
            state.fills =
                snapshot.fills ||
                orders.map((fill) => ({
                    fill,
                    account: null,
                    valuation_status: 'legacy_unavailable',
                }));
            state.nextFill = snapshot.next_fill_before ?? null;
        }
        renderFills();
        $('paper-fx-info').textContent = fxDescription(snapshot, unit);
        renderTradeActivity('paper', snapshot, unit);
        $('paper-fx-pnl').textContent =
            unit === 'USDT'
                ? `원화 현금 환산손익 ${number(account.cash_fx_pnl, 6)} USDT · 초기 대비 평가액 변화 ${number(account.equity_change_pnl, 6)} USDT`
                : '';
        const quote = snapshot.quote_status || {};
        $('paper-message').textContent =
            (quote.message || '서버 모의 계좌 조회 완료') +
            (quote.updated_at ? ' · 확인 ' + time(quote.updated_at) + ' KST' : '');
        $('paper-message').classList.toggle(
            'quote-unavailable',
            ['partial', 'unavailable'].includes(quote.state),
        );
        $('paper-reset-currency').value = 'KRW';
        $('paper-order-note').textContent =
            `KRW 현물형 모의 계좌 · 수수료 ${number(base.fee_bps, 2)}bp / 슬리피지 ${number(base.slippage_bps, 2)}bp. 주문 시 서버가 새 호가를 확인합니다.`;
        state.paperLoaded = true;
    }
    function renderFills() {
        tableRows(
            'paper-orders',
            state.fills.map((report) => {
                const f = report.fill,
                    value = pnlFill(report, unit),
                    a = pnlAccount(report, unit),
                    position = report.positions?.find(
                        (p) => p.exchange === f.exchange && p.ticker === f.ticker,
                    );
                return [
                    time(f.timestamp),
                    `${exchanges[f.exchange] || f.exchange} / ${f.ticker}`,
                    f.side === 'buy' ? '매수' : '매도',
                    number(f.quantity, 8),
                    price(value.price),
                    number(value.fee, 6),
                    number(value.realized_pnl, 6),
                    number(a.realized_pnl, 4),
                    number(a.unrealized_pnl, 4),
                    number(a.theory_unrealized_pnl, 4),
                    number(a.total_pnl, 4),
                    number(a.theory_total_pnl, 4),
                    number(a.cash ?? (unit === 'KRW' ? f.cash : null), 2),
                    number(a.equity, 2),
                    number(position?.quantity ?? (report.account ? 0 : null), 8),
                    price(pnlTheoryPrice(report, unit)),
                    unit === 'USDT' && report.usdt?.status !== 'complete'
                        ? '환율·원가 미평가'
                        : report.valuation_status === 'legacy_unavailable'
                          ? '과거 평가 기록 없음'
                          : a.unrealized_pnl === null
                            ? '시장가 평가 불가'
                            : a.theory_unrealized_pnl === null
                              ? '이론가 평가 불가'
                              : '기록됨',
                    number(report.usdt?.fill?.fx?.krw_per_usdt, 4),
                    number(fillNotional(report, unit), unit === 'USDT' ? 6 : 2),
                ];
            }),
            19,
            '아직 모의 체결이 없습니다.',
        );
        $('paper-fills-more').hidden = state.nextFill === null;
    }
    async function loadPaper({ clear = true, preserveFills = false } = {}) {
        state.loading++;
        const version = ++state.paperVersion;
        if (clear) clearError('paper');
        $('paper-refresh').disabled = true;
        try {
            const snapshot = await api('/api/paper');
            if (version === state.paperVersion) renderPaper(snapshot, preserveFills);
        } catch (error) {
            if (version === state.paperVersion) {
                errorAt('paper', error);
                $('paper-message').textContent =
                    '최신 잔고와 시세를 확인하지 못했습니다. 새로고침해주세요.';
            }
        } finally {
            state.loading--;
            if (version === state.paperVersion) $('paper-refresh').disabled = state.paperBusy;
        }
    }
    function paperBusy(value) {
        state.paperBusy = value;
        if (value) state.paperVersion++;
        for (const id of ['paper-submit', 'paper-reset', 'paper-refresh']) $(id).disabled = value;
    }
    async function orderPaper(event) {
        event.preventDefault();
        if (state.paperBusy) return;
        clearError('paper');
        paperBusy(true);
        try {
            const quantity = Number($('paper-quantity').value);
            if (!Number.isFinite(quantity) || quantity <= 0)
                throw new Error('0보다 큰 주문 수량을 입력해주세요.');
            await api('/api/paper/orders', {
                method: 'POST',
                body: { ...source('paper'), side: $('paper-side').value, quantity },
            });
            await loadPaper({ clear: false });
            $('paper-quantity').value = '';
        } catch (error) {
            errorAt('paper', error);
        } finally {
            paperBusy(false);
        }
    }
    async function resetPaper(event) {
        event.preventDefault();
        if (state.paperBusy) return;
        clearError('paper');
        if (!$('paper-reset-confirm').checked) {
            errorAt('paper', new Error('초기화 확인 항목을 선택해주세요.'));
            return;
        }
        paperBusy(true);
        try {
            const initial_cash = Number($('paper-reset-cash').value);
            if (!Number.isFinite(initial_cash) || initial_cash <= 0)
                throw new Error('초기 자금은 0보다 커야 합니다.');
            await api('/api/paper/reset', {
                method: 'POST',
                body: { initial_cash, quote_currency: $('paper-reset-currency').value },
            });
            $('paper-reset-confirm').checked = false;
            await loadPaper({ clear: false });
        } catch (error) {
            errorAt('paper', error);
        } finally {
            paperBusy(false);
        }
    }

    $('paper-fills-more').addEventListener('click', async () => {
        const version = state.paperVersion,
            before = state.nextFill;
        if (before === null || state.paging) return;
        state.paging = true;
        $('paper-fills-more').disabled = true;
        try {
            const result = await api(`/api/paper/fills?before=${before}`);
            if (version !== state.paperVersion) return;
            state.fills.push(...result.items);
            state.nextFill = result.next_before;
            renderFills();
        } catch (error) {
            errorAt('paper', error);
        } finally {
            state.paging = false;
            $('paper-fills-more').disabled = false;
        }
    });
    $('paper-fills-csv').addEventListener('click', async () => {
        try {
            const blob = await api('/api/paper/fills/csv', { responseType: 'blob' }),
                url = URL.createObjectURL(blob),
                a = document.createElement('a');
            a.href = url;
            a.download = 'paper-fills.csv';
            a.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch (error) {
            errorAt('paper', error);
        }
    });
    function populate() {
        populateSource('paper', getCatalog, { allowedExchanges: paperExchanges });
    }
    $('paper-exchange').addEventListener('change', () =>
        populateTickers('paper', getCatalog, true),
    );
    $('paper-ticker').addEventListener(
        'change',
        () => ($('paper-ticker').value = $('paper-ticker').value.trim().toUpperCase()),
    );
    $('paper-side').addEventListener(
        'change',
        () =>
            ($('paper-submit').textContent =
                $('paper-side').value === 'buy' ? '모의 매수 주문' : '모의 매도 주문'),
    );
    $('paper-refresh').addEventListener('click', () => {
        if (!state.paperBusy) loadPaper();
    });
    $('paper-order-form').addEventListener('submit', orderPaper);
    $('paper-reset-form').addEventListener('submit', resetPaper);
    $('paper-pnl-currency').addEventListener('change', () => {
        unit = pnlCurrency('paper', $('paper-pnl-currency').value);
        if (state.snapshot) renderPaper(state.snapshot, true);
    });
    populate();
    return {
        refresh: populate,
        onEnter() {
            state.visible = true;
            loadPaper();
            clearInterval(state.timer);
            state.timer = setInterval(() => {
                if (
                    state.visible &&
                    !document.hidden &&
                    !state.paperBusy &&
                    !state.loading &&
                    !state.paging
                )
                    loadPaper({ clear: false, preserveFills: true });
            }, 2000);
        },
        onLeave() {
            state.visible = false;
            clearInterval(state.timer);
            state.paperVersion++;
        },
    };
}
