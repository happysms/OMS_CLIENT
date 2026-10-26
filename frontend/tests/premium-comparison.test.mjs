import assert from 'node:assert/strict';
import test from 'node:test';
import {buildPremiumComparison, premiumComparisonCsv} from '../features/analysis/premiums.js';

const hour = 3600000, end = Date.parse('2026-10-27T00:00:00Z');
const sample = (at, mean, samples = 3600, status = 'complete') => ({hour_ms: at, mean_pct: mean, samples, status});

test('symbols align on common hours without joining across missing values', () => {
  const result = buildPremiumComparison([
    {ticker: 'BTC', data: {items: [sample(end - 3*hour, -2), sample(end - hour, 0)]}},
    {ticker: 'ETH', data: {items: [sample(end - 2*hour, 4, 10, 'partial'), sample(end, 999)]}},
  ], end, 3);
  assert.equal(result.times.length, 72);
  assert.equal(result.times[0], end - 72*hour);
  assert.equal(result.times.at(-1), end - hour);
  assert.deepEqual(result.series[0].values.slice(-3), [-2, null, 0]);
  assert.deepEqual(result.series[1].values.slice(-3), [null, 4, null]);
  assert.deepEqual(result.series[1].samples.slice(-3), [0, 10, 0]);
});

test('failed and invalid samples remain empty without removing successful symbols', () => {
  const result = buildPremiumComparison([
    {ticker: 'BTC', error: 'network failed'},
    {ticker: 'ETH', data: {items: [sample(end - hour, 2)]}},
    {ticker: 'XRP', data: {items: [sample(end - hour, 55, 100, 'error'), sample(end - 2*hour, 4, 0), sample(end - 3*hour, Infinity)]}},
  ], end, 7);
  assert.equal(result.times.length, 168);
  assert.ok(result.series[0].values.every(value => value === null));
  assert.equal(result.series[0].statuses.at(-1), 'request_error');
  assert.equal(result.series[1].values.at(-1), 2);
  assert.ok(result.series[2].values.every(value => value === null));
  assert.equal(result.series[2].statuses.at(-1), 'error');
  const csv = premiumComparisonCsv(result);
  assert.match(csv, /BTC_premium_pct/);
  assert.match(csv, /"","0","request_error","2","3600","complete"/);
  assert.equal(csv.split('\r\n').length, 169);
});

test('comparison clips to requested boundaries and validates its bounded window', () => {
  const inputs = [{ticker: 'BTC', data: {items: [sample(end - 73*hour, 1), sample(end - 72*hour, 2), sample(end, 3)]}}];
  assert.equal(buildPremiumComparison(inputs, end, 3).series[0].values[0], 2);
  assert.equal(buildPremiumComparison(inputs, end, 7).series[0].values.filter(Number.isFinite).length, 2);
  for (const [at, days] of [[end, 30], [end+1, 3], [NaN, 7]]) {
    assert.throws(() => buildPremiumComparison(inputs, at, days));
  }
});
