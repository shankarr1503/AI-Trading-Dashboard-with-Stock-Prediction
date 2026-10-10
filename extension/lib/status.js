// What the toolbar badge, its tooltip and the popup's state line show.
// Pure functions over a "snapshot" collected by lib/poller.js:
//
//   { phase: 'disconnected' | 'no_access' | 'unpaired' | 'untrusted' | 'signed_out' | 'user' | 'admin',
//     server, user, bot (GET /api/bot/status body, admins only), error, at }

import { modeLabel, money, pct, relativeTime, truncate } from './format.js';

const DARK = '#0a0b0d';
const WHITE = '#ffffff';

// key → badge text / colours / popup label. Badge text is at most 4 characters.
export const BADGES = Object.freeze({
  running:      { text: 'RUN',  color: '#00d4aa', textColor: DARK,  label: 'RUNNING' },
  paused:       { text: 'OFF',  color: '#4fa3ff', textColor: DARK,  label: 'PAUSED' },
  halted:       { text: 'HALT', color: '#ff4757', textColor: WHITE, label: 'HALTED' },
  flatten:      { text: 'FLAT', color: '#ff4757', textColor: WHITE, label: 'FLATTEN PENDING' },
  data_fault:   { text: 'DATA', color: '#ffa502', textColor: DARK,  label: 'DATA FAULT' },
  failing:      { text: 'ERR',  color: '#ffa502', textColor: DARK,  label: 'FAILING' },
  stale:        { text: 'LATE', color: '#ffa502', textColor: DARK,  label: 'STALE' },
  disconnected: { text: 'DOWN', color: '#5a6478', textColor: WHITE, label: 'DISCONNECTED' },
  no_access:    { text: '!',    color: '#ffa502', textColor: DARK,  label: 'NO ACCESS' },
  unpaired:     { text: 'PAIR', color: '#ffa502', textColor: DARK,  label: 'NOT PAIRED' },
  untrusted:    { text: '!!',   color: '#ff4757', textColor: WHITE, label: 'UNVERIFIED SERVER' },
  signed_out:   { text: '?',    color: '#5a6478', textColor: WHITE, label: 'NOT SIGNED IN' },
  user:         { text: '',     color: '#5a6478', textColor: WHITE, label: 'CONNECTED' },
});

export const FAILURE_THRESHOLD = 2;      // consecutive failed cycles worth a warning (the server alerts at 2 too)
export const DATA_FAULT_THRESHOLD = 1;   // badge shows a data fault from the first cycle; notifications wait for 2

/**
 * The server reports `stale` whenever no cycle ran recently, including when
 * the bot loop never ran at all (e.g. a server started without the bot). That
 * only matters while the loop has work to do: the bot is enabled, positions are
 * open (their stops are managed every cycle) or a flatten is pending.
 */
export function effectiveStale(bot) {
  if (!bot || !bot.stale) return false;
  return !!(bot.enabled || (bot.open_positions || 0) > 0 || bot.flatten_requested);
}

/** Primary state plus health flags of a bot status body. */
export function botView(bot) {
  const primary = bot.flatten_requested ? 'flatten' : bot.halted ? 'halted' : bot.enabled ? 'running' : 'paused';
  const health = {
    stale: effectiveStale(bot),
    dataFault: (bot.consecutive_data_faults || 0) >= DATA_FAULT_THRESHOLD,
    failing: (bot.consecutive_failures || 0) >= FAILURE_THRESHOLD,
    configError: bot.config_error || null,
  };
  // Most severe first: a halt outranks health warnings, which outrank running/paused.
  let key = primary;
  if (primary !== 'flatten' && primary !== 'halted') {
    if (health.dataFault) key = 'data_fault';
    else if (health.failing) key = 'failing';
    else if (health.stale) key = 'stale';
  }
  return { primary, key, health };
}

/** Warning chips for the popup, in display order. */
export function botWarnings(bot, now = Date.now()) {
  const { health } = botView(bot);
  const out = [];
  if (health.stale) {
    out.push({
      key: 'stale',
      label: 'STALE',
      detail: bot.last_cycle_at
        ? `No trading cycle since ${relativeTime(bot.last_cycle_at, now)}: is the bot loop running?`
        : 'No trading cycle has run yet: is the bot loop running?',
    });
  }
  if (health.dataFault) {
    out.push({
      key: 'data_fault',
      label: 'DATA FAULT',
      detail: `${bot.consecutive_data_faults} cycle(s) without a trustworthy price for a held position: entries paused, breakers frozen.`,
    });
  }
  if (health.failing) {
    out.push({
      key: 'failing',
      label: 'FAILING',
      detail: `${bot.consecutive_failures} consecutive failed cycles${bot.last_error ? `: ${truncate(bot.last_error, 140)}` : ''}`,
    });
  }
  if (health.configError) {
    out.push({ key: 'config', label: 'CONFIG', detail: `Invalid risk overrides: ${truncate(health.configError, 140)}` });
  }
  return out;
}

function hostOf(server) {
  try {
    return new URL(server).host;
  } catch {
    return server || '';
  }
}

/**
 * Everything the badge and the popup header need for a snapshot:
 * { key, label, badge: {text,color,textColor}, title, primary, warnings }.
 */
export function describe(snapshot, now = Date.now()) {
  const server = snapshot.server || '';
  const lines = [];
  let key;
  let primary = null;
  let warnings = [];
  switch (snapshot.phase) {
    case 'no_access':
      key = 'no_access';
      lines.push(`No permission to reach ${hostOf(server)}: open the extension options and save the server again.`);
      break;
    case 'unpaired':
      key = 'unpaired';
      lines.push('Not paired with the desktop app: copy the pairing code from the app\'s tray menu into the extension options.');
      break;
    case 'untrusted':
      key = 'untrusted';
      lines.push(`The server at ${hostOf(server)} could not prove it is your AI Trading Bot app: nothing is sent to it.`);
      if (snapshot.error) lines.push(truncate(snapshot.error, 160));
      break;
    case 'signed_out':
      key = 'signed_out';
      lines.push('Not signed in: open the extension options to sign in.');
      break;
    case 'user':
      key = 'user';
      lines.push(`Connected as ${snapshot.user ? snapshot.user.username : 'user'}.`);
      lines.push('Bot status and controls need an administrator account.');
      break;
    case 'admin': {
      const bot = snapshot.bot || {};
      const view = botView(bot);
      key = view.key;
      primary = view.primary;
      warnings = botWarnings(bot, now);
      lines.push(`${BADGES[view.primary].label} (${modeLabel(bot.mode)})`);
      if (bot.halt_reason && (view.primary === 'halted' || view.primary === 'flatten')) {
        lines.push(truncate(bot.halt_reason, 120));
      }
      for (const w of warnings) lines.push(`${w.label}: ${w.detail}`);
      lines.push(`Equity ${money(bot.equity)} · drawdown ${pct(bot.drawdown_pct)}`);
      lines.push(`Open positions ${bot.open_positions ?? 0} · last cycle ${relativeTime(bot.last_cycle_at, now)}`);
      break;
    }
    case 'disconnected':
    default:
      key = 'disconnected';
      lines.push(`Server not reachable at ${server}.`);
      if (snapshot.error) lines.push(truncate(snapshot.error, 140));
      lines.push('Is the desktop app running? Its tray menu shows the server URL.');
      break;
  }
  const badge = BADGES[key];
  const heading = ['user', 'signed_out', 'disconnected', 'no_access', 'unpaired', 'untrusted'].includes(key)
    ? `AI Trading Bot: ${badge.label}`
    : 'AI Trading Bot';
  const title = [heading, ...lines, server ? `Server: ${server}` : ''].filter(Boolean).join('\n');
  return {
    key,
    label: badge.label,
    badge: { text: badge.text, color: badge.color, textColor: badge.textColor },
    title,
    primary,
    warnings,
  };
}
