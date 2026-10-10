'use strict';
/**
 * The pairing code of this installation (<data dir>/pairing.key, written by the
 * sidecar: backend/desktop.py ensure_pairing_secret). The tray copies it for the
 * Chrome extension, which uses it to check that the server on its configured
 * address really is this app before it sends a password or a token
 * (GET /api/desktop/pair). Also the X-Desktop-Token rule for the app window.
 */
const fs = require('node:fs');
const path = require('node:path');

const PAIRING_FILE = 'pairing.key';
const PAIRING_RE = /^[A-Z2-7]{20,}$/;

/** The stored code (upper case, no separators), or null when there is none (yet). */
function readPairingCode(dataDir, fileSystem = fs) {
  if (!dataDir) return null;
  let text;
  try {
    text = fileSystem.readFileSync(path.join(dataDir, PAIRING_FILE), 'utf8');
  } catch {
    return null;
  }
  const code = String(text).trim().toUpperCase();
  return PAIRING_RE.test(code) ? code : null;
}

/** "ABCDEFGHIJKLMNOPQRST" → "ABCD-EFGH-IJKL-MNOP-QRST" (the extension ignores the dashes). */
function formatPairingCode(code) {
  return String(code).match(/.{1,4}/g).join('-');
}

/**
 * Whether the shell adds its control token (X-Desktop-Token) to a request: only the
 * app window's own POST /auth/register to the bot server's origin. While no account
 * exists the server requires it, so only the app's owner can create the first
 * (administrator) account, never a web page or another program on the computer.
 */
function addsDesktopToken({ url, method, appUrl, fromAppWindow }) {
  if (!fromAppWindow || !appUrl || String(method).toUpperCase() !== 'POST') return false;
  let target;
  let app;
  try {
    target = new URL(String(url));
    app = new URL(String(appUrl));
  } catch {
    return false;
  }
  return target.origin === app.origin && target.pathname === '/auth/register' && !target.username && !target.password;
}

module.exports = { PAIRING_FILE, readPairingCode, formatPairingCode, addsDesktopToken };
