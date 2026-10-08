import assert from 'node:assert/strict';
import { test } from 'node:test';

import { alertConditions, computeNotifications } from '../lib/alerts.js';
import { BADGES, botView, botWarnings, describe, effectiveStale } from '../lib/status.js';
import { ADMIN, botStatus, USER } from './fake-chrome.mjs';

const SERVER = 'http://127.0.0.1:47821';
const NOW = Date.parse('2026-10-08T12:05:00Z');
const admin = (bot) => describe({ phase: 'admin', server: SERVER, user: ADMIN, bot: botStatus(bot), at: NOW }, NOW);

test('badge texts fit and colours are set', () => {
  for (const [key, b] of Object.entries(BADGES)) {
    assert.ok(b.text.length <= 4, `${key} badge text too long`);
    assert.match(b.color, /^#[0-9a-f]{6}$/i);
    assert.match(b.textColor, /^#[0-9a-f]{6}$/i);
    assert.ok(b.label);
  }
});

test('running and paused', () => {
  const run = admin({ enabled: true });
  assert.equal(run.key, 'running');
  assert.equal(run.badge.text, 'RUN');
  assert.equal(run.badge.color, '#00d4aa');
  assert.match(run.title, /RUNNING \(PAPER\)/);
  assert.match(run.title, /Equity \$100,500\.00 · drawdown 1\.25%/);
  assert.match(run.title, /Server: http:\/\/127\.0\.0\.1:47821/);
  const pause = admin({ enabled: false });
  assert.equal(pause.key, 'paused');
  assert.equal(pause.badge.text, 'OFF');
  assert.equal(pause.primary, 'paused');
});

test('halted and flatten pending outrank everything', () => {
  const halted = admin({ halted: true, halt_reason: 'KILL: drawdown 12% ≥ 10%', stale: true, enabled: true, consecutive_failures: 5 });
  assert.equal(halted.key, 'halted');
  assert.equal(halted.badge.text, 'HALT');
  assert.equal(halted.badge.color, '#ff4757');
  assert.match(halted.title, /KILL: drawdown/);
  const flat = admin({ halted: true, flatten_requested: true, halt_reason: 'FLATTEN: manual by owner' });
  assert.equal(flat.key, 'flatten');
  assert.equal(flat.badge.text, 'FLAT');
  assert.equal(flat.label, 'FLATTEN PENDING');
});

test('stale only matters while the loop has work to do', () => {
  // Fresh install, server without a bot loop: paused, flat, never cycled → not alarming.
  const idle = botStatus({ stale: true, last_cycle_at: null, enabled: false, open_positions: 0 });
  assert.equal(effectiveStale(idle), false);
  assert.equal(admin(idle).badge.text, 'OFF');
  assert.equal(effectiveStale({ ...idle, enabled: true }), true);
  assert.equal(effectiveStale({ ...idle, open_positions: 2 }), true);
  assert.equal(effectiveStale({ ...idle, flatten_requested: true }), true);
  assert.equal(effectiveStale({ ...idle, stale: false, enabled: true }), false);
  const late = admin({ stale: true, enabled: true, last_cycle_at: '2026-10-08T09:05:00' });
  assert.equal(late.key, 'stale');
  assert.equal(late.badge.text, 'LATE');
  assert.equal(late.primary, 'running', 'the popup still says RUNNING, with a STALE warning');
  assert.match(late.title, /STALE: No trading cycle since 3 h ago/);
  const never = botWarnings(botStatus({ stale: true, enabled: true, last_cycle_at: null }), NOW);
  assert.match(never[0].detail, /No trading cycle has run yet/);
});

test('data faults and failing cycles', () => {
  const df = admin({ enabled: true, consecutive_data_faults: 1 });
  assert.equal(df.key, 'data_fault');
  assert.equal(df.badge.text, 'DATA');
  const fail = admin({ enabled: true, consecutive_failures: 2, last_error: 'TimeoutError: broker' });
  assert.equal(fail.key, 'failing');
  assert.equal(fail.badge.text, 'ERR');
  assert.match(fail.title, /2 consecutive failed cycles: TimeoutError: broker/);
  assert.equal(admin({ enabled: true, consecutive_failures: 1 }).key, 'running', 'one failure is not a trend');
  const both = admin({ enabled: true, consecutive_failures: 3, consecutive_data_faults: 2, stale: true });
  assert.equal(both.key, 'data_fault', 'most severe health warning wins');
  assert.deepEqual(both.warnings.map((w) => w.key), ['stale', 'data_fault', 'failing']);
  const cfg = botWarnings(botStatus({ config_error: 'max_positions must be > 0' }));
  assert.deepEqual(cfg.map((w) => w.key), ['config']);
});

test('connection phases', () => {
  const down = describe({ phase: 'disconnected', server: SERVER, error: 'Cannot reach the server at x' }, NOW);
  assert.equal(down.key, 'disconnected');
  assert.equal(down.badge.text, 'DOWN');
  assert.match(down.title, /tray menu shows the server URL/);
  const out = describe({ phase: 'signed_out', server: SERVER }, NOW);
  assert.equal(out.badge.text, '?');
  assert.match(out.title, /Not signed in/);
  const user = describe({ phase: 'user', server: SERVER, user: USER }, NOW);
  assert.equal(user.badge.text, '');
  assert.match(user.title, /administrator/);
  const noAccess = describe({ phase: 'no_access', server: 'https://bot.example.com' }, NOW);
  assert.equal(noAccess.badge.text, '!');
  assert.match(noAccess.title, /bot\.example\.com/);
});

test('botView', () => {
  assert.deepEqual(botView(botStatus({ enabled: true })).health,
    { stale: false, dataFault: false, failing: false, configError: null });
});

// ─── Notifications ──────────────────────────────────────────────────────────

const ALL_ON = { halted: true, flatten: true, stale: true, failures: true, dataFaults: true };

function run(sequence, prefs = ALL_ON) {
  // Feed a sequence of bot states through the transition logic like the poller does.
  let prev = null;
  const out = [];
  for (const overrides of sequence) {
    const bot = botStatus(overrides);
    const next = alertConditions(bot);
    out.push(computeNotifications(prev, next, bot, prefs, NOW).map((n) => n.kind));
    prev = next;
  }
  return out;
}

test('a halt notifies once, not on every poll', () => {
  const halted = { halted: true, halt_reason: 'KILL: daily loss 3.1% ≥ 3%' };
  assert.deepEqual(run([{}, halted, halted, halted]), [[], ['halted'], [], []]);
  const [n] = computeNotifications(alertConditions(botStatus()), alertConditions(botStatus(halted)), botStatus(halted), ALL_ON);
  assert.equal(n.id, 'tradebot-halted');
  assert.equal(n.critical, true);
  assert.match(n.message, /daily loss/);
});

test('halt → reset → halt again notifies again', () => {
  const halted = { halted: true };
  assert.deepEqual(run([halted, {}, halted]), [['halted'], [], ['halted']]);
});

test('panic flatten: requested once (no separate halt), then done', () => {
  const requested = { halted: true, flatten_requested: true, halt_reason: 'FLATTEN: manual by owner', open_positions: 2 };
  const done = { halted: true, flatten_requested: false, halt_reason: 'FLATTEN: manual by owner', open_positions: 0 };
  assert.deepEqual(run([{}, requested, requested, done, done]), [[], ['flatten'], [], ['flatten'], []]);
  const bot = botStatus(requested);
  const [n] = computeNotifications(alertConditions(botStatus()), alertConditions(bot), bot, ALL_ON);
  assert.equal(n.title, 'Panic flatten requested');
  assert.match(n.message, /manual by owner/);
  assert.doesNotMatch(n.message, /FLATTEN:/);
  const doneBot = botStatus(done);
  const [d] = computeNotifications(alertConditions(bot), alertConditions(doneBot), doneBot, ALL_ON);
  assert.equal(d.title, 'Flatten complete');
  assert.match(d.message, /All positions are closed/);
});

test('flatten requested on an already halted bot still notifies the flatten', () => {
  assert.deepEqual(run([{ halted: true }, { halted: true, flatten_requested: true }]), [['halted'], ['flatten']]);
});

test('first observation reports active conditions but never "flatten complete"', () => {
  assert.deepEqual(run([{ halted: true, flatten_requested: false }]), [['halted']]);
  assert.deepEqual(run([{}]), [[]]);
});

test('stale, consecutive failures and data faults ≥ 2 each notify once per transition', () => {
  const stale = { enabled: true, stale: true };
  assert.deepEqual(run([{ enabled: true }, stale, stale, { enabled: true }, stale]), [[], ['stale'], [], [], ['stale']]);
  assert.deepEqual(run([{ consecutive_failures: 1 }, { consecutive_failures: 2 }, { consecutive_failures: 3 }]),
    [[], ['failures'], []]);
  assert.deepEqual(run([{ consecutive_data_faults: 1 }, { consecutive_data_faults: 2 }, { consecutive_data_faults: 5 }]),
    [[], ['dataFaults'], []]);
  // Paused, flat bot without a loop: stale is not alarming.
  assert.deepEqual(run([{ stale: true, last_cycle_at: null }, { stale: true, last_cycle_at: null }]), [[], []]);
});

test('notification preferences silence a kind without breaking transitions', () => {
  const prefs = { ...ALL_ON, halted: false };
  assert.deepEqual(run([{}, { halted: true }, { halted: true, consecutive_failures: 2 }], prefs), [[], [], ['failures']]);
});

test('several new conditions at once give one notification each', () => {
  const kinds = run([{ halted: true, enabled: false, stale: true, open_positions: 1, consecutive_failures: 4, consecutive_data_faults: 3 }]);
  assert.deepEqual(kinds, [['halted', 'stale', 'failures', 'dataFaults']]);
});
