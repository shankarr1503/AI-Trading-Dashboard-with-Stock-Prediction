'use strict';
// The packaging contract (appId, sidecar location, platform targets) and that
// everything main.js loads at runtime is part of the package.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
const build = pkg.build;

test('identity and versions are pinned', () => {
  assert.equal(pkg.name, 'ai-trading-bot-desktop');
  assert.equal(pkg.productName, 'AI Trading Bot');
  assert.equal(build.productName, 'AI Trading Bot');
  assert.equal(build.appId, 'com.aitradingbot.desktop');
  assert.equal(pkg.main, 'main.js');
  assert.match(pkg.version, /^\d+\.\d+\.\d+$/);
  for (const [name, version] of Object.entries(pkg.devDependencies)) assert.match(version, /^\d+\.\d+\.\d+$/, `${name} must be pinned exactly`);
});

test('the sidecar ships as <resources>/backend', () => {
  assert.deepEqual(
    build.extraResources.map(({ from, to }) => ({ from, to })),
    [{ from: '../dist-backend/tradebot-backend', to: 'backend' }],
  );
  assert.equal(build.beforePack, 'scripts/before-pack.js');
  assert.ok(fs.existsSync(path.join(root, build.beforePack)));
});

test('platform targets', () => {
  assert.deepEqual(build.win.target, [{ target: 'nsis', arch: ['x64'] }]);
  assert.equal(build.nsis.oneClick, false);
  assert.equal(build.nsis.perMachine, false);
  assert.equal(build.nsis.allowToChangeInstallationDirectory, true);
  assert.equal(build.nsis.createDesktopShortcut, true);
  assert.equal(build.nsis.createStartMenuShortcut, true);
  assert.deepEqual(build.mac.target, ['dmg', 'zip']);
  assert.equal(build.mac.category, 'public.app-category.finance');
  assert.equal(build.mac.hardenedRuntime, true);
  assert.deepEqual(build.linux.target, ['AppImage']);
  assert.equal(build.directories.buildResources, 'build');
});

test('the Windows installer asks a running app to quit gracefully before replacing it', () => {
  assert.equal(build.nsis.include, 'build/installer.nsh');
  const nsh = fs.readFileSync(path.join(root, build.nsis.include), 'utf8');
  assert.match(nsh, /!macro customCheckAppRunning[\s\S]*!macroend/);
  assert.match(nsh, /Exec `"\$INSTDIR\\\$\{APP_EXECUTABLE_FILENAME\}" --quit`/);
  assert.match(nsh, /!insertmacro _CHECK_APP_RUNNING/); // then electron-builder's own check as the fallback
  // The wait covers the sidecar's worst-case stop (lib/shutdown.js) plus the shell's margin.
  const { shutdownWaitMs } = require('../lib/shutdown');
  const wait = Number(nsh.match(/!define TRADEBOT_QUIT_WAIT_SECONDS (\d+)/)[1]);
  assert.ok(wait * 1000 >= shutdownWaitMs(null), `${wait} s`);
  // main.js turns --quit into a graceful quit of the running instance.
  const main = fs.readFileSync(path.join(root, 'main.js'), 'utf8');
  assert.match(main, /argv\.includes\('--quit'\)\) quitUnattended/);
});

test('the macOS entitlements allow Electron and the bundled Python', () => {
  for (const key of ['entitlements', 'entitlementsInherit']) {
    const plist = fs.readFileSync(path.join(root, build.mac[key]), 'utf8');
    for (const entitlement of [
      'com.apple.security.cs.allow-jit',
      'com.apple.security.cs.allow-unsigned-executable-memory',
      'com.apple.security.cs.disable-library-validation',
    ]) {
      assert.match(plist, new RegExp(`<key>${entitlement.replace(/\./g, '\\.')}</key>\\s*<true/>`), `${key}: ${entitlement}`);
    }
  }
});

test('the app icon is a 1024x1024 PNG', () => {
  const png = fs.readFileSync(path.join(root, 'build', 'icon.png'));
  assert.equal(png.subarray(1, 4).toString('latin1'), 'PNG');
  assert.equal(png.readUInt32BE(16), 1024);
  assert.equal(png.readUInt32BE(20), 1024);
});

function globToRegExp(glob) {
  const escaped = glob.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*\*\//g, '(?:.*/)?').replace(/\*\*/g, '.*').replace(/\*/g, '[^/]*');
  return new RegExp(`^${escaped}$`);
}

test('every local module and asset main.js uses is packaged, and no tests or scripts are', () => {
  const patterns = build.files.map(globToRegExp);
  const packaged = (rel) => patterns.some((re) => re.test(rel));
  const runtime = new Set(['main.js', 'package.json']);
  const queue = ['main.js'];
  while (queue.length) {
    const file = queue.shift();
    const source = fs.readFileSync(path.join(root, file), 'utf8');
    for (const [, spec] of source.matchAll(/require\('(\.{1,2}\/[^']+)'\)/g)) {
      const rel = path.posix.normalize(path.posix.join(path.posix.dirname(file), spec.endsWith('.js') ? spec : `${spec}.js`));
      if (!runtime.has(rel)) {
        runtime.add(rel);
        queue.push(rel);
      }
    }
  }
  for (const rel of ['pages/status.html', 'pages/status.js', 'pages/status.css', 'assets/icon.png', 'assets/tray.png', 'assets/tray@2x.png', 'assets/trayTemplate.png', 'assets/trayTemplate@2x.png']) {
    assert.ok(fs.existsSync(path.join(root, rel)), `${rel} exists`);
    runtime.add(rel);
  }
  for (const rel of runtime) assert.ok(packaged(rel), `${rel} is packaged`);
  for (const rel of ['test/protocol.test.js', 'scripts/smoke.mjs', 'scripts/before-pack.js', 'build/icon.png', 'node_modules/electron/index.js']) {
    assert.ok(!packaged(rel), `${rel} is not packaged`);
  }
});
