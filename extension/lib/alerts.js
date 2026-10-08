// Desktop notifications: one per *transition* of an alert condition, never one
// per poll. The conditions of the last poll are persisted (lib/poller.js), so a
// browser restart or a server outage in between does not repeat a notification.

import { relativeTime, truncate } from './format.js';
import { effectiveStale } from './status.js';

export const NOTIFICATION_PREFIX = 'tradebot-';
export const NOTIFY_FAILURES_AT = 2;
export const NOTIFY_DATA_FAULTS_AT = 2;

/** The alertable conditions of a bot status body (all booleans). */
export function alertConditions(bot) {
  return {
    halted: !!bot.halted,
    flatten: !!bot.flatten_requested,
    stale: effectiveStale(bot),
    failures: (bot.consecutive_failures || 0) >= NOTIFY_FAILURES_AT,
    dataFaults: (bot.consecutive_data_faults || 0) >= NOTIFY_DATA_FAULTS_AT,
  };
}

const NONE = Object.freeze({ halted: false, flatten: false, stale: false, failures: false, dataFaults: false });

/**
 * Notifications for the transitions from `prev` to `next` conditions.
 * `prev` null means "first observation of this server and account": conditions
 * that are already true are reported once (e.g. the bot was halted while the
 * browser was closed), but "flatten complete" needs a pending flatten seen first.
 * `prefs` is settings.notify ({halted, flatten, stale, failures, dataFaults}).
 * Each notification: { id, kind, title, message, critical }.
 */
export function computeNotifications(prev, next, bot, prefs = {}, now = Date.now()) {
  const p = prev || NONE;
  const on = (kind) => prefs[kind] !== false;
  const out = [];
  const reason = bot.halt_reason ? truncate(String(bot.halt_reason).replace(/^FLATTEN:\s*/, ''), 160) : '';

  if (next.flatten && !p.flatten) {
    if (on('flatten')) {
      out.push({
        kind: 'flatten', critical: true,
        title: 'Panic flatten requested',
        message: `The bot is halted and closing every position at market${reason ? ` (${reason})` : ''}.`,
      });
    }
  } else if (!next.flatten && prev && prev.flatten) {
    if (on('flatten')) {
      const open = bot.open_positions || 0;
      out.push({
        kind: 'flatten', critical: false,
        title: 'Flatten complete',
        message: open
          ? `${open} position(s) are still open: check the dashboard.`
          : 'All positions are closed. The bot stays halted until you reset it in the dashboard.',
      });
    }
  }
  // A new flatten already says the bot is halted: one notification is enough.
  if (next.halted && !p.halted && !(next.flatten && !p.flatten) && on('halted')) {
    out.push({
      kind: 'halted', critical: true,
      title: 'Trading bot HALTED',
      message: reason ? `Reason: ${reason}` : 'The circuit breaker stopped the bot. Review it in the dashboard.',
    });
  }
  if (next.stale && !p.stale && on('stale')) {
    out.push({
      kind: 'stale', critical: false,
      title: 'Trading bot is not cycling',
      message: bot.last_cycle_at
        ? `No cycle since ${relativeTime(bot.last_cycle_at, now)}: stops and exits are not being managed. Is the bot loop running?`
        : 'No trading cycle has run yet. Is the bot loop running?',
    });
  }
  if (next.failures && !p.failures && on('failures')) {
    out.push({
      kind: 'failures', critical: true,
      title: `${bot.consecutive_failures} failed trading cycles in a row`,
      message: bot.last_error ? truncate(bot.last_error, 200) : 'Check the server log.',
    });
  }
  if (next.dataFaults && !p.dataFaults && on('dataFaults')) {
    out.push({
      kind: 'dataFaults', critical: false,
      title: `Data fault for ${bot.consecutive_data_faults} cycles`,
      message: 'No trustworthy price for a held position: entries are paused and circuit breakers frozen until prices return.',
    });
  }
  return out.map((n) => ({ ...n, id: `${NOTIFICATION_PREFIX}${n.kind}` }));
}
