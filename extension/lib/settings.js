// User settings (chrome.storage.local "settings") with defaults and validation.

import { tryNormalizePairingCode } from './pairing.js';
import { DEFAULT_SERVER_URL, tryNormalizeServerUrl } from './server.js';

export const SETTINGS_KEY = 'settings';
export const MIN_POLL_MINUTES = 1;
export const MAX_POLL_MINUTES = 15;

export const NOTIFY_KINDS = Object.freeze({
  halted: 'Bot halted (circuit breaker or manual halt)',
  flatten: 'Panic flatten requested / completed',
  stale: 'Bot loop stale (no recent cycle while it has work to do)',
  failures: 'Repeated failed cycles',
  dataFaults: 'Data faults (no trustworthy price for held positions)',
});

export function defaultSettings() {
  return {
    serverUrl: DEFAULT_SERVER_URL,
    pairingCode: '',     // the desktop app's pairing code (lib/pairing.js)
    pollMinutes: 1,
    notify: Object.fromEntries(Object.keys(NOTIFY_KINDS).map((k) => [k, true])),
  };
}

export function clampPollMinutes(value) {
  const n = Math.round(Number(value));
  if (!Number.isFinite(n)) return defaultSettings().pollMinutes;
  return Math.min(MAX_POLL_MINUTES, Math.max(MIN_POLL_MINUTES, n));
}

/** Merge stored values over the defaults, dropping anything invalid. */
export function sanitizeSettings(stored) {
  const base = defaultSettings();
  const s = stored && typeof stored === 'object' ? stored : {};
  const notify = { ...base.notify };
  if (s.notify && typeof s.notify === 'object') {
    for (const k of Object.keys(notify)) {
      if (typeof s.notify[k] === 'boolean') notify[k] = s.notify[k];
    }
  }
  return {
    serverUrl: tryNormalizeServerUrl(s.serverUrl) || base.serverUrl,
    pairingCode: tryNormalizePairingCode(s.pairingCode),
    pollMinutes: s.pollMinutes === undefined ? base.pollMinutes : clampPollMinutes(s.pollMinutes),
    notify,
  };
}

export async function getSettings(chromeApi = globalThis.chrome) {
  const got = await chromeApi.storage.local.get(SETTINGS_KEY);
  return sanitizeSettings(got[SETTINGS_KEY]);
}

/** Shallow-merge `partial` into the stored settings (notify is merged per key). */
export async function saveSettings(partial, chromeApi = globalThis.chrome) {
  const current = await getSettings(chromeApi);
  const next = sanitizeSettings({
    ...current,
    ...partial,
    notify: { ...current.notify, ...(partial && partial.notify) },
  });
  await chromeApi.storage.local.set({ [SETTINGS_KEY]: next });
  return next;
}
