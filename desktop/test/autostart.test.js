'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { autostartPath, desktopEntry, quoteExecArg } = require('../lib/autostart');

test('autostartPath follows XDG_CONFIG_HOME', () => {
  assert.equal(autostartPath({}, '/home/me'), path.join('/home/me', '.config', 'autostart', 'ai-trading-bot.desktop'));
  assert.equal(autostartPath({ XDG_CONFIG_HOME: '/cfg' }, '/home/me'), path.join('/cfg', 'autostart', 'ai-trading-bot.desktop'));
  assert.equal(autostartPath({ XDG_CONFIG_HOME: 'relative' }, '/home/me'), path.join('/home/me', '.config', 'autostart', 'ai-trading-bot.desktop'));
});

test('quoteExecArg follows the Desktop Entry quoting rules', () => {
  assert.equal(quoteExecArg('/opt/app/AI-Trading-Bot.AppImage'), '/opt/app/AI-Trading-Bot.AppImage');
  assert.equal(quoteExecArg('/home/me/My Apps/AI Trading Bot.AppImage'), '"/home/me/My Apps/AI Trading Bot.AppImage"');
  assert.equal(quoteExecArg('a"b$c`d\\e'), '"a\\"b\\$c\\`d\\\\e"');
  assert.equal(quoteExecArg('100%'), '100%%');
  assert.equal(quoteExecArg(''), '""');
});

test('desktopEntry', () => {
  const entry = desktopEntry({ name: 'AI Trading Bot', exec: '/home/me/Apps/AI Trading Bot.AppImage', args: ['--hidden'], comment: 'c' });
  assert.equal(
    entry,
    '[Desktop Entry]\nType=Application\nName=AI Trading Bot\nComment=c\nExec="/home/me/Apps/AI Trading Bot.AppImage" --hidden\nTerminal=false\nX-GNOME-Autostart-enabled=true\n',
  );
});
