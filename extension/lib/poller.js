// One monitoring pass, used by the service worker (background.js): collect a
// snapshot from the server, update the toolbar badge and tooltip, raise
// notifications for new alert conditions and persist what was seen.
// chrome and fetch are injected so the whole pass is unit-testable.

import { computeNotifications, alertConditions } from './alerts.js';
import { ApiClient, createTokenStore } from './api.js';
import { hasServerPermission } from './server.js';
import { getSettings } from './settings.js';
import { describe } from './status.js';

export const MONITOR_KEY = 'monitor';   // chrome.storage.local
export const ALARM_NAME = 'tradebot-poll';

/** Ask the server what state things are in. Never throws. */
export async function collectSnapshot({ server, client, chromeApi, now = Date.now() }) {
  const snap = { at: now, server, phase: 'disconnected', user: null, bot: null, error: null };
  if (!(await hasServerPermission(server, chromeApi))) {
    snap.phase = 'no_access';
    return snap;
  }
  try {
    await client.health();
  } catch (e) {
    // "Cannot reach the server" adds nothing to the disconnected state; timeouts and HTTP errors do.
    snap.error = e.kind === 'network' ? null : e.message;
    return snap;
  }
  if (!(await client.isSignedIn())) {
    snap.phase = 'signed_out';
    return snap;
  }
  try {
    snap.user = await client.me();
  } catch (e) {
    if (e.kind === 'auth') {
      snap.phase = 'signed_out';
    } else {
      snap.error = e.message;   // server up but failing: shown as disconnected with the reason
    }
    return snap;
  }
  if (!snap.user.is_superuser) {
    snap.phase = 'user';
    return snap;
  }
  try {
    snap.bot = await client.botStatus();
    snap.phase = 'admin';
  } catch (e) {
    if (e.kind === 'auth') snap.phase = 'signed_out';
    else if (e.kind === 'http' && e.status === 403) snap.phase = 'user';
    else snap.error = e.message;
  }
  return snap;
}

export async function applyBadge(chromeApi, display) {
  const action = chromeApi.action;
  await action.setBadgeText({ text: display.badge.text });
  await action.setBadgeBackgroundColor({ color: display.badge.color });
  if (typeof action.setBadgeTextColor === 'function') {
    await action.setBadgeTextColor({ color: display.badge.textColor });
  }
  await action.setTitle({ title: display.title });
}

async function notify(chromeApi, n) {
  try {
    await chromeApi.notifications.create(n.id, {
      type: 'basic',
      iconUrl: chromeApi.runtime.getURL('icons/icon128.png'),
      title: n.title,
      message: n.message,
      priority: n.critical ? 2 : 1,
      requireInteraction: !!n.critical,
    });
    return true;
  } catch {
    return false;   // notifications blocked by the OS or the user: the badge still tells the story
  }
}

/**
 * The full pass. Returns { snapshot, display, notifications }.
 * Alert conditions are remembered per server and account ("scope"); passes
 * that cannot see the bot (server down, signed out) keep the last conditions,
 * so coming back does not repeat old notifications.
 */
export async function runPoll({ chromeApi = globalThis.chrome, fetchImpl, now = () => Date.now() } = {}) {
  const settings = await getSettings(chromeApi);
  const server = settings.serverUrl;
  const client = new ApiClient({ baseUrl: server, tokens: createTokenStore(chromeApi, now), fetchImpl, now });
  const snapshot = await collectSnapshot({ server, client, chromeApi, now: now() });
  const display = describe(snapshot, now());
  await applyBadge(chromeApi, display);

  const stored = (await chromeApi.storage.local.get(MONITOR_KEY))[MONITOR_KEY] || {};
  const record = { ...stored, snapshot, display: { key: display.key, label: display.label } };
  let sent = [];
  if (snapshot.phase === 'admin') {
    const scope = `${server}|${snapshot.user.id}`;
    const prev = stored.scope === scope ? stored.conditions || null : null;
    const next = alertConditions(snapshot.bot);
    const notifications = computeNotifications(prev, next, snapshot.bot, settings.notify, now());
    for (const n of notifications) {
      if (await notify(chromeApi, n)) sent.push(n);
    }
    record.scope = scope;
    record.conditions = next;
  }
  await chromeApi.storage.local.set({ [MONITOR_KEY]: record });
  return { snapshot, display, notifications: sent };
}

/** Last snapshot the service worker stored (for an instant first paint of the popup). */
export async function lastSnapshot(chromeApi = globalThis.chrome) {
  const stored = (await chromeApi.storage.local.get(MONITOR_KEY))[MONITOR_KEY];
  return stored && stored.snapshot ? stored.snapshot : null;
}

/** (Re)create the polling alarm when it is missing or its period changed. */
export async function ensureAlarm(chromeApi, periodInMinutes) {
  const existing = await chromeApi.alarms.get(ALARM_NAME);
  if (existing && existing.periodInMinutes === periodInMinutes) return false;
  await chromeApi.alarms.clear(ALARM_NAME);
  await chromeApi.alarms.create(ALARM_NAME, { periodInMinutes, delayInMinutes: periodInMinutes });
  return true;
}
