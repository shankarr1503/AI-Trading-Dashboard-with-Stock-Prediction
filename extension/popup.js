// Toolbar popup: bot state and controls (administrators), open positions,
// recent decisions and a ticker panel (any signed-in user). Every panel loads
// on its own, so a slow call never blocks the rest of the popup.

import { ApiClient } from './lib/api.js';
import { $, fill, h, message, show } from './lib/dom.js';
import {
  DASH, dateRange, modeLabel, money, num, parseServerTime, pct, qty, relativeTime, signClass, truncate,
} from './lib/format.js';
import { collectSnapshot, lastSnapshot } from './lib/poller.js';
import { dashboardUrl } from './lib/server.js';
import { getSettings } from './lib/settings.js';
import { BADGES, botView, botWarnings, describe } from './lib/status.js';
import { detectTicker, normalizeSymbol } from './lib/tickers.js';

const PANIC_ARM_MS = 20_000;   // the confirmation step disarms itself after this long

let settings;
let client;
let snapshot = null;
let refreshSeq = 0;
let tickerSeq = 0;
let busy = false;              // a bot control request is in flight
let panicTimer = null;

// ─── Small helpers ────────────────────────────────────────────────────────────

function errorText(e) {
  if (!e) return 'Unknown error';
  if (e.kind === 'auth') return `${e.message}. Open settings to sign in.`;
  return e.message || String(e);
}

function tellWorker() {
  // Let the service worker refresh the badge too (fire and forget).
  try {
    chrome.runtime.sendMessage({ type: 'refresh' }).catch(() => {});
  } catch {
    /* worker unavailable: the next alarm updates the badge */
  }
}

function openOptions() {
  chrome.runtime.openOptionsPage();
}

function openDashboard(page = 'bot') {
  chrome.tabs.create({ url: dashboardUrl(settings.serverUrl, page) });
}

// ─── Connection header and notice ─────────────────────────────────────────────

function renderHeader(snap) {
  const conn = $('conn');
  const text = $('conn-text');
  let host = settings.serverUrl;
  try {
    host = new URL(settings.serverUrl).host;
  } catch { /* keep the raw value */ }
  const who = snap.user ? `${snap.user.username}${snap.user.is_superuser ? ' (admin)' : ''}` : '';
  switch (snap.phase) {
    case 'admin':
    case 'user':
      conn.dataset.state = 'ok';
      text.textContent = `${host} · ${who}`;
      break;
    case 'signed_out':
      conn.dataset.state = 'warn';
      text.textContent = `${host} · not signed in`;
      break;
    case 'no_access':
      conn.dataset.state = 'warn';
      text.textContent = `${host} · no access`;
      break;
    default:
      conn.dataset.state = 'down';
      text.textContent = `${host} · offline`;
  }
  conn.title = settings.serverUrl;
}

function renderNotice(snap) {
  const notice = $('notice');
  let title = '';
  let body = '';
  let retry = true;
  let optionsLabel = 'Open settings';
  switch (snap.phase) {
    case 'disconnected':
      title = 'Cannot reach the server';
      body = `${settings.serverUrl} is not answering${snap.error ? ` (${truncate(snap.error, 120)})` : ''}. `
        + 'Start the AI Trading Bot desktop app (its tray menu shows the server URL) or check the address in settings.';
      break;
    case 'no_access':
      title = 'Access to the server not granted';
      body = `Chrome has not allowed this extension to reach ${settings.serverUrl}. Open settings and save the server again to grant access.`;
      retry = false;
      break;
    case 'signed_out':
      title = 'Not signed in';
      body = 'Sign in with your AI Trading Bot account to see the bot and analyse tickers.';
      optionsLabel = 'Sign in';
      retry = false;
      break;
    case 'user':
      title = `Signed in as ${snap.user ? snap.user.username : 'a user'}`;
      body = 'Bot status and controls need an administrator account. You can still analyse tickers below.';
      retry = false;
      optionsLabel = 'Settings';
      break;
    default:
      show(notice, false);
      return;
  }
  $('notice-title').textContent = title;
  $('notice-text').textContent = body;
  show($('notice-retry'), retry);
  $('notice-options').textContent = optionsLabel;
  show(notice, true);
}

// ─── Bot card ─────────────────────────────────────────────────────────────────

function dailyPnl(bot) {
  const summary = bot.last_cycle_summary || {};
  const p = summary.daily_pnl_pct;
  if (typeof p !== 'number') return null;
  const equity = typeof summary.equity === 'number' ? summary.equity : bot.equity;
  const amount = typeof equity === 'number' && p > -100 ? equity - equity / (1 + p / 100) : null;
  return { pct: p, amount };
}

function renderBot(snap) {
  const card = $('bot');
  if (snap.phase !== 'admin' || !snap.bot) {
    show(card, false);
    show($('positions'), false);
    show($('decisions'), false);
    return;
  }
  const bot = snap.bot;
  const view = botView(bot);
  const state = $('bot-state');
  state.textContent = BADGES[view.primary].label;
  state.dataset.state = view.primary;

  const mode = $('bot-mode');
  mode.textContent = modeLabel(bot.mode);
  mode.className = `tag${bot.live_trading ? ' live' : ''}`;
  mode.title = bot.live_trading ? 'Live trading with real money' : 'Simulated / paper trading';
  $('bot-updated').textContent = `updated ${relativeTime(snap.at)}`;

  const reason = $('bot-reason');
  const showReason = (view.primary === 'halted' || view.primary === 'flatten') && bot.halt_reason;
  reason.textContent = showReason ? truncate(bot.halt_reason, 220) : '';
  show(reason, !!showReason);

  fill($('bot-warnings'), botWarnings(bot).map((w) => h('li', { dataset: { key: w.key } }, h('b', {}, w.label), w.detail)));

  $('m-equity').textContent = money(bot.equity);
  const dd = $('m-drawdown');
  dd.textContent = pct(bot.drawdown_pct);
  dd.className = `num${(bot.drawdown_pct || 0) > 0 ? ' down' : ''}`;
  const daily = dailyPnl(bot);
  const dailyEl = $('m-daily');
  if (daily) {
    fill(dailyEl, pct(daily.pct, { signed: true }),
      daily.amount !== null ? h('small', {}, ` ${money(daily.amount, { signed: true, digits: 0 })}`) : null);
    dailyEl.className = `num ${signClass(daily.pct)}`;
  } else {
    dailyEl.textContent = DASH;
    dailyEl.className = 'num';
  }
  const cycle = $('m-cycle');
  const summaryStatus = bot.last_cycle_summary && bot.last_cycle_summary.status;
  fill(cycle, relativeTime(bot.last_cycle_at),
    summaryStatus ? h('small', {}, ` · ${String(summaryStatus).replace(/_/g, ' ')}`) : null);
  const at = parseServerTime(bot.last_cycle_at);
  cycle.title = at ? at.toLocaleString() : 'No cycle has run yet';

  const halted = view.primary === 'halted' || view.primary === 'flatten';
  $('btn-start').disabled = busy || halted || !!bot.enabled;
  $('btn-start').title = halted ? 'The bot is halted: review and reset it in the dashboard first' : 'Allow new entries';
  $('btn-pause').disabled = busy || !bot.enabled;
  $('btn-pause').title = 'Stop opening positions (open positions keep their stops)';
  $('btn-panic').disabled = busy;

  show(card, true);
  show($('positions'), true);
  show($('decisions'), true);
}

async function loadPositions(seq) {
  const msg = $('pos-msg');
  message(msg, 'Loading positions…', 'busy');
  let rows;
  try {
    rows = await client.botPositions();
  } catch (e) {
    if (seq !== refreshSeq) return;
    fill($('pos-body'));
    $('pos-count').textContent = '';
    message(msg, errorText(e), 'error');
    return;
  }
  if (seq !== refreshSeq) return;
  $('pos-count').textContent = `(${rows.length})`;
  fill($('pos-body'), rows.map((p) => h('tr', {},
    h('td', { class: 'sym', title: p.opened_at ? `Opened ${relativeTime(p.opened_at)}` : null }, p.symbol),
    h('td', { class: 'r' }, qty(p.qty)),
    h('td', { class: 'r' }, num(p.entry_price)),
    h('td', { class: 'r' }, num(p.stop_price)),
    h('td', { class: `r ${signClass(p.unrealized_pnl)}`, title: p.current_price ? `Last ${num(p.current_price)}` : 'No current price' },
      money(p.unrealized_pnl, { signed: true }),
      h('small', {}, pct(p.unrealized_pnl_pct, { signed: true }))),
  )));
  show($('pos-table'), rows.length > 0);
  message(msg, rows.length ? '' : 'No open positions.');
}

async function loadDecisions() {
  const msg = $('dec-msg');
  message(msg, 'Loading…', 'busy');
  try {
    const rows = await client.botDecisions(20);
    fill($('dec-list'), rows.map((d) => h('li', {},
      h('span', { class: 'when', title: parseServerTime(d.created_at)?.toLocaleString() || '' }, relativeTime(d.created_at)),
      h('span', {}, h('span', { class: `act ${d.action === 'BUY' ? 'up' : d.action === 'SELL' ? 'down' : 'flat'}` }, d.action),
        d.symbol ? ` ${d.symbol}` : ''),
      h('span', { class: 'num flat' }, typeof d.score === 'number' ? d.score.toFixed(2) : ''),
      Array.isArray(d.reasons) && d.reasons.length ? h('span', { class: 'why' }, truncate(d.reasons[0], 140)) : null,
    )));
    message(msg, rows.length ? '' : 'No decisions recorded yet.');
  } catch (e) {
    message(msg, errorText(e), 'error');
  }
}

// ─── Refresh ──────────────────────────────────────────────────────────────────

function render(snap) {
  snapshot = snap;
  renderHeader(snap);
  renderNotice(snap);
  renderBot(snap);
  show($('ticker'), snap.phase === 'admin' || snap.phase === 'user');
  document.title = `AI Trading Bot: ${describe(snap).label}`;
}

async function refresh({ notifyWorker = true } = {}) {
  const seq = ++refreshSeq;
  $('refresh').disabled = true;
  try {
    const snap = await collectSnapshot({ server: settings.serverUrl, client, chromeApi: chrome });
    if (seq !== refreshSeq) return;
    render(snap);
    if (snap.phase === 'admin') {
      loadPositions(seq);
      if ($('decisions').open) loadDecisions();
    }
  } finally {
    if (seq === refreshSeq) $('refresh').disabled = false;
  }
  if (notifyWorker) tellWorker();
}

// ─── Bot controls ─────────────────────────────────────────────────────────────

async function control(label, call) {
  if (busy) return;
  busy = true;
  const msg = $('bot-msg');
  message(msg, `${label}…`, 'busy');
  if (snapshot) renderBot(snapshot);
  try {
    const result = await call();
    busy = false;
    return result;
  } catch (e) {
    busy = false;
    message(msg, `${label} failed: ${errorText(e)}`, 'error');
    return undefined;
  } finally {
    await refresh();
  }
}

async function start() {
  const r = await control('Starting', () => client.botStart());
  if (r) message($('bot-msg'), 'Bot started: new entries are allowed.', 'ok');
}

async function pause() {
  const r = await control('Pausing', () => client.botStop());
  if (r) message($('bot-msg'), 'Bot paused: no new entries; open positions keep their stops.', 'ok');
}

function disarmPanic() {
  clearTimeout(panicTimer);
  panicTimer = null;
  show($('panic-confirm'), false);
  show($('btn-panic'), true);
}

function armPanic() {
  message($('bot-msg'), '');
  show($('btn-panic'), false);
  show($('panic-confirm'), true);
  $('btn-panic-cancel').focus();
  clearTimeout(panicTimer);
  panicTimer = setTimeout(disarmPanic, PANIC_ARM_MS);
}

function flattenMessage(r) {
  const closed = Array.isArray(r.closed) ? r.closed.length : 0;
  switch (r.status) {
    case 'done':
      return [`Flatten complete: ${closed} position(s) closed. The bot is halted until you reset it in the dashboard.`, 'ok'];
    case 'pending':
      return [`Flatten pending: ${closed} closed so far. ${r.message || ''}`.trim(), 'error'];
    case 'queued':
      return [`Flatten queued: ${r.message || 'it runs as soon as the current cycle ends.'}`, 'error'];
    default:
      return [`Flatten: ${r.status || 'requested'} ${r.message || ''}`.trim(), ''];
  }
}

async function panicConfirmed() {
  disarmPanic();
  const r = await control('Flattening every position', () => client.botFlatten());
  if (r) {
    const [text, kind] = flattenMessage(r);
    message($('bot-msg'), text, kind);
  }
}

// ─── Ticker panel ─────────────────────────────────────────────────────────────

function section(id, ...children) {
  const el = $(id);
  fill(el, ...children);
  show(el, true);
  return el;
}

function loadingSection(id, title) {
  section(id, h('h3', {}, title), h('p', { class: 'loading' }, 'Loading'));
}

function errorSection(id, title, e) {
  let text = errorText(e);
  if (e && e.kind === 'http' && e.status === 404) text = `${text}. Market data may be unavailable right now.`;
  section(id, h('h3', {}, title), h('p', { class: 'msg error' }, text));
}

function kv(rows) {
  return h('dl', { class: 'kv' }, rows.filter(Boolean).flatMap(([k, v, cls]) => [h('dt', {}, k), h('dd', { class: cls || null }, v)]));
}

const signed = (v, d = 2) => (typeof v === 'number' ? `${v > 0 ? '+' : ''}${v.toFixed(d)}` : DASH);

function renderQuote(q) {
  const change = typeof q.change_pct === 'number' ? q.change_pct : null;
  section('quote',
    h('span', { class: 'qsym' }, q.symbol),
    h('span', { class: 'qprice' }, num(q.current_price)),
    h('span', { class: 'num flat' }, q.currency && q.currency !== 'USD' ? q.currency : ''),
    change !== null ? h('span', { class: `num ${signClass(change)}` }, pct(change, { signed: true })) : null,
    q.name && q.name !== q.symbol ? h('span', { class: 'qname' }, q.name) : null);
}

function renderAnalysis(a) {
  const sig = String(a.signal || 'HOLD').toUpperCase();
  const reasons = Array.isArray(a.reasons) ? a.reasons.slice(0, 5) : [];
  section('analysis',
    h('h3', {}, "Bot's view", h('span', { class: `signal ${sig}` }, sig)),
    kv([
      ['Technical score', signed(a.technical_score), `num ${signClass(a.technical_score)}`],
      ['Blended score', signed(a.score), `num ${signClass(a.score)}`],
      ['Regime', a.regime ? String(a.regime).replace(/_/g, ' ') : DASH],
      ['Entry', num(a.entry_price ?? a.current_price)],
      a.stop_loss != null ? ['Stop', num(a.stop_loss), 'down'] : null,
      a.target_price != null ? ['Target', num(a.target_price), 'up'] : null,
      a.risk_reward_ratio != null ? ['Reward : risk', `${num(a.risk_reward_ratio)} : 1`] : null,
      typeof a.expected_edge_pct === 'number'
        ? ['Edge vs costs', `${pct(a.expected_edge_pct, { signed: true })} vs ${pct(a.round_trip_cost_pct)}${a.worth_the_costs ? '' : ' (not worth it)'}`]
        : null,
    ]),
    reasons.length ? h('ul', { class: 'reasons' }, reasons.map((r) => h('li', {}, truncate(r, 200)))) : null);
}

function scoreClass(v) {
  if (typeof v !== 'number') return '';
  return v >= 65 ? 'score-strong' : v >= 40 ? 'score-neutral' : 'score-weak';
}

function scoreWord(v) {
  if (typeof v !== 'number') return '';
  return v >= 65 ? 'strong' : v >= 40 ? 'neutral' : 'weak';
}

function renderFundamentals(d) {
  const card = d.scorecard || {};
  const f = d.fundamentals || {};
  const v = d.valuation || {};
  const cal = (d.snapshot && d.snapshot.calendar) || {};
  const piotroski = f.piotroski && typeof f.piotroski.score === 'number' ? `${f.piotroski.score}/9` : 'n/a';
  const zone = f.altman && f.altman.zone ? String(f.altman.zone).replace(/_/g, ' ') : 'n/a';
  const composite = typeof card.composite === 'number' ? card.composite : null;
  const failed = d.data_quality && d.data_quality.failed_sections;
  const failedCalendar = Array.isArray(failed) && failed.includes('calendar');
  section('fundamentals',
    h('h3', {}, 'Research'),
    kv([
      ['Composite score', composite !== null ? `${composite.toFixed(0)}/100 (${scoreWord(composite)})` : 'n/a',
        `num ${scoreClass(composite)}`],
      typeof card.coverage === 'number' ? ['Data coverage', `${(card.coverage * 100).toFixed(0)}%`] : null,
      ['Fair value', v.fair_value != null
        ? `${num(v.fair_value)} (${pct(v.upside_pct, { signed: true, digits: 1 })})` : 'n/a',
      `num ${signClass(v.upside_pct)}`],
      ['Piotroski F-score', piotroski],
      ['Altman zone', zone, zone === 'distress' ? 'down' : zone === 'safe' ? 'up' : ''],
      ['Next earnings', cal.next_earnings ? dateRange(cal.next_earnings, cal.next_earnings_end)
        : failedCalendar ? 'unknown (fetch failed)' : 'none scheduled'],
    ]),
    Array.isArray(f.flags) && f.flags.length
      ? h('ul', { class: 'reasons' }, f.flags.slice(0, 3).map((x) => h('li', { class: 'down' }, truncate(x, 160)))) : null,
    failed && failed.length ? h('p', { class: 'hint' }, `Partial data: ${failed.join(', ')} could not be fetched.`) : null);
}

async function analyze(rawSymbol) {
  const symbol = normalizeSymbol(rawSymbol);
  const msg = $('ticker-msg');
  if (!symbol) {
    message(msg, 'Enter a valid symbol: letters, digits and . - ^ = (up to 20), e.g. AAPL, BRK-B, RELIANCE.NS.', 'error');
    return;
  }
  const seq = ++tickerSeq;
  message(msg, '');
  $('ticker-input').value = symbol;
  show($('quote'), false);
  loadingSection('analysis', "Bot's view");
  loadingSection('fundamentals', 'Research');

  client.quote(symbol).then((q) => { if (seq === tickerSeq) renderQuote(q); })
    .catch(() => { if (seq === tickerSeq) show($('quote'), false); });
  client.analyze(symbol).then((a) => { if (seq === tickerSeq) renderAnalysis(a); })
    .catch((e) => { if (seq === tickerSeq) errorSection('analysis', "Bot's view", e); });
  client.fundamentals(symbol).then((d) => { if (seq === tickerSeq) renderFundamentals(d); })
    .catch((e) => { if (seq === tickerSeq) errorSection('fundamentals', 'Research', e); });
}

/** Ticker of the page in the active tab (readable thanks to activeTab when the popup opens). */
async function detectFromActiveTab() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    const found = tab && tab.url ? detectTicker(tab.url) : null;
    if (found) {
      $('ticker-input').value = found.symbol;
      $('ticker-source').textContent = `Detected on ${found.source}`;
    }
    return found;
  } catch {
    return null;
  }
}

// ─── Startup ──────────────────────────────────────────────────────────────────

function wire() {
  $('refresh').addEventListener('click', () => refresh());
  $('open-options').addEventListener('click', openOptions);
  $('notice-options').addEventListener('click', openOptions);
  $('notice-retry').addEventListener('click', () => refresh());
  $('btn-start').addEventListener('click', start);
  $('btn-pause').addEventListener('click', pause);
  $('btn-dashboard').addEventListener('click', () => openDashboard('bot'));
  $('btn-panic').addEventListener('click', armPanic);
  $('btn-panic-cancel').addEventListener('click', disarmPanic);
  $('btn-panic-confirm').addEventListener('click', panicConfirmed);
  $('decisions').addEventListener('toggle', () => { if ($('decisions').open) loadDecisions(); });
  $('ticker-form').addEventListener('submit', (ev) => {
    ev.preventDefault();
    $('ticker-source').textContent = '';
    analyze($('ticker-input').value);
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && !$('panic-confirm').hidden) {
      ev.preventDefault();
      disarmPanic();
    }
  });
}

async function init() {
  $('version').textContent = chrome.runtime.getManifest().version;
  wire();
  settings = await getSettings();
  client = new ApiClient({ baseUrl: settings.serverUrl });

  // Instant first paint from what the service worker saw last, then a live refresh.
  const last = await lastSnapshot().catch(() => null);
  if (last && last.server === settings.serverUrl) render(last);
  const detection = detectFromActiveTab();
  const refreshed = refresh();

  const found = await detection;
  if (found && (await client.isSignedIn())) analyze(found.symbol);
  await refreshed;
}

init().catch((e) => {
  const msg = $('bot-msg');
  show($('bot'), true);
  message(msg, `The popup failed to start: ${errorText(e)}`, 'error');
});
