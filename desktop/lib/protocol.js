'use strict';
/**
 * The sidecar's stdout protocol (see backend/desktop.py): one line each, flushed,
 *
 *   TRADEBOT_READY {"url": "http://127.0.0.1:47821", "port": 47821, "data_dir": "...", "version": "..."}
 *   TRADEBOT_ERROR <message>
 *
 * Pipes deliver arbitrary chunks, so LineBuffer reassembles lines across chunk
 * boundaries (including a UTF-8 character split between two chunks).
 */
const { StringDecoder } = require('node:string_decoder');

const READY = 'TRADEBOT_READY';
const ERROR = 'TRADEBOT_ERROR';
const MAX_LINE_LENGTH = 64 * 1024;
// The hosts the sidecar's TrustedHostMiddleware accepts.
const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost']);

class LineBuffer {
  constructor({ maxLineLength = MAX_LINE_LENGTH } = {}) {
    this._decoder = new StringDecoder('utf8');
    this._pending = '';
    this._max = maxLineLength;
  }

  /** Feed a chunk (Buffer or string); returns the lines it completed, without line terminators. */
  push(chunk) {
    this._pending += typeof chunk === 'string' ? chunk : this._decoder.write(chunk);
    const lines = this._pending.split('\n');
    this._pending = lines.pop();
    if (this._pending.length > this._max) {
      // A runaway line without a newline: hand it over rather than buffer forever.
      lines.push(this._pending);
      this._pending = '';
    }
    return lines.map(stripCR);
  }

  /** The stream ended: returns the unterminated last line, if any. */
  end() {
    const rest = this._pending + this._decoder.end();
    this._pending = '';
    return rest ? [stripCR(rest)] : [];
  }
}

function stripCR(line) {
  return line.endsWith('\r') ? line.slice(0, -1) : line;
}

/**
 * Checks a TRADEBOT_READY payload. The URL must be plain http on a loopback
 * address with the reported port: the window loads it, so it must never point
 * anywhere else.
 */
function validateReadyInfo(info) {
  if (!info || typeof info !== 'object' || Array.isArray(info)) {
    return { ok: false, reason: 'the payload is not a JSON object' };
  }
  const port = info.port;
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    return { ok: false, reason: `invalid port ${JSON.stringify(port)}` };
  }
  let url;
  try {
    url = new URL(String(info.url));
  } catch {
    return { ok: false, reason: `invalid url ${JSON.stringify(info.url)}` };
  }
  if (url.protocol !== 'http:' || !LOOPBACK_HOSTS.has(url.hostname)) {
    return { ok: false, reason: `the url ${url.href} is not a local http address` };
  }
  if (Number(url.port || 80) !== port) {
    return { ok: false, reason: `the url ${url.href} does not match port ${port}` };
  }
  return {
    ok: true,
    info: {
      url: url.origin,
      port,
      dataDir: typeof info.data_dir === 'string' ? info.data_dir : null,
      version: typeof info.version === 'string' ? info.version : null,
    },
  };
}

/**
 * Parses one stdout line. Returns
 *   {type: 'ready', info: {url, port, dataDir, version}}
 *   {type: 'error', message}
 *   {type: 'invalid', reason, line}    a READY line we cannot use
 *   null                               anything else (not part of the protocol)
 */
function parseProtocolLine(line) {
  const text = String(line).replace(/^﻿/, '').trim();
  const kind = text.startsWith(READY) ? READY : text.startsWith(ERROR) ? ERROR : null;
  if (!kind) return null;
  const rest = text.slice(kind.length);
  if (rest && !/^\s/.test(rest)) return null; // e.g. TRADEBOT_READYNESS: not ours
  const payload = rest.trim();
  if (kind === ERROR) {
    return { type: 'error', message: payload || 'Unknown startup error' };
  }
  let info;
  try {
    info = JSON.parse(payload);
  } catch {
    return { type: 'invalid', reason: 'the TRADEBOT_READY payload is not valid JSON', line: text };
  }
  const checked = validateReadyInfo(info);
  if (!checked.ok) return { type: 'invalid', reason: checked.reason, line: text };
  return { type: 'ready', info: checked.info };
}

module.exports = { LineBuffer, parseProtocolLine, validateReadyInfo, MAX_LINE_LENGTH };
