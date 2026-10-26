import assert from 'node:assert/strict';
import test from 'node:test';
import {replayPeriod, formatReplayPeriod} from '../features/replay/period.js';

test('KST input preserves the full requested dates across years', () => {
  const period = replayPeriod('2026-12-31T23:59:59', '2027-01-01T00:00:01');
  assert.equal(period.error, undefined);
  assert.equal(period.startMs, Date.parse('2026-12-31T14:59:59Z'));
  assert.equal(period.durationMs, 2000);
  assert.match(period.label, /2026-12-31 23:59:59 ~ 2027-01-01 00:00:01 KST/);
  assert.match(period.label, /시작 포함·종료 미포함/);
});

test('missing, reversed and invalid dates are rejected before submission', () => {
  for (const [start, end] of [
    ['', '2026-10-27T12:00'], ['2026-10-27T12:00', ''],
    ['2026-10-27T12:00', '2026-10-27T12:00'],
    ['2026-10-27T12:00', '2026-10-26T12:00'],
    ['2026-02-30T12:00', '2026-03-03T12:00'],
    ['2026-10-27', '2026-10-28'],
  ]) assert.ok(replayPeriod(start, end).error, `${start} ~ ${end}`);
});

test('seven days is allowed; an extra millisecond is rejected', () => {
  assert.equal(replayPeriod('2026-10-20T12:00', '2026-10-27T12:00').durationMs, 7 * 86400000);
  assert.ok(replayPeriod('2026-10-20T12:00', '2026-10-27T12:00:00.001').error);
  assert.equal(formatReplayPeriod(undefined, undefined), '기간 정보 없음');
});
