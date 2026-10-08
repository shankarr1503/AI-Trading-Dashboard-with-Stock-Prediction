'use strict';
/**
 * Where the app keeps its data. The data folder is shared with the sidecar
 * (trading.db, settings.env, secret.key, logs/, backups/) and must be the
 * sidecar's own default (backend/desktop.py default_data_dir), so that the
 * recovery commands (`tradebot-backend create-admin ...`) find the app's
 * database without --data-dir:
 *
 *   Windows  %APPDATA%\AI Trading Bot                    (= Electron's default userData)
 *   macOS    ~/Library/Application Support/AI Trading Bot (= Electron's default userData)
 *   Linux    ~/.local/share/ai-trading-bot                (Electron would use ~/.config/AI Trading Bot)
 *
 * Chromium's own files (caches, cookies, local storage, crash dumps) go into a
 * "browser" subfolder so the data folder stays readable.
 */
const path = require('node:path');

const APP_NAME = 'AI Trading Bot';

function defaultDataDir(platform, env, homedir) {
  if (platform === 'win32') {
    return path.win32.join(env.APPDATA || path.win32.join(homedir, 'AppData', 'Roaming'), APP_NAME);
  }
  if (platform === 'darwin') return path.posix.join(homedir, 'Library', 'Application Support', APP_NAME);
  return path.posix.join(homedir, '.local', 'share', 'ai-trading-bot');
}

function browserDataDir(userData) {
  return path.join(userData, 'browser');
}

module.exports = { APP_NAME, defaultDataDir, browserDataDir };
