import { TimeChart } from '../../shared/charts.js';
import {
    $,
    exchanges,
    milliseconds,
    number,
    time,
    errorAt,
    clearError,
    tableRows,
    source,
    range,
} from '../../shared/dom.js';
import { createHistoryControls } from '../../shared/source-controls.js';
import { createJobRunner } from '../../shared/jobs.js';

// Values supplied by the analysis API are already percentages. The raw ratio is
// shown separately; account cash payments cannot be inferred from these records.
export function initFunding({ api, getCatalog }) {
    const state = { version: 0, selection: null, result: null, jobId: null };
    const percent = (value) => (Number.isFinite(value) ? number(value, 10) + '%' : '—');
    const raw = (value) => (Number.isFinite(value) ? number(value, 16) : '—');
    const remaining = (value) => (Number.isFinite(value) ? number(value, 3) + '초' : '—');
    const chart = new TimeChart($('funding-chart'), {
        format: percent,
        onHover: (t) => chart.setCursor(t),
        onZoom: (r) => chart.setZoom(r),
    });
    const job = createJobRunner({ api, prefix: 'funding', getVersion: () => state.version });
    const selected = () => ({
        ...source('funding'),
        start: $('funding-start').value,
        end: $('funding-end').value,
        interval: $('funding-interval').value,
        price_mode: $('funding-price-mode').value,
    });
    const key = () => JSON.stringify(selected());
    function clear(message = '거래소·종목과 기간을 선택한 뒤 펀딩비율 조회를 누르세요.') {
        state.result = null;
        state.jobId = null;
        for (const id of ['funding-rate', 'funding-next', 'funding-range', 'funding-rows'])
            $(id).textContent = '—';
        $('funding-identity').textContent = '조회 후 거래소와 종목을 표시합니다.';
        $('funding-asof').textContent = '마지막 관측 기준 잔여 시간입니다.';
        $('funding-points').textContent = '저장된 스냅샷 기준';
        $('funding-rate-label').textContent =
            $('funding-price-mode').value === 'mean'
                ? '마지막 구간 평균 펀딩비율'
                : '마지막 원본 펀딩비율';
        $('funding-table-rate-label').textContent =
            $('funding-price-mode').value === 'mean' ? '평균 펀딩비율 (%)' : '펀딩비율 (%)';
        $('funding-table-raw-label').textContent =
            $('funding-price-mode').value === 'mean' ? '구간 평균 비율' : '원본 비율';
        $('funding-chart-label').textContent =
            '원천 펀딩주기 기준 % · 조회 결과는 계좌 정산금액이 아닙니다.';
        $('funding-price-help').textContent =
            $('funding-price-mode').value === 'mean'
                ? '구간 내 펀딩비율 스냅샷의 평균입니다. 원본 관측시각·서버시각·잔여 시간은 그 구간의 마지막 관측에서 가져옵니다.'
                : '각 구간에서 마지막으로 저장된 원본 펀딩비율을 사용합니다. 잔여 시간은 당시 관측 기준이며 현재 시점의 카운트다운이 아닙니다.';
        chart.setData([], []);
        chart.setZoom(null);
        chart.setCursor(null);
        $('funding-placeholder').hidden = false;
        $('funding-placeholder').querySelector('strong').textContent = message;
        tableRows('funding-table', [], 6, '아직 조회한 펀딩비율이 없습니다.');
        $('funding-csv').disabled = true;
    }
    function changed() {
        const selection = key();
        if (selection === state.selection) return;
        state.selection = selection;
        state.version++;
        clearError('funding');
        clear();
        $('funding-run').disabled = false;
        $('funding-run').textContent = '펀딩비율 조회';
        $('funding-message').textContent =
            '설정이 바뀌었습니다. 펀딩비율 조회를 누르면 새 조건으로 계산합니다.';
    }
    function current(version, selection) {
        if (version !== state.version) return false;
        if (selection !== key()) {
            changed();
            return false;
        }
        return true;
    }
    async function run(event) {
        event.preventDefault();
        const version = ++state.version,
            selection = key();
        state.selection = selection;
        clearError('funding');
        clear('서버에서 저장된 펀딩비율을 조회하고 있습니다.');
        $('funding-run').disabled = true;
        $('funding-run').textContent = '조회 중…';
        try {
            const instrument = source('funding'),
                config = {
                    ...range('funding'),
                    price_mode: $('funding-price-mode').value,
                    legs: [{ id: 'A', ...instrument, data_type: 'funding' }],
                    formula: 'A',
                    unit: '%',
                    ewm_span: 1,
                    max_points: 6000,
                };
            const response = await job('/api/analyses', config, version);
            if (!response || !current(version, selection)) return;
            const result = response.result,
                leg = result.legs?.[0];
            if (
                leg?.data_type !== 'funding' ||
                !Array.isArray(leg.values) ||
                leg.values.length !== result.times.length
            )
                throw new Error('조회 결과에 펀딩비율 소스가 없습니다.');
            if (
                leg.exchange !== instrument.exchange ||
                leg.ticker.toUpperCase() !== instrument.ticker.toUpperCase()
            )
                throw new Error('요청한 종목과 조회 결과가 다릅니다. 다시 조회해주세요.');
            if (result.meta?.price_mode !== config.price_mode)
                throw new Error('요청한 조회 방식과 결과가 다릅니다. 다시 조회해주세요.');
            state.result = result;
            state.jobId = response.id;
            const last = result.times.length - 1,
                mean = config.price_mode === 'mean',
                identity = `${exchanges[leg.exchange] || leg.exchange} · ${leg.ticker}`,
                mode = mean ? '구간 평균 펀딩비율' : '마지막 원본 펀딩비율';
            $('funding-rate').textContent = percent(leg.values[last]);
            $('funding-identity').textContent = identity + ' · ' + mode;
            $('funding-next').textContent = remaining(leg.next_funding_in_seconds?.[last]);
            $('funding-asof').textContent = Number.isFinite(leg.source_times?.[last])
                ? `관측 ${time(leg.source_times[last])} KST 기준`
                : '원본에 정산 잔여 시간이 없으면 —로 표시합니다.';
            $('funding-range').textContent =
                percent(result.stats.min) + ' / ' + percent(result.stats.max);
            $('funding-rows').textContent = number(result.meta.rows_read, 0);
            $('funding-points').textContent =
                `${number(result.meta.aligned_points, 0)}개 구간 · 차트 ${number(result.times.length, 0)}개`;
            $('funding-chart-label').textContent =
                `${identity} · ${mode} · ${time(result.times[0])} ~ ${time(result.times.at(-1))} KST`;
            chart.setData(
                result.times,
                [{ label: mode + ' (%)', values: leg.values, color: '#b8a1ed', width: 1.8 }],
                {
                    gapMs:
                        milliseconds[config.interval] *
                        Math.max(
                            1,
                            Math.ceil(
                                (result.meta.aligned_points || result.times.length) /
                                    Math.max(1, result.times.length),
                            ),
                        ),
                },
            );
            $('funding-placeholder').hidden = leg.values.some(Number.isFinite);
            $('funding-csv').disabled = !result.times.length;
            const rows = [];
            for (let i = last; i >= Math.max(0, result.times.length - 100); i--)
                rows.push([
                    time(result.times[i]),
                    Number.isFinite(leg.source_times?.[i]) ? time(leg.source_times[i]) : '—',
                    percent(leg.values[i]),
                    raw(leg.funding_rate?.[i]),
                    remaining(leg.next_funding_in_seconds?.[i]),
                    Number.isFinite(leg.server_times?.[i]) ? time(leg.server_times[i]) : '—',
                ]);
            tableRows('funding-table', rows, 6, '선택한 구간에 펀딩비율이 없습니다.');
            $('funding-message').textContent =
                `${identity} · ${mode} 조회 완료. ` + (result.meta.warnings || []).join(' ');
        } catch (error) {
            if (current(version, selection)) {
                clear('조회하지 못했습니다. 조건을 확인한 뒤 다시 조회하세요.');
                errorAt('funding', error);
                $('funding-message').textContent = '펀딩비율을 조회하지 못했습니다.';
            }
        } finally {
            if (current(version, selection)) {
                $('funding-run').disabled = false;
                $('funding-run').textContent = '펀딩비율 조회';
            }
        }
    }
    async function exportCSV() {
        if (!state.jobId) return;
        const id = state.jobId,
            version = state.version,
            selection = state.selection;
        clearError('funding');
        $('funding-csv').disabled = true;
        try {
            const response = await fetch('/api/analyses/' + encodeURIComponent(id) + '/csv', {
                credentials: 'same-origin',
                headers: { Accept: 'text/csv' },
            });
            if (!current(version, selection) || state.jobId !== id) return;
            if (response.status === 401) {
                location.assign('/login?next=%2F');
                throw new Error('로그인이 만료되었습니다. 다시 로그인해주세요.');
            }
            if (!response.ok) {
                let detail = {};
                try {
                    detail = await response.json();
                } catch {}
                throw new Error(
                    typeof detail.error === 'string'
                        ? detail.error
                        : 'CSV를 내보내지 못했습니다. 다시 조회해주세요.',
                );
            }
            const blob = await response.blob();
            if (!current(version, selection) || state.jobId !== id) return;
            const url = URL.createObjectURL(blob),
                link = document.createElement('a');
            link.href = url;
            link.download = 'oms-funding-rates.csv';
            link.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch (error) {
            if (current(version, selection) && state.jobId === id) errorAt('funding', error);
        } finally {
            if (current(version, selection) && state.jobId === id)
                $('funding-csv').disabled = !state.result?.times.length;
        }
    }
    $('funding-form').addEventListener('submit', run);
    $('funding-csv').addEventListener('click', exportCSV);
    for (const input of $('funding-form').querySelectorAll('input'))
        input.addEventListener('input', changed);
    const controls = createHistoryControls({
        prefix: 'funding',
        api,
        getCatalog,
        onChanged: changed,
        dataType: 'funding',
    });
    state.selection = key();
    clear();
    return {
        ready: controls.ready,
        refresh: controls.refresh,
        onEnter() {
            chart.draw();
        },
        async openQuery(query) {
            if (query.data_type !== 'funding')
                throw new Error('이 파일은 펀딩비율 화면에서 열 수 없습니다.');
            controls.applySelection(query);
            const selection = key();
            await controls.bounds({ setRange: false });
            if (selection !== key()) return;
            return run({ preventDefault() {} });
        },
    };
}
