// Replay inputs always mean Korean time, independent of the browser's timezone.
const MAX_PERIOD_MS = 7 * 86400000;
const KST_OFFSET_MS = 9 * 3600000;

function parseKst(value) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,3})?)?$/.test(value)) return NaN;
  const ms = Date.parse(value + '+09:00');
  if (!Number.isFinite(ms)) return NaN;
  // Reject normalized invalid calendar dates, such as February 30.
  const calendar = new Date(ms + KST_OFFSET_MS).toISOString();
  return calendar.slice(0, 16) === value.slice(0, 16) ? ms : NaN;
}

export function formatReplayPeriod(startMs, endMs) {
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) return '기간 정보 없음';
  const stamp = value => new Date(value + KST_OFFSET_MS).toISOString()
    .slice(0, 23).replace('T', ' ').replace(/\.000$/, '');
  return `${stamp(startMs)} ~ ${stamp(endMs)} KST · 시작 포함·종료 미포함`;
}

export function replayPeriod(start, end) {
  if (!start || !end) return {error: '모의투자 시작 일시와 종료 일시를 모두 입력하세요.'};
  const startMs = parseKst(start), endMs = parseKst(end);
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) {
    return {error: '한국 시간 기준으로 유효한 시작·종료 일시를 입력하세요.'};
  }
  const durationMs = endMs - startMs;
  if (durationMs <= 0) return {error: '종료 일시는 시작 일시보다 늦어야 합니다.'};
  if (durationMs > MAX_PERIOD_MS) return {error: '한 번의 모의투자 기간은 최대 7일입니다.'};
  return {startMs, endMs, durationMs, label: formatReplayPeriod(startMs, endMs)};
}
