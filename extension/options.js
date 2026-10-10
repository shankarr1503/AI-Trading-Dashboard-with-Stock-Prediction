// Options page: server URL (+ the host permission it needs), the desktop app's
// pairing code, sign-in, polling interval and notification preferences.

import { ApiClient, createTokenStore } from './lib/api.js';
import { $, fill, h, message, show } from './lib/dom.js';
import { relativeTime } from './lib/format.js';
import {
  forgetPairings, normalizePairingCode, pairingRequired, tryNormalizePairingCode, verifyServer,
} from './lib/pairing.js';
import { MONITOR_KEY } from './lib/poller.js';
import {
  DEFAULT_SERVER_URL, DESKTOP_PORTS, findDesktopServers, hasServerPermission, normalizeServerUrl, permissionPatternFor,
  tryNormalizeServerUrl,
} from './lib/server.js';
import {
  getSettings, MAX_POLL_MINUTES, MIN_POLL_MINUTES, NOTIFY_KINDS, saveSettings,
} from './lib/settings.js';
import { describe } from './lib/status.js';

let settings;
let client;
const tokens = createTokenStore();

function errorText(e) {
  return (e && e.message) || String(e);
}

function makeClient(url) {
  return new ApiClient({ baseUrl: url, tokens, pairingCode: settings.pairingCode });
}

function formatCode(code) {
  return code ? code.match(/.{1,4}/g).join('-') : '';
}

/** The code typed in the field when it is valid, else the saved one. */
function currentCode() {
  return tryNormalizePairingCode($('pairing-code').value) || settings.pairingCode;
}

/**
 * Check that `url` is the paired desktop app, when that is needed. Resolves with a
 * sentence for the message line, or null when no check applies; throws an ApiError.
 */
async function pairingCheck(url) {
  if (!pairingRequired(url, settings.pairingCode)) return null;
  await makeClient(url).ensureTrusted({ force: true });
  return 'Paired: it proved it is your AI Trading Bot app.';
}

function hostOf(url) {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

/**
 * Ask Chrome for access to a non-loopback server. Must run straight from the
 * click (user gesture), before any other await. Resolves true when access is held.
 */
async function requestAccess(url) {
  const pattern = permissionPatternFor(url);
  if (!pattern) return true;
  try {
    return await chrome.permissions.request({ origins: [pattern] });
  } catch {
    return false;
  }
}

async function testConnection(url, msgEl) {
  message(msgEl, `Connecting to ${url}…`, 'busy');
  const probe = makeClient(url);
  try {
    const health = await probe.health();
    const name = health && health.service ? health.service : 'server';
    const version = health && health.version ? ` ${health.version}` : '';
    const connected = `Connected: ${name}${version} is ${health && health.status ? health.status : 'up'}.`;
    let paired;
    try {
      paired = await pairingCheck(url);
    } catch (e) {
      message(msgEl, `${connected} ${errorText(e)}`, 'error');
      return false;
    }
    message(msgEl, paired ? `${connected} ${paired}` : connected, 'ok');
    return true;
  } catch (e) {
    let hint = '';
    if (e.kind === 'network') {
      hint = permissionPatternFor(url)
        ? ' Check the address, that the server is up and that its certificate is valid.'
        : ' Is the desktop app running? Its tray menu shows the server URL.';
    }
    message(msgEl, `${errorText(e)}.${hint}`, 'error');
    return false;
  }
}

// ─── Server ─────────────────────────────────────────────────────────────────

async function onSaveServer(ev) {
  ev.preventDefault();
  const msg = $('server-msg');
  let url;
  try {
    url = normalizeServerUrl($('server-url').value);
  } catch (e) {
    message(msg, errorText(e), 'error');
    return;
  }
  if (!(await requestAccess(url))) {
    message(msg, `Chrome did not grant access to ${hostOf(url)}, so the extension cannot reach it.`, 'error');
    return;
  }
  const previous = settings.serverUrl;
  if (url !== previous) {
    // Tokens belong to the server that issued them: sign out of the old one.
    await tokens.clear();
    const oldPattern = permissionPatternFor(previous);
    if (oldPattern && oldPattern !== permissionPatternFor(url)) {
      chrome.permissions.remove({ origins: [oldPattern] }).catch(() => {});
    }
  }
  settings = await saveSettings({ serverUrl: url });
  client = makeClient(url);
  $('server-url').value = url;
  const ok = await testConnection(url, msg);
  message(msg, ok ? `Saved. ${msg.textContent}` : `Saved, but: ${msg.textContent}`, ok ? 'ok' : 'error');
  await renderAccount();
}

async function onTestServer() {
  const msg = $('server-msg');
  let url;
  try {
    url = normalizeServerUrl($('server-url').value);
  } catch (e) {
    message(msg, errorText(e), 'error');
    return;
  }
  if (!(await requestAccess(url))) {
    message(msg, `Chrome did not grant access to ${hostOf(url)}.`, 'error');
    return;
  }
  await testConnection(url, msg);
}

/**
 * "Find the desktop app": every server answering on the app's ports. Any program can
 * answer there, so with a pairing code only a server that proves it is the paired
 * app is picked; without one, only a single answer is filled in (and sign-in still
 * waits for the pairing code).
 */
async function onFindServer() {
  const msg = $('server-msg');
  const button = $('server-find');
  const first = DESKTOP_PORTS[0];
  const last = DESKTOP_PORTS[DESKTOP_PORTS.length - 1];
  button.disabled = true;
  message(msg, `Looking for the desktop app on ports ${first}–${last}…`, 'busy');
  try {
    const found = await findDesktopServers();
    if (!found.length) {
      $('server-url').value = DEFAULT_SERVER_URL;
      message(msg, `The desktop app is not running on this computer (nothing answered on ports ${first}–${last}). `
        + 'Start it, then try again.', 'error');
      return;
    }
    const code = currentCode();
    let pick;
    let note = '';
    if (code) {
      const results = await Promise.all(found.map((url) => verifyServer({ baseUrl: url, code }).then(() => true, () => false)));
      const mine = found.filter((_, i) => results[i]);
      const others = found.filter((_, i) => !results[i]);
      if (!mine.length) {
        message(msg, `Found ${found.join(', ')}, but none of them proved it is your app with this pairing code. `
          + 'Check the code (tray menu → Copy Pairing Code for the Chrome Extension).', 'error');
        return;
      }
      [pick] = mine;
      note = ' (pairing code checked)';
      if (others.length) note += `. Also answering, but NOT your app: ${others.join(', ')}`;
    } else if (found.length === 1) {
      [pick] = found;
      note = '. Enter its pairing code below before you sign in';
    } else {
      message(msg, `Several servers answer on this computer (${found.join(', ')}). Enter the pairing code below, `
        + 'then press Find again to pick your app.', 'error');
      return;
    }
    $('server-url').value = pick;
    if (pick === settings.serverUrl) {
      message(msg, `The desktop app is running at ${pick}, the server already in use${note}.`, 'ok');
    } else {
      message(msg, `Found the desktop app at ${pick}${note}. Press Save to use it.`, 'ok');
      $('server-save').focus();
    }
  } finally {
    button.disabled = false;
  }
}

// ─── Pairing ────────────────────────────────────────────────────────────────

async function onSavePairing(ev) {
  ev.preventDefault();
  const msg = $('pairing-msg');
  const typed = $('pairing-code').value.trim();
  let code = '';
  if (typed) {
    try {
      code = normalizePairingCode(typed);
    } catch (e) {
      message(msg, errorText(e), 'error');
      return;
    }
  }
  settings = await saveSettings({ pairingCode: code });
  forgetPairings();
  client = makeClient(settings.serverUrl);
  $('pairing-code').value = formatCode(code);
  if (!code) {
    message(msg, 'Pairing code removed.', 'ok');
    return;
  }
  if (!pairingRequired(settings.serverUrl, code)) {
    message(msg, 'Saved. It is checked for servers on this computer; the current server is checked by its certificate.', 'ok');
    return;
  }
  message(msg, `Saved. Checking ${settings.serverUrl}…`, 'busy');
  try {
    await client.ensureTrusted({ force: true });
    message(msg, `Saved. Paired: ${settings.serverUrl} proved it is your AI Trading Bot app.`, 'ok');
  } catch (e) {
    message(msg, `Saved, but: ${errorText(e)}`, 'error');
  }
}

// ─── Account ────────────────────────────────────────────────────────────────

function showSignedIn(user) {
  $('who-server').textContent = settings.serverUrl;
  $('who').textContent = user ? `${user.username} (${user.email})` : 'your account';
  const role = $('who-role');
  role.textContent = user && user.is_superuser ? 'ADMINISTRATOR' : 'USER';
  role.className = `tag${user && user.is_superuser ? ' admin' : ''}`;
  show($('signed-in'), true);
  show($('login-form'), false);
}

function showSignedOut() {
  show($('signed-in'), false);
  show($('login-form'), true);
}

async function renderAccount() {
  const t = await tokens.load(settings.serverUrl);
  if (!t.refreshToken) {
    showSignedOut();
    return;
  }
  showSignedIn(t.user);
  // Re-validate in the background: the session may have been revoked on the server.
  client.me().then(showSignedIn).catch((e) => {
    if (e.kind === 'auth') {
      showSignedOut();
      message($('account-msg'), 'Your session has ended: sign in again.', 'error');
    } else if (e.kind === 'pairing') {
      message($('account-msg'), `Your session is not used: ${errorText(e)}`, 'error');
    }
  });
}

async function onSignIn(ev) {
  ev.preventDefault();
  const msg = $('account-msg');
  const typed = tryNormalizeServerUrl($('server-url').value);
  if (typed !== settings.serverUrl) {
    message(msg, 'Save the server URL first.', 'error');
    return;
  }
  const email = $('email').value.trim();
  const password = $('password').value;
  if (!email || !password) {
    message(msg, 'Enter your email and password.', 'error');
    return;
  }
  if (!(await hasServerPermission(settings.serverUrl))) {
    message(msg, `No access to ${hostOf(settings.serverUrl)}: press Save above to grant it.`, 'error');
    return;
  }
  $('sign-in').disabled = true;
  message(msg, 'Signing in…', 'busy');
  try {
    const user = await client.login(email, password);
    $('password').value = '';
    showSignedIn(user);
    message(msg, user.is_superuser
      ? `Signed in as ${user.username}. The toolbar badge now shows the bot state.`
      : `Signed in as ${user.username}. This account is not an administrator: bot status, alerts and controls are unavailable, ticker analysis works.`,
    'ok');
  } catch (e) {
    const text = e.kind === 'http' && e.status === 401 ? 'Wrong email or password.'
      : e.kind === 'http' && e.status === 422 ? 'Enter a valid email address.'
        : e.kind === 'pairing' ? `Not signed in: ${errorText(e)}`
          : errorText(e);
    message(msg, text, 'error');
  } finally {
    $('sign-in').disabled = false;
  }
}

async function onSignOut() {
  await client.logout();
  showSignedOut();
  message($('account-msg'), 'Signed out. The tokens were removed from this browser.', 'ok');
}

// ─── Monitoring ─────────────────────────────────────────────────────────────

function renderMonitoring() {
  const select = $('poll');
  fill(select, Array.from({ length: MAX_POLL_MINUTES - MIN_POLL_MINUTES + 1 }, (_, i) => {
    const n = MIN_POLL_MINUTES + i;
    return h('option', { value: n, selected: n === settings.pollMinutes }, String(n));
  }));
  fill($('notify-list'), Object.entries(NOTIFY_KINDS).map(([key, label]) => h('label', {},
    h('input', { type: 'checkbox', 'data-kind': key, checked: settings.notify[key] !== false }),
    h('span', {}, label))));
}

async function onPollChange() {
  settings = await saveSettings({ pollMinutes: Number($('poll').value) });
  message($('monitor-msg'), `Saved: checking every ${settings.pollMinutes} minute(s).`, 'ok');
}

async function onNotifyChange(ev) {
  const input = ev.target;
  if (!(input instanceof HTMLInputElement) || !input.dataset.kind) return;
  settings = await saveSettings({ notify: { [input.dataset.kind]: input.checked } });
  message($('monitor-msg'), 'Saved.', 'ok');
}

async function onTestNotification() {
  try {
    await chrome.notifications.create('tradebot-test', {
      type: 'basic',
      iconUrl: chrome.runtime.getURL('icons/icon128.png'),
      title: 'AI Trading Bot',
      message: 'Notifications work. You will be alerted when the bot halts, flattens, stalls or keeps failing.',
      priority: 1,
    });
    message($('monitor-msg'), 'Test notification sent. If you do not see it, check your system notification settings.', 'ok');
  } catch (e) {
    message($('monitor-msg'), `Could not show a notification: ${errorText(e)}`, 'error');
  }
}

// ─── Status line ────────────────────────────────────────────────────────────

function renderStatusLine(record) {
  const el = $('status-line');
  const snap = record && record.snapshot;
  if (!snap || snap.server !== settings.serverUrl) {
    el.textContent = '';
    return;
  }
  el.textContent = `Status: ${describe(snap).label} (checked ${relativeTime(snap.at)}).`;
}

// ─── Startup ────────────────────────────────────────────────────────────────

async function init() {
  settings = await getSettings();
  client = makeClient(settings.serverUrl);
  $('server-url').value = settings.serverUrl;
  $('pairing-code').value = formatCode(settings.pairingCode);
  renderMonitoring();

  $('server-form').addEventListener('submit', onSaveServer);
  $('server-test').addEventListener('click', onTestServer);
  $('server-find').addEventListener('click', onFindServer);
  $('pairing-form').addEventListener('submit', onSavePairing);
  $('login-form').addEventListener('submit', onSignIn);
  $('sign-out').addEventListener('click', onSignOut);
  $('poll').addEventListener('change', onPollChange);
  $('notify-list').addEventListener('change', onNotifyChange);
  $('test-notification').addEventListener('click', onTestNotification);

  const stored = await chrome.storage.local.get(MONITOR_KEY);
  renderStatusLine(stored[MONITOR_KEY]);
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'local' && changes[MONITOR_KEY]) renderStatusLine(changes[MONITOR_KEY].newValue);
  });
  await renderAccount();
}

init().catch((e) => message($('server-msg'), `Settings failed to load: ${errorText(e)}`, 'error'));
