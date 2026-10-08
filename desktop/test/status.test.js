'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { describeExit, formatStatus, quitWarning } = require('../lib/status');

const INFO = { trading_mode: 'paper', bot_enabled: true, halted: false, open_positions: 2 };

test('formatStatus', () => {
  assert.equal(formatStatus('starting', null), 'Bot server starting…');
  assert.equal(formatStatus('stopping', INFO), 'Bot server stopping…');
  assert.equal(formatStatus('stopped', INFO), 'Bot server stopped');
  assert.equal(formatStatus('running', null), 'Bot server running');
  assert.equal(formatStatus('running', INFO), 'Bot running · paper trading · 2 open positions');
  assert.equal(formatStatus('running', { ...INFO, bot_enabled: false, open_positions: 1 }), 'Bot paused · paper trading · 1 open position');
  assert.equal(formatStatus('running', { ...INFO, halted: true, trading_mode: 'alpaca_live', open_positions: 0 }), 'Bot halted · LIVE trading · 0 open positions');
});

test('quitWarning: only open paper positions need the app running', () => {
  const warning = quitWarning(INFO);
  assert.match(warning.message, /2 open paper positions/);
  assert.match(warning.detail, /only checked while the app is running/);
  assert.match(quitWarning({ ...INFO, open_positions: 1 }).message, /1 open paper position\./);
  assert.equal(quitWarning({ ...INFO, open_positions: 0 }), null);
  assert.equal(quitWarning({ ...INFO, trading_mode: 'alpaca_paper' }), null); // brackets live at the broker
  assert.equal(quitWarning({ ...INFO, trading_mode: 'alpaca_live' }), null);
  assert.equal(quitWarning(null), null); // server unreachable: nothing to warn about
  assert.equal(quitWarning({ ...INFO, open_positions: 'x' }), null);
});

test('describeExit', () => {
  assert.equal(describeExit(0, null), 'exit code 0');
  assert.equal(describeExit(1, null), 'exit code 1');
  assert.equal(describeExit(null, 'SIGKILL'), 'stopped by signal SIGKILL');
  assert.equal(describeExit(null, null), 'exited');
});
