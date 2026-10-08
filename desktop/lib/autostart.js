'use strict';
/**
 * "Start at login" on Linux. Electron's app.setLoginItemSettings only covers
 * Windows and macOS; on Linux the freedesktop.org way is a .desktop file in
 * $XDG_CONFIG_HOME/autostart. Only offered for the AppImage, whose path
 * ($APPIMAGE) is stable.
 */
const path = require('node:path');

const AUTOSTART_FILE = 'ai-trading-bot.desktop';

function autostartPath(env, homedir) {
  const configHome = env.XDG_CONFIG_HOME && path.isAbsolute(env.XDG_CONFIG_HOME) ? env.XDG_CONFIG_HOME : path.join(homedir, '.config');
  return path.join(configHome, 'autostart', AUTOSTART_FILE);
}

/** Quotes one Exec= argument per the Desktop Entry Specification. */
function quoteExecArg(arg) {
  const text = String(arg).replace(/%/g, '%%');
  if (!/[\s"'\\><~|&;$*?#()`]/.test(text) && text !== '') return text;
  return `"${text.replace(/(["`$\\])/g, '\\$1')}"`;
}

function desktopEntry({ name, exec, args = [], comment = '' }) {
  const execLine = [exec, ...args].map(quoteExecArg).join(' ');
  return [
    '[Desktop Entry]',
    'Type=Application',
    `Name=${name}`,
    comment ? `Comment=${comment}` : null,
    `Exec=${execLine}`,
    'Terminal=false',
    'X-GNOME-Autostart-enabled=true',
    '',
  ]
    .filter((line) => line !== null)
    .join('\n');
}

module.exports = { AUTOSTART_FILE, autostartPath, quoteExecArg, desktopEntry };
