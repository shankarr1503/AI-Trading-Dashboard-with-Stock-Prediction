// Service worker: polls the server on an alarm (every 1–15 minutes), keeps the
// toolbar badge current and raises notifications for new alert conditions.
// All logic lives in lib/poller.js; this file only wires Chrome events to it.

import { ApiClient, AUTH_KEY, createTokenStore } from './lib/api.js';
import { ALARM_NAME, ensureAlarm, runPoll } from './lib/poller.js';
import { dashboardUrl } from './lib/server.js';
import { getSettings, SETTINGS_KEY } from './lib/settings.js';
import { NOTIFICATION_PREFIX } from './lib/alerts.js';

let inFlight = null;

/** One poll at a time: alarms, popup requests and setting changes share it. */
function poll() {
  if (!inFlight) {
    inFlight = runPoll()
      .catch((e) => {
        console.warn('Poll failed', e);
        return null;
      })
      .finally(() => {
        inFlight = null;
      });
  }
  return inFlight;
}

async function schedule() {
  const { pollMinutes } = await getSettings();
  await ensureAlarm(chrome, pollMinutes);
}

chrome.runtime.onInstalled.addListener(async (details) => {
  await schedule();
  await poll();
  if (details.reason === 'install') {
    chrome.runtime.openOptionsPage().catch(() => {});
  }
});

chrome.runtime.onStartup.addListener(async () => {
  await schedule();
  await poll();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) poll();
});

/** Sign-in, sign-out or another account: not a mere token rotation (which every refresh writes). */
function accountChanged(change) {
  const a = change.oldValue;
  const b = change.newValue;
  if (!a || !b) return !!a !== !!b;
  return a.server !== b.server || (a.user && a.user.id) !== (b.user && b.user.id)
    || (a.user && a.user.is_superuser) !== (b.user && b.user.is_superuser);
}

// Settings or sign-in changed (options page): re-schedule and refresh the badge now.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local') return;
  if (SETTINGS_KEY in changes || (AUTH_KEY in changes && accountChanged(changes[AUTH_KEY]))) {
    schedule().then(poll);
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  // Only this extension's own pages talk to the worker (no content scripts, not externally connectable).
  if (sender.id !== chrome.runtime.id || !message || typeof message !== 'object') return false;
  if (message.type === 'refresh') {
    poll().then((result) => sendResponse(result ? { ok: true, display: result.display, snapshot: result.snapshot }
      : { ok: false }));
    return true;   // async response
  }
  return false;
});

// Clicking a notification opens the bot page of the web dashboard.
chrome.notifications.onClicked.addListener(async (id) => {
  if (!id.startsWith(NOTIFICATION_PREFIX)) return;
  const { serverUrl } = await getSettings();
  await chrome.tabs.create({ url: dashboardUrl(serverUrl, 'bot') });
  chrome.notifications.clear(id);
});

// A worker restarted by Chrome keeps no state: make sure the alarm exists.
schedule().catch(() => {});

// Exposed for debugging from the service worker console (and the end-to-end test).
globalThis.tradebot = { poll, ApiClient, createTokenStore };
