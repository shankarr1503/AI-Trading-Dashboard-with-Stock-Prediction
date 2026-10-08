// Display formatting shared by the popup, the options page and the badge title.
// Pure functions: no chrome.* or DOM access.

const DASH = '—';

function isNum(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

/** Parse a server timestamp. The API sends naive ISO strings for SQLite rows: those are UTC. */
export function parseServerTime(value) {
  if (value === null || value === undefined || value === '') return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value === 'number') return Number.isFinite(value) ? new Date(value) : null;   // epoch ms
  if (typeof value !== 'string') return null;
  let s = value.trim();
  // "2026-10-08T12:00:00" or "2026-10-08 12:00:00.123456" without an offset → UTC.
  if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(s)) s = `${s.replace(' ', 'T')}Z`;
  // JavaScript only understands milliseconds: trim microseconds.
  s = s.replace(/(\.\d{3})\d+/, '$1');
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "just now", "5 min ago", "3 h ago", "2 d ago" (or "in 5 min" for future times). */
export function relativeTime(value, now = Date.now()) {
  const d = parseServerTime(value);
  if (!d) return 'never';
  const seconds = Math.round((now - d.getTime()) / 1000);
  const future = seconds < 0;
  const abs = Math.abs(seconds);
  let text;
  if (abs < 45) return future ? 'in a moment' : 'just now';
  if (abs < 90 * 60) text = `${Math.max(1, Math.round(abs / 60))} min`;
  else if (abs < 36 * 3600) text = `${Math.round(abs / 3600)} h`;
  else text = `${Math.round(abs / 86400)} d`;
  return future ? `in ${text}` : `${text} ago`;
}

/** 12345.6 → "$12,345.60" (no symbol for null currency). */
export function money(v, { currency = 'USD', digits = 2, signed = false } = {}) {
  if (!isNum(v)) return DASH;
  const sign = signed && v > 0 ? '+' : v < 0 ? '−' : '';
  const body = Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  const symbol = currency === 'USD' ? '$' : '';
  const suffix = currency && currency !== 'USD' ? ` ${currency}` : '';
  return `${sign}${symbol}${body}${suffix}`;
}

/** Plain price/number: 189.2345 → "189.23". */
export function num(v, digits = 2) {
  if (!isNum(v)) return DASH;
  return v.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** Percent values that are already in percent units: 1.234 → "1.23%" (signed: "+1.23%"). */
export function pct(v, { digits = 2, signed = false } = {}) {
  if (!isNum(v)) return DASH;
  const sign = signed && v > 0 ? '+' : v < 0 ? '−' : '';
  return `${sign}${Math.abs(v).toFixed(digits)}%`;
}

/** Share quantities: whole numbers without decimals, fractional shares with up to 4. */
export function qty(v) {
  if (!isNum(v)) return DASH;
  return Number.isInteger(v) ? v.toLocaleString('en-US') : v.toLocaleString('en-US', { maximumFractionDigits: 4 });
}

/** CSS colour class for a signed value. */
export function signClass(v) {
  if (!isNum(v) || v === 0) return 'flat';
  return v > 0 ? 'up' : 'down';
}

/** Human trading mode. */
export function modeLabel(mode) {
  switch (mode) {
    case 'paper': return 'PAPER';
    case 'alpaca_paper': return 'ALPACA PAPER';
    case 'alpaca_live': return 'LIVE';
    default: return mode ? String(mode).toUpperCase() : DASH;
  }
}

/** Shorten long server-provided text for notifications and titles. */
export function truncate(text, max = 160) {
  if (text === null || text === undefined) return '';
  const s = String(text).replace(/\s+/g, ' ').trim();
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

/** "next_earnings" may be an ISO date or null; show "2026-10-30" or "Oct 30 – Nov 3". */
export function dateRange(start, end) {
  if (!start) return DASH;
  const fmt = (s) => {
    const d = new Date(`${String(s).slice(0, 10)}T00:00:00Z`);
    if (Number.isNaN(d.getTime())) return String(s);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
  };
  if (!end || String(end).slice(0, 10) === String(start).slice(0, 10)) return fmt(start);
  return `${fmt(start)} – ${fmt(end)} (unconfirmed)`;
}

export { DASH };
