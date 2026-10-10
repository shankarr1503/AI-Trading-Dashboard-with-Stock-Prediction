'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {
  DEFAULT_STOP_DEADLINE_S, STOP_MARGIN_MS, durationText, shutdownWaitMs, stopDeadlineSeconds, stoppingDetail,
} = require('../lib/shutdown');

test('the shell waits longer than the sidecar\'s worst-case stop', () => {
  // backend/desktop.py: BOT_STOP_TIMEOUT 150 + cancel 15 + HTTP 30 + tasks 5 + worker threads 10.
  const sidecarWorstCase = 150 + 15 + 30 + 5 + 10;
  assert.equal(DEFAULT_STOP_DEADLINE_S, sidecarWorstCase);
  assert.ok(shutdownWaitMs(null) > sidecarWorstCase * 1000);
  assert.equal(shutdownWaitMs({ stopping: true, deadline_seconds: 210 }), 210_000 + STOP_MARGIN_MS);
  assert.equal(shutdownWaitMs({ stopping: true, deadline_seconds: 61 }), 61_000 + STOP_MARGIN_MS);
  // Missing or nonsense answers fall back to the default; absurd ones are capped.
  for (const answer of [null, {}, { deadline_seconds: null }, { deadline_seconds: 'x' }, { deadline_seconds: -5 }]) {
    assert.equal(stopDeadlineSeconds(answer), DEFAULT_STOP_DEADLINE_S, JSON.stringify(answer));
  }
  assert.equal(stopDeadlineSeconds({ deadline_seconds: 1e9 }), 3600);
  assert.equal(stopDeadlineSeconds({ deadline_seconds: 12.2 }), 13);
});

test('the "Stopping safely" text matches the wait', () => {
  assert.equal(durationText(210), '3½ minutes');
  assert.equal(durationText(150), '2½ minutes');
  assert.equal(durationText(60), '1 minute');
  assert.equal(durationText(61), '1½ minutes');
  assert.equal(durationText(5), 'half a minute');
  assert.match(stoppingDetail(), /at most about 3½ minutes/);
});
