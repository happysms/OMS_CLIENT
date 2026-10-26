import { TimeChart } from '../../shared/charts.js';

const HOUR = 3600000;
const COMPARISON_LIMIT = 10;
const COMPARISON_COLORS = [
    '#5bdfb7',
    '#78b4e8',
    '#f1b66e',
    '#b8a1ed',
    '#f48da7',
    '#94cf77',
    '#e5d36b',
    '#d49b72',
    '#7dd4d9',
    '#d49de0',
];
const comparisonNumber = new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 5 });
const comparisonTime = (value) =>
    new Date(value + 9 * HOUR).toISOString().slice(0, 16).replace('T', ' ');

// Every symbol uses the same complete-hour grid. Nulls never become zero or carry forward.
export function buildPremiumComparison(results, endMs, days) {
    if (![3, 7].includes(days) || !Number.isSafeInteger(endMs) || endMs % HOUR !== 0) {
        throw new Error('비교 기준 시간과 기간을 확인하세요.');
    }
    const times = Array.from({ length: days * 24 }, (_, i) => endMs - (days * 24 - i) * HOUR);
    const series = results.map(({ ticker, data, error }, index) => {
        const lookup = new Map((data?.items || []).map((item) => [item.hour_ms, item]));
        const samples = [],
            statuses = [];
        const values = times.map((at) => {
            const item = lookup.get(at);
            const valid =
                !error &&
                ['complete', 'partial'].includes(item?.status) &&
                Number.isFinite(item.mean_pct) &&
                Number.isInteger(item.samples) &&
                item.samples > 0 &&
                item.samples <= 3600;
            samples.push(valid ? item.samples : 0);
            statuses.push(error ? 'request_error' : item?.status || 'pending');
            return valid ? item.mean_pct : null;
        });
        return {
            ticker,
            label: ticker,
            color: COMPARISON_COLORS[index],
            values,
            samples,
            statuses,
            error: error || null,
            isolatedPoints: true,
        };
    });
    return { times, series, endMs, days };
}

export function premiumComparisonCsv(comparison) {
    const cell = (value) => '"' + String(value ?? '').replaceAll('"', '""') + '"';
    const header = [
        'time_kst',
        ...comparison.series.flatMap((line) => [
            line.ticker + '_premium_pct',
            line.ticker + '_samples',
            line.ticker + '_status',
        ]),
    ];
    const rows = comparison.times.map((at, i) => [
        comparisonTime(at),
        ...comparison.series.flatMap((line) => [line.values[i], line.samples[i], line.statuses[i]]),
    ]);
    return '\ufeff' + [header, ...rows].map((row) => row.map(cell).join(',')).join('\r\n');
}

export class PremiumComparison {
    constructor({ api, onSelectionChange = () => {} }) {
        this.api = api;
        this.onSelectionChange = onSelectionChange;
        this.selected = new Set();
        this.universe = new Set();
        this.seeded = false;
        this.endMs = null;
        this.version = 0;
        this.pending = null;
        this.loading = false;
        this.timer = null;
        this.result = null;
        this.$ = (id) => document.getElementById('premium-compare-' + id);
        this.chart = new TimeChart(this.$('chart'), {
            format: (value) => comparisonNumber.format(value) + '%',
            onHover: (at) => this.chart.setCursor(at),
            onZoom: (range) => this.chart.setZoom(range),
        });
        this.$('days').addEventListener('change', () => this.changed());
        this.$('clear').addEventListener('click', () => {
            this.selected.clear();
            this.changed();
        });
        this.$('reload').addEventListener('click', () => this.refresh());
        this.$('reset').addEventListener('click', () => this.chart.setZoom(null));
        this.$('details').addEventListener('toggle', () => this.renderTable());
        this.$('csv').addEventListener('click', () => {
            if (!this.result) return;
            const url = URL.createObjectURL(
                new Blob([premiumComparisonCsv(this.result)], { type: 'text/csv;charset=utf-8' }),
            );
            const a = document.createElement('a');
            a.href = url;
            a.download = 'premium-comparison.csv';
            a.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
        });
    }
    setUniverse(items, endMs) {
        this.universe = new Set(items.map((item) => item.ticker));
        this.endMs = endMs;
        this.selected = new Set([...this.selected].filter((ticker) => this.universe.has(ticker)));
        if (!this.seeded && items.length) {
            this.selected = new Set(items.slice(0, 3).map((item) => item.ticker));
            this.seeded = true;
        }
        this.renderLegend();
    }
    toggle(ticker, enabled) {
        if (!this.universe.has(ticker)) return false;
        if (enabled && !this.selected.has(ticker) && this.selected.size >= COMPARISON_LIMIT) {
            this.$('status').textContent =
                '최대 10개 종목을 비교할 수 있습니다. 선택한 종목을 해제한 뒤 추가하세요.';
            return false;
        }
        if (enabled) this.selected.add(ticker);
        else this.selected.delete(ticker);
        this.changed();
        return true;
    }
    clearView(message) {
        this.result = null;
        this.chart.setCursor(null);
        this.chart.setData([], []);
        this.$('head').replaceChildren();
        this.$('rows').replaceChildren();
        this.$('csv').disabled = true;
        this.$('status').textContent = message;
    }
    changed() {
        this.cancel();
        this.chart.setZoom(null);
        this.clearView(
            this.selected.size ? '선택한 종목의 괴리율을 조회합니다.' : '비교할 종목을 선택하세요.',
        );
        this.renderLegend();
        this.onSelectionChange();
        if (this.selected.size) this.timer = setTimeout(() => this.refresh(), 150);
    }
    cancel() {
        ++this.version;
        this.pending = null;
        clearTimeout(this.timer);
        this.chart.setCursor(null);
    }
    renderLegend() {
        this.$('legend').replaceChildren(
            ...[...this.selected].map((ticker, index) => {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'small-button';
                button.style.borderColor = COMPARISON_COLORS[index];
                button.style.color = COMPARISON_COLORS[index];
                button.textContent = ticker + ' ×';
                button.setAttribute('aria-label', ticker + ' 비교에서 제외');
                button.addEventListener('click', () => this.toggle(ticker, false));
                return button;
            }),
        );
    }
    async refresh() {
        clearTimeout(this.timer);
        const version = ++this.version;
        this.pending = null;
        if (!this.selected.size || !Number.isFinite(this.endMs)) {
            this.clearView('비교할 종목을 선택하세요.');
            return;
        }
        this.pending = {
            version,
            tickers: [...this.selected],
            endMs: this.endMs,
            days: Number(this.$('days').value),
        };
        this.clearView(`${this.selected.size}개 종목 비교 조회 중…`);
        if (this.loading) return;
        this.loading = true;
        try {
            // One drain owns requests. A newer selection replaces pending work; at most 3 requests are in flight.
            while (this.pending) {
                const request = this.pending;
                this.pending = null;
                const results = [];
                for (let offset = 0; offset < request.tickers.length; offset += 3) {
                    if (request.version !== this.version) break;
                    const tickers = request.tickers.slice(offset, offset + 3);
                    const batch = await Promise.allSettled(
                        tickers.map((ticker) =>
                            this.api(`/api/premiums/hourly?ticker=${encodeURIComponent(ticker)}`),
                        ),
                    );
                    batch.forEach((item, i) =>
                        results.push(
                            item.status === 'fulfilled'
                                ? { ticker: tickers[i], data: item.value }
                                : {
                                      ticker: tickers[i],
                                      error: item.reason?.message || '조회 실패',
                                  },
                        ),
                    );
                }
                if (request.version !== this.version) continue;
                this.result = buildPremiumComparison(results, request.endMs, request.days);
                this.chart.setData(this.result.times, this.result.series, { gapMs: HOUR });
                const failed = results.filter((item) => item.error).map((item) => item.ticker);
                const valid = this.result.series.filter((line) =>
                    line.values.some(Number.isFinite),
                ).length;
                this.$('status').textContent =
                    `선택 ${results.length}종목 · 유효 ${valid}종목 · 최근 ${request.days}일 · ${comparisonTime(this.result.times[0])} ~ ${comparisonTime(request.endMs)} KST (종료 미포함)` +
                    (failed.length ? ` · 조회 실패: ${failed.join(', ')} (빈 값으로 표시)` : '') +
                    (valid === 0 ? ' · 표시할 유효 값이 없습니다. 집계 상태를 확인하세요.' : '');
                this.$('csv').disabled = false;
                this.renderTable();
            }
        } catch (error) {
            this.clearView(error.message || '비교 데이터를 표시하지 못했습니다.');
        } finally {
            this.loading = false;
        }
    }
    renderTable() {
        if (!this.result || !this.$('details').open) return;
        const makeRow = (values, tag = 'td') => {
            const tr = document.createElement('tr');
            for (const value of values) {
                const cell = document.createElement(tag);
                cell.textContent = value;
                tr.append(cell);
            }
            return tr;
        };
        this.$('head').replaceChildren(
            makeRow(
                [
                    '시간 시작 · KST',
                    ...this.result.series.map((line) => line.ticker + ' · % / 유효 초'),
                ],
                'th',
            ),
        );
        this.$('rows').replaceChildren(
            ...this.result.times
                .map((at, i) =>
                    makeRow([
                        comparisonTime(at),
                        ...this.result.series.map((line) =>
                            Number.isFinite(line.values[i])
                                ? `${comparisonNumber.format(line.values[i])}% / ${line.samples[i]}`
                                : line.statuses[i] === 'request_error'
                                  ? '조회 실패'
                                  : line.statuses[i] === 'error'
                                    ? '집계 오류'
                                    : '—',
                        ),
                    ]),
                )
                .reverse(),
        );
    }
}

export function initPremiums({ api }) {
    const $ = (id) => document.getElementById(id),
        details = $('premium-details');
    let result = null,
        selected = null,
        timer = null,
        loading = false,
        hourVersion = 0;
    const comparison = new PremiumComparison({ api, onSelectionChange: () => render() });
    const number = (value, digits = 4) =>
        Number.isFinite(value)
            ? new Intl.NumberFormat('ko-KR', { maximumFractionDigits: digits }).format(value)
            : '—';
    const time = (value) =>
        Number.isFinite(value)
            ? new Intl.DateTimeFormat('ko-KR', {
                  timeZone: 'Asia/Seoul',
                  month: '2-digit',
                  day: '2-digit',
                  hour: '2-digit',
                  minute: '2-digit',
                  hour12: false,
              }).format(value)
            : '—';
    const error = (value) => {
        $('premium-error').textContent = value?.message || String(value);
        $('premium-error').hidden = false;
    };
    const clearError = () => {
        $('premium-error').hidden = true;
    };
    const chart = new TimeChart($('premium-hourly-chart'), {
        format: (v) => number(v) + '%',
        onHover: (t) => chart.setCursor(t),
        onZoom: (range) => chart.setZoom(range),
    });
    function row(values) {
        const tr = document.createElement('tr');
        for (const value of values) {
            const td = document.createElement('td');
            td.textContent = String(value);
            tr.append(td);
        }
        return tr;
    }
    function render() {
        if (!result) return;
        const job = result.job || {},
            states = {
                idle: '대기',
                running: '집계 중',
                complete: '집계 완료',
                partial: '일부 오류',
                failed: '집계 실패',
                interrupted: '중단 후 재개 대기',
            };
        $('premium-enabled').checked = result.enabled;
        $('premium-enabled').disabled = false;
        $('premium-status').textContent =
            `공통 ${result.items.length}종목 · 기준 ${time(result.end_ms)} KST (직전 시간까지) · ${states[job.state] || job.state}${job.state === 'running' ? ` ${job.completed_units || 0}/${job.total_units || 0} · ${job.ticker || ''}` : ''} · ${result.enabled ? `다음 확인 ${time(result.next_update_ms)} KST` : '자동 누적 꺼짐'}`;
        if (job.last_error || job.error) error(job.last_error || job.error);
        $('premium-refresh').disabled = job.state === 'running';
        $('premium-rows').replaceChildren();
        const query = $('premium-search').value.trim().toUpperCase();
        for (const item of result.items.filter((item) => item.ticker.includes(query))) {
            const a = item.windows['3'],
                b = item.windows['7'];
            const tr = row([
                item.ticker,
                number(a.mean_pct),
                number(a.coverage_pct, 2) + '%',
                number(b.mean_pct),
                number(b.coverage_pct, 2) + '%',
                time(item.latest_hour_ms),
                item.error_hours,
            ]);
            const selection = document.createElement('td'),
                checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.checked = comparison.selected.has(item.ticker);
            checkbox.setAttribute('aria-label', item.ticker + ' 비교 선택');
            checkbox.addEventListener('change', () => {
                if (!comparison.toggle(item.ticker, checkbox.checked))
                    checkbox.checked = comparison.selected.has(item.ticker);
            });
            selection.append(checkbox);
            tr.prepend(selection);
            if (item.ticker === selected) tr.classList.add('premium-selected');
            const td = document.createElement('td'),
                button = document.createElement('button');
            button.type = 'button';
            button.className = 'small-button';
            button.textContent = '보기';
            button.setAttribute('aria-label', `${item.ticker} 시간별 괴리 보기`);
            button.addEventListener('click', () => loadHourly(item.ticker).catch(error));
            td.append(button);
            tr.append(td);
            $('premium-rows').append(tr);
        }
        if (!$('premium-rows').children.length) {
            const empty = row(['표시할 종목이 없습니다.']);
            empty.firstElementChild.colSpan = 9;
            $('premium-rows').append(empty);
        }
    }
    async function loadHourly(ticker) {
        const version = ++hourVersion,
            changed = selected !== ticker;
        selected = ticker;
        if (changed) {
            chart.setZoom(null);
            chart.setData([], []);
            $('premium-hourly-rows').replaceChildren();
            $('premium-hourly-title').textContent = `${ticker} · 시간별 괴리 불러오는 중`;
        }
        render();
        const data = await api(`/api/premiums/hourly?ticker=${encodeURIComponent(ticker)}`);
        if (version !== hourVersion) return;
        chart.setData(
            data.items.map((item) => item.hour_ms),
            [
                {
                    label: `${ticker} 시간 평균`,
                    values: data.items.map((item) => item.mean_pct),
                    color: '#5bdfb7',
                    isolatedPoints: true,
                },
            ],
            { gapMs: 3600000 },
        );
        $('premium-hourly-title').textContent = `${ticker} · 최근 7일 시간별 괴리 평균`;
        $('premium-hourly-rows').replaceChildren();
        const labels = {
            complete: '완료',
            partial: '일부 관측',
            empty: '관측 없음',
            pending: '집계 대기',
            error: '읽기 오류',
        };
        for (const item of [...data.items].reverse()) {
            const tr = row([
                time(item.hour_ms),
                number(item.mean_pct),
                `${item.samples.toLocaleString()} / 3,600`,
                labels[item.status] || item.status,
            ]);
            if (item.error) tr.title = item.error;
            $('premium-hourly-rows').append(tr);
        }
    }
    function schedule() {
        clearTimeout(timer);
        if (details.open)
            timer = setTimeout(
                () => {
                    if (document.hidden || $('module-analysis').hidden) {
                        schedule();
                        return;
                    }
                    load();
                },
                result?.job?.state === 'running' ? 3000 : 15000,
            );
    }
    async function load() {
        if (loading) return;
        loading = true;
        clearError();
        try {
            result = await api('/api/premiums');
            comparison.setUniverse(result.items, result.end_ms);
            render();
            const ticker = result.items.some((item) => item.ticker === selected)
                ? selected
                : result.items[0]?.ticker;
            const updates = await Promise.allSettled([
                ticker ? loadHourly(ticker) : Promise.resolve(),
                details.open ? comparison.refresh() : Promise.resolve(),
            ]);
            for (const update of updates) if (update.status === 'rejected') error(update.reason);
        } catch (e) {
            error(e);
        } finally {
            loading = false;
            schedule();
        }
    }
    details.addEventListener('toggle', () => {
        if (details.open) load();
        else {
            clearTimeout(timer);
            comparison.cancel();
            chart.setCursor(null);
        }
    });
    $('premium-search').addEventListener('input', render);
    $('premium-reload').addEventListener('click', load);
    $('premium-refresh').addEventListener('click', async () => {
        clearError();
        $('premium-refresh').disabled = true;
        try {
            await api('/api/premiums/refresh', { method: 'POST', body: {} });
            $('premium-status').textContent =
                '최근 7일의 새 데이터와 변경된 시간대를 확인하도록 요청했습니다.';
            clearTimeout(timer);
            timer = setTimeout(load, 1000);
        } catch (e) {
            error(e);
            $('premium-refresh').disabled = false;
        }
    });
    $('premium-enabled').addEventListener('change', async () => {
        const enabled = $('premium-enabled').checked;
        $('premium-enabled').disabled = true;
        try {
            await api('/api/premiums/settings', { method: 'POST', body: { enabled } });
            await load();
        } catch (e) {
            $('premium-enabled').checked = !enabled;
            error(e);
        } finally {
            $('premium-enabled').disabled = false;
        }
    });
    $('premium-csv').addEventListener('click', async () => {
        try {
            const blob = await api('/api/premiums/csv', { responseType: 'blob' }),
                url = URL.createObjectURL(blob),
                a = document.createElement('a');
            a.href = url;
            a.download = 'premium-averages.csv';
            a.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch (e) {
            error(e);
        }
    });
    window.addEventListener('pagehide', () => {
        clearTimeout(timer);
        comparison.cancel();
    });
}
