// Pairing with the desktop app.
//
// The desktop app's server listens on this computer (http://127.0.0.1:47821, or the
// next free port up to 47841). Any other program on the computer, including one run
// by another user, can listen on such a port too and pose as the app. So before the
// extension sends a password or a session token to a server on this computer, it
// asks the server to prove it knows the app's pairing code: the extension sends a
// random nonce to GET /api/desktop/pair and compares the answer with
//
//     HMAC-SHA256(pairing code, "tradebot-pair-v1\n<server URL>\n<nonce>")
//
// computed here (backend/main.py pairing_proof). The server URL is part of the
// message, so an impostor on 47821 cannot relay the challenge to the real app on
// 47822 and pass its answer off as its own. The user copies the code once from the
// app's tray menu (Copy Pairing Code for the Chrome Extension). Servers on other
// machines are reached over https:// and authenticated by TLS instead.

import { DESKTOP_PORTS, isLoopbackHost } from './server.js';

export const PAIRING_CONTEXT = 'tradebot-pair-v1';
export const PAIR_TTL_MS = 60_000;        // a successful check is reused this long, then repeated
const CODE_RE = /^[A-Z2-7]{20,64}$/;

export class PairingError extends Error {
  /** kind: 'unpaired' (no code entered), 'mismatch' (not the paired app), 'network' (no answer). */
  constructor(message, kind) {
    super(message);
    this.name = 'PairingError';
    this.kind = kind;
  }
}

/** "abcd-efgh ijkl…" → "ABCDEFGHIJKL…". Throws PairingError for anything that is not a pairing code. */
export function normalizePairingCode(input) {
  const code = String(input ?? '').toUpperCase().replace(/[\s-]+/g, '');
  if (!CODE_RE.test(code)) {
    throw new PairingError(
      'That is not a pairing code: copy it from the desktop app (tray menu → Copy Pairing Code for the Chrome Extension)',
      'unpaired');
  }
  return code;
}

/** Same as normalizePairingCode, but '' for an empty or invalid value. */
export function tryNormalizePairingCode(input) {
  try {
    return normalizePairingCode(input);
  } catch {
    return '';
  }
}

function loopbackHttp(baseUrl) {
  try {
    const url = new URL(baseUrl);
    return url.protocol === 'http:' && isLoopbackHost(url.hostname) ? url : null;
  } catch {
    return null;
  }
}

/**
 * Whether credentials may only go to `baseUrl` after a pairing check: always for the
 * desktop app's ports on this computer, and for every server on this computer once a
 * pairing code is entered (the app may have been moved to another port).
 */
export function pairingRequired(baseUrl, code) {
  const url = loopbackHttp(baseUrl);
  if (!url) return false;
  if (code) return true;
  return DESKTOP_PORTS.includes(Number(url.port || 80));
}

function hex(buffer) {
  return Array.from(new Uint8Array(buffer), (b) => b.toString(16).padStart(2, '0')).join('');
}

export function randomNonce(cryptoImpl = globalThis.crypto) {
  const bytes = new Uint8Array(32);
  cryptoImpl.getRandomValues(bytes);
  return hex(bytes);
}

/** hex HMAC-SHA256(code, "tradebot-pair-v1\n<serverUrl>\n<nonce>"). */
export async function pairingProof(code, serverUrl, nonce, subtle = globalThis.crypto.subtle) {
  const enc = new TextEncoder();
  const key = await subtle.importKey('raw', enc.encode(code), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const mac = await subtle.sign('HMAC', key, enc.encode(`${PAIRING_CONTEXT}\n${serverUrl}\n${nonce}`));
  return hex(mac);
}

/** Compares two strings without stopping at the first difference. */
export function sameText(a, b) {
  const x = String(a);
  const y = String(b);
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i += 1) {
    diff |= (x.charCodeAt(i) || 0) ^ (y.charCodeAt(i) || 0);
  }
  return diff === 0;
}

/**
 * Challenge the server at `baseUrl` once. Resolves true when it proved it knows
 * `code`; rejects with a PairingError otherwise.
 */
export async function verifyServer({ baseUrl, code, fetchImpl, timeoutMs = 8000 }) {
  if (!code) {
    throw new PairingError(
      'Pair the extension with the desktop app first: paste the pairing code from the app\'s tray menu '
      + '(Copy Pairing Code for the Chrome Extension) into the extension\'s options', 'unpaired');
  }
  const doFetch = fetchImpl || globalThis.fetch.bind(globalThis);
  const nonce = randomNonce();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let res;
  let text;
  try {
    res = await doFetch(`${baseUrl}/api/desktop/pair?nonce=${nonce}`, {
      method: 'GET', headers: { Accept: 'application/json' }, signal: controller.signal,
      credentials: 'omit', cache: 'no-store', redirect: 'error',
    });
    text = await res.text();
  } catch {
    throw new PairingError(`Cannot reach the server at ${baseUrl}`, 'network');
  } finally {
    clearTimeout(timer);
  }
  const notTheApp = (why) => new PairingError(
    `The server at ${baseUrl} could not prove it is your AI Trading Bot app (${why}). Nothing was sent to it. `
    + 'If the app runs on another port, use "Find the desktop app"; otherwise another program may be posing as it.',
    'mismatch');
  if (!res.ok) throw notTheApp(res.status === 404 ? 'it has no pairing check' : `HTTP ${res.status}`);
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    throw notTheApp('unexpected answer');
  }
  if (!body || typeof body !== 'object' || typeof body.proof !== 'string') throw notTheApp('unexpected answer');
  if (body.server !== baseUrl) {
    const hint = loopbackHttp(body.server) ? `: it is the app at ${body.server}, use that address` : '';
    throw new PairingError(`The server at ${baseUrl} answers for another address${hint}. Nothing was sent to it.`,
      'mismatch');
  }
  const expected = await pairingProof(code, baseUrl, nonce);
  if (!sameText(body.proof.toLowerCase(), expected)) throw notTheApp('wrong pairing code');
  return true;
}

// Successful checks per server and code: { at } (and the check in flight, shared).
const verified = new Map();
const inFlight = new Map();

/** Forget every successful check (e.g. after the pairing code changed). */
export function forgetPairings() {
  verified.clear();
}

/**
 * Resolves when `baseUrl` may receive credentials: no check needed, or a successful
 * check within PAIR_TTL_MS (force: always check now). Rejects with a PairingError.
 */
export async function ensurePaired({ baseUrl, code, fetchImpl, now = () => Date.now(), force = false }) {
  if (!pairingRequired(baseUrl, code)) return 'not-required';
  const key = `${baseUrl}\n${code || ''}`;
  const last = verified.get(key);
  if (!force && last && now() - last < PAIR_TTL_MS && now() >= last) return 'cached';
  let pending = inFlight.get(key);
  if (!pending) {
    pending = verifyServer({ baseUrl, code, fetchImpl })
      .then(() => {
        verified.set(key, now());
        return 'verified';
      }, (e) => {
        verified.delete(key);
        throw e;
      })
      .finally(() => inFlight.delete(key));
    inFlight.set(key, pending);
  }
  return pending;
}
