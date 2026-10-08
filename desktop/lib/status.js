'use strict';
/** Texts the tray and the quit flow derive from GET /api/desktop/info. */

const MODE_LABELS = {
  paper: 'paper trading',
  alpaca_paper: 'Alpaca paper',
  alpaca_live: 'LIVE trading',
};

function modeLabel(mode) {
  return MODE_LABELS[mode] || String(mode || 'unknown mode');
}

function plural(count, word) {
  return `${count} ${word}${count === 1 ? '' : 's'}`;
}

/** One line for the tray menu / tooltip. */
function formatStatus(phase, info) {
  if (phase === 'starting') return 'Bot server starting…';
  if (phase === 'stopping') return 'Bot server stopping…';
  if (phase !== 'running') return 'Bot server stopped';
  if (!info) return 'Bot server running';
  const bot = info.halted ? 'Bot halted' : info.bot_enabled ? 'Bot running' : 'Bot paused';
  const positions = Number.isFinite(Number(info.open_positions)) ? Number(info.open_positions) : 0;
  return `${bot} · ${modeLabel(info.trading_mode)} · ${plural(positions, 'open position')}`;
}

/**
 * The warning shown before quitting, or null. Positions of the built-in paper
 * simulator are only watched (stop-loss / take-profit) while the app runs;
 * broker-held positions keep their protective orders at the broker.
 */
function quitWarning(info) {
  if (!info || info.trading_mode !== 'paper') return null;
  const count = Number(info.open_positions);
  if (!Number.isFinite(count) || count <= 0) return null;
  return {
    message: `You have ${plural(count, 'open paper position')}.`,
    detail:
      "Paper positions are simulated by this app: their stop-loss and take-profit levels are only " +
      'checked while the app is running. While it is closed nothing protects them, and they are ' +
      'only re-evaluated when you start the app again.\n\n' +
      'To keep them protected, close the window instead of quitting: the app keeps running in the ' +
      'background.',
  };
}

function describeExit(code, signal) {
  if (signal) return `stopped by signal ${signal}`;
  if (code === null || code === undefined) return 'exited';
  return `exit code ${code}`;
}

module.exports = { formatStatus, quitWarning, modeLabel, describeExit };
