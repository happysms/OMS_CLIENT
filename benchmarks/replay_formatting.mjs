// Isolated formatter benchmark, not a claim about complete browser frame time.
import assert from 'node:assert/strict';
import { performance } from 'node:perf_hooks';
import { formatNumber, formatTime } from '../frontend/features/replay/controller.js';

const oldNumber = (value) =>
    value === null || value === undefined || !Number.isFinite(Number(value))
        ? '—'
        : Number(value).toLocaleString('ko-KR', { maximumFractionDigits: 8 });
const oldTime = (value) =>
    value === null || value === undefined
        ? '—'
        : new Intl.DateTimeFormat('ko-KR', {
              timeZone: 'Asia/Seoul',
              month: '2-digit',
              day: '2-digit',
              hour: '2-digit',
              minute: '2-digit',
              second: '2-digit',
              fractionalSecondDigits: 3,
              hour12: false,
          }).format(new Date(value));

const values = [
    null,
    undefined,
    NaN,
    Infinity,
    -Infinity,
    -0,
    0,
    1,
    -1234.567890123,
    '12345.6789',
    '',
    false,
    'bad',
];
for (const value of values) assert.equal(formatNumber(value), oldNumber(value));
for (const value of [null, undefined, 0, 1793026800123, 1793113199999])
    assert.equal(formatTime(value), oldTime(value));

function run(number, time) {
    const start = performance.now();
    let length = 0;
    for (let i = 0; i < 5000; i++) length += number(i / 3).length + time(1793026800000 + i).length;
    return { ms: performance.now() - start, length };
}
const before = [],
    after = [];
for (let i = 0; i < 3; i++) {
    const original = run(oldNumber, oldTime),
        optimized = run(formatNumber, formatTime);
    assert.equal(original.length, optimized.length);
    before.push(original.ms);
    after.push(optimized.ms);
}
const median = (values) => [...values].sort((a, b) => a - b)[1];
console.log(
    JSON.stringify(
        { before_ms: before, after_ms: after, speedup: median(before) / median(after) },
        null,
        2,
    ),
);
