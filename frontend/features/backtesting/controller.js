import { TimeChart } from '../../shared/charts.js';
import {
    $,
    exchanges,
    number,
    currency,
    price,
    time,
    errorAt,
    clearError,
    tableRows,
    source,
    range,
} from '../../shared/dom.js';
import { createHistoryControls } from '../../shared/source-controls.js';
import { createJobRunner } from '../../shared/jobs.js';

export function initBacktesting({ api, getCatalog }) {
    const state = { versions: { backtest: 0 }, backtest: null };
    const backtestChart = new TimeChart($('backtest-chart'), {
        format: (v) => number(v, 2),
        onHover: (t) => backtestChart.setCursor(t),
        onZoom: (r) => backtestChart.setZoom(r),
    });
    const job = createJobRunner({
        api,
        prefix: 'backtest',
        getVersion: () => state.versions.backtest,
    });
    function changed() {
        state.versions.backtest++;
        $('backtest-run').disabled = false;
        $('backtest-run').textContent = '백테스트 실행';
        $('backtest-message').textContent = '설정이 바뀌었습니다. 실행하면 새 조건을 적용합니다.';
    }
    async function runBacktest(event) {
        event.preventDefault();
        const version = ++state.versions.backtest;
        clearError('backtest');
        $('backtest-run').disabled = true;
        $('backtest-run').textContent = '계산 중…';
        try {
            const selection = source('backtest'),
                config = {
                    ...selection,
                    ...range('backtest'),
                    strategy: 'ma_cross',
                    fast_window: Number($('backtest-fast').value),
                    slow_window: Number($('backtest-slow').value),
                    initial_cash: Number($('backtest-cash').value),
                    fee_bps: Number($('backtest-fee').value),
                    slippage_bps: Number($('backtest-slippage').value),
                };
            if (
                !Number.isInteger(config.fast_window) ||
                !Number.isInteger(config.slow_window) ||
                config.fast_window < 1 ||
                config.fast_window >= config.slow_window
            )
                throw new Error('단기 이동평균 구간은 장기 구간보다 작은 양의 정수여야 합니다.');
            const response = await job('/api/backtests', config, version);
            if (!response) return;
            const r = response.result,
                m = r.metrics,
                unit = currency(selection.exchange);
            state.backtest = r;
            $('bt-return').textContent = number(m.return_pct, 2) + ' %';
            $('bt-final').textContent = `최종 평가액 ${number(m.final_equity, 2)} ${unit}`;
            $('bt-drawdown').textContent = number(m.max_drawdown_pct, 2) + ' %';
            $('bt-trades').textContent = number(m.closed_trades ?? m.trades, 0) + ' 회';
            $('bt-winrate').textContent = '승률 ' + number(m.win_rate_pct, 2) + ' %';
            $('bt-fees').textContent = number(m.fees_paid, 2);
            $('bt-currency').textContent = unit;
            const lines = [
                { label: '전략 자산', values: r.equity.values, color: '#5bdfb7', width: 1.8 },
            ];
            if (Array.isArray(r.equity.benchmark))
                lines.push({
                    label: '매수 후 보유',
                    values: r.equity.benchmark,
                    color: '#78b4e8',
                    dashed: true,
                });
            backtestChart.setZoom(null);
            backtestChart.setData(r.equity.times, lines);
            $('backtest-placeholder').hidden = r.equity.values.some(Number.isFinite);
            $('backtest-chart-label').textContent =
                `${exchanges[selection.exchange] || selection.exchange} ${selection.ticker} · ${unit} · ${config.fast_window}/${config.slow_window} 이동평균 · 민트: 전략${lines.length > 1 ? ' / 파랑: 매수 후 보유' : ''}`;
            const trades = r.trades || [];
            $('backtest-trade-count').textContent = number(trades.length, 0) + ' FILLS';
            tableRows(
                'backtest-trades',
                trades
                    .slice()
                    .reverse()
                    .map((trade) => [
                        time(trade.timestamp ?? trade.time),
                        trade.side === 'buy' ? '매수' : '매도',
                        price(trade.price),
                        number(trade.quantity, 8),
                        number(trade.fee, 4),
                        number(trade.realized_pnl, 4),
                        number(trade.realized_pnl_cumulative, 4),
                        number(trade.unrealized_pnl, 4),
                        number(trade.theory_unrealized_pnl, 4),
                        number(trade.total_pnl, 4),
                        number(trade.equity, 2),
                        price(trade.theory_price),
                    ]),
                12,
                '선택한 구간에 전략 체결이 없습니다.',
            );
            $('backtest-assumptions-list').replaceChildren();
            for (const text of r.assumptions || []) {
                const item = document.createElement('li');
                item.textContent = text;
                $('backtest-assumptions-list').append(item);
            }
            $('backtest-assumptions').hidden = !(r.assumptions || []).length;
            $('backtest-message').textContent =
                `백테스트 완료 · ${number(m.trades ?? trades.length, 0)}회 체결 · 수수료 ${config.fee_bps}bp / 슬리피지 ${config.slippage_bps}bp 적용`;
        } catch (error) {
            if (version === state.versions.backtest) {
                errorAt('backtest', error);
                $('backtest-message').textContent = '백테스트 조건을 확인해주세요.';
            }
        } finally {
            if (version === state.versions.backtest) {
                $('backtest-run').disabled = false;
                $('backtest-run').textContent = '백테스트 실행';
            }
        }
    }

    $('backtest-form').addEventListener('submit', runBacktest);
    $('backtest-reset-zoom').addEventListener('click', () => backtestChart.setZoom(null));
    const controls = createHistoryControls({
        prefix: 'backtest',
        api,
        getCatalog,
        onChanged: changed,
    });
    return {
        ready: controls.ready,
        refresh: controls.refresh,
        onEnter() {
            backtestChart.draw();
        },
    };
}
