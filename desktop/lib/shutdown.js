'use strict';
/**
 * How long the shell waits for the sidecar to stop. The sidecar's worst case
 * (backend/desktop.py shutdown_deadline()) is a trading cycle or bot operation that
 * runs into BOT_STOP_TIMEOUT (150 s), 15 s for its cancellation, up to 30 s for the
 * HTTP server, 5 s for leftover tasks and 10 s for blocking calls in worker threads:
 * 210 s. POST /api/desktop/shutdown answers with that number (deadline_seconds); the
 * shell waits that long plus a margin, and only then kills the process.
 */

const DEFAULT_STOP_DEADLINE_S = 210;
const MAX_STOP_DEADLINE_S = 3600;
const STOP_MARGIN_MS = 20_000;

/** The deadline the sidecar announced (seconds), or the default one. */
function stopDeadlineSeconds(answer) {
  const seconds = Number(answer && answer.deadline_seconds);
  if (!Number.isFinite(seconds) || seconds <= 0) return DEFAULT_STOP_DEADLINE_S;
  return Math.min(Math.ceil(seconds), MAX_STOP_DEADLINE_S);
}

/** Milliseconds to wait for the sidecar's exit before killing it. */
function shutdownWaitMs(answer) {
  return stopDeadlineSeconds(answer) * 1000 + STOP_MARGIN_MS;
}

/** 210 → "3½ minutes", 150 → "2½ minutes", 60 → "1 minute" (rounded up to half minutes). */
function durationText(seconds) {
  const halves = Math.max(1, Math.ceil(Number(seconds) / 30));
  const whole = Math.floor(halves / 2);
  const half = halves % 2 === 1;
  if (whole === 0) return 'half a minute';
  return `${whole}${half ? '½' : ''} minute${whole === 1 && !half ? '' : 's'}`;
}

/** The "Stopping safely…" detail text, in line with the wait above. */
function stoppingDetail(deadlineSeconds = DEFAULT_STOP_DEADLINE_S) {
  return (
    'Waiting for the trading bot to finish its current cycle and save its state. ' +
    `This usually takes a few seconds, at most about ${durationText(deadlineSeconds)}.`
  );
}

module.exports = {
  DEFAULT_STOP_DEADLINE_S,
  STOP_MARGIN_MS,
  stopDeadlineSeconds,
  shutdownWaitMs,
  durationText,
  stoppingDetail,
};
