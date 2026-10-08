'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { buildSidecarEnv, findPython, resolveSidecar, sidecarExecutableName, splitCommandLine } = require('../lib/sidecar');

const TOKEN = 'a'.repeat(64);

test('the packaged sidecar lives in <resources>/backend, per platform', () => {
  const win = resolveSidecar({ isPackaged: true, resourcesPath: 'C:\\Program Files\\AI Trading Bot\\resources', platform: 'win32', dataDir: 'C:\\Users\\me\\AppData\\Roaming\\AI Trading Bot' });
  assert.equal(win.command, 'C:\\Program Files\\AI Trading Bot\\resources\\backend\\tradebot-backend.exe');
  assert.equal(win.cwd, 'C:\\Users\\me\\AppData\\Roaming\\AI Trading Bot');
  assert.deepEqual(win.args, []);
  assert.equal(win.mustExist, true);

  const mac = resolveSidecar({ isPackaged: true, resourcesPath: '/Applications/AI Trading Bot.app/Contents/Resources', platform: 'darwin' });
  assert.equal(mac.command, '/Applications/AI Trading Bot.app/Contents/Resources/backend/tradebot-backend');
  assert.equal(mac.cwd, '/Applications/AI Trading Bot.app/Contents/Resources/backend');

  const linux = resolveSidecar({ isPackaged: true, resourcesPath: '/tmp/.mount_AI/resources', platform: 'linux', dataDir: '/home/me/.config/AI Trading Bot' });
  assert.equal(linux.command, '/tmp/.mount_AI/resources/backend/tradebot-backend');
});

test('a packaged app ignores TRADEBOT_BACKEND_CMD', () => {
  const spec = resolveSidecar({ isPackaged: true, resourcesPath: '/r', platform: 'linux', env: { TRADEBOT_BACKEND_CMD: '/bin/sh -c evil' } });
  assert.equal(spec.kind, 'bundled');
  assert.equal(spec.command, '/r/backend/tradebot-backend');
});

test('development runs python -m backend.desktop from the repository root with the frontend export', () => {
  const spec = resolveSidecar({ isPackaged: false, platform: 'linux', env: {}, repoRoot: '/src/app' });
  assert.equal(spec.kind, 'python');
  assert.equal(spec.command, 'python3');
  assert.deepEqual(spec.args, ['-m', 'backend.desktop']);
  assert.equal(spec.cwd, '/src/app');
  assert.deepEqual(spec.env, { PYTHONPATH: '/src/app', TRADEBOT_STATIC_DIR: '/src/app/frontend/out' });

  const win = resolveSidecar({ isPackaged: false, platform: 'win32', env: { PYTHONPATH: 'C:\\lib', TRADEBOT_STATIC_DIR: 'D:\\out' }, repoRoot: 'C:\\src\\app' });
  assert.equal(win.command, 'python');
  assert.deepEqual(win.env, { PYTHONPATH: 'C:\\src\\app;C:\\lib' });
});

test('findPython prefers TRADEBOT_PYTHON, then a repository virtualenv', () => {
  assert.equal(findPython({ platform: 'linux', env: { TRADEBOT_PYTHON: '/opt/py/bin/python3.11' }, repoRoot: '/r', fileExists: () => true }), '/opt/py/bin/python3.11');
  assert.equal(findPython({ platform: 'linux', env: {}, repoRoot: '/r', fileExists: (f) => f === '/r/venv/bin/python' }), '/r/venv/bin/python');
  assert.equal(findPython({ platform: 'win32', env: {}, repoRoot: 'C:\\r', fileExists: (f) => f === 'C:\\r\\.venv\\Scripts\\python.exe' }), 'C:\\r\\.venv\\Scripts\\python.exe');
  assert.equal(findPython({ platform: 'darwin', env: {}, repoRoot: '/r', fileExists: () => false }), 'python3');
});

test('TRADEBOT_BACKEND_CMD overrides the development command', () => {
  const spec = resolveSidecar({
    isPackaged: false,
    platform: 'linux',
    env: { TRADEBOT_BACKEND_CMD: '../dist-backend/tradebot-backend/tradebot-backend --no-bot' },
    repoRoot: '/src/app',
  });
  assert.equal(spec.kind, 'custom');
  assert.equal(spec.command, '/src/dist-backend/tradebot-backend/tradebot-backend');
  assert.deepEqual(spec.args, ['--no-bot']);
  assert.deepEqual(spec.env, {});
  const bare = resolveSidecar({ isPackaged: false, platform: 'win32', env: { TRADEBOT_BACKEND_CMD: '"C:\\Python 311\\python.exe" -m backend.desktop' }, repoRoot: 'C:\\src' });
  assert.equal(bare.command, 'C:\\Python 311\\python.exe');
  assert.deepEqual(bare.args, ['-m', 'backend.desktop']);
});

test('splitCommandLine handles quotes and keeps backslashes', () => {
  assert.deepEqual(splitCommandLine('  a  b\tc '), ['a', 'b', 'c']);
  assert.deepEqual(splitCommandLine(`"with space" 'single quoted' x""y ""`), ['with space', 'single quoted', 'xy', '']);
  assert.deepEqual(splitCommandLine('C:\\dir\\app.exe --flag'), ['C:\\dir\\app.exe', '--flag']);
  assert.deepEqual(splitCommandLine(''), []);
  assert.throws(() => splitCommandLine('"unterminated'), /unterminated/);
});

test('sidecarExecutableName', () => {
  assert.equal(sidecarExecutableName('win32'), 'tradebot-backend.exe');
  assert.equal(sidecarExecutableName('darwin'), 'tradebot-backend');
  assert.equal(sidecarExecutableName('linux'), 'tradebot-backend');
});

test('buildSidecarEnv sets the launch contract and drops what must not leak', () => {
  const env = buildSidecarEnv(
    {
      PATH: '/usr/bin',
      HOME: '/home/me',
      ELECTRON_RUN_AS_NODE: '1',
      TRADEBOT_CONTROL_TOKEN: 'stale',
      TRADEBOT_SMOKE: '1',
      TRADEBOT_SMOKE_RESULT: '/tmp/x',
      TRADING_MODE: 'paper',
      UNDEF: undefined,
    },
    { dataDir: '/data', token: TOKEN, extraEnv: { PYTHONPATH: '/src' } },
  );
  assert.equal(env.TRADEBOT_DATA_DIR, '/data');
  assert.equal(env.TRADEBOT_CONTROL_TOKEN, TOKEN);
  assert.equal(env.TRADEBOT_PORT, '47821');
  assert.equal(env.PYTHONPATH, '/src');
  assert.equal(env.PYTHONIOENCODING, 'utf-8');
  assert.equal(env.PATH, '/usr/bin');
  assert.equal(env.TRADING_MODE, 'paper'); // real environment variables still reach the sidecar
  for (const key of ['ELECTRON_RUN_AS_NODE', 'TRADEBOT_SMOKE', 'TRADEBOT_SMOKE_RESULT', 'UNDEF']) assert.equal(key in env, false, key);
});

test('buildSidecarEnv passes TRADEBOT_PORT through (the sidecar validates it)', () => {
  assert.equal(buildSidecarEnv({ TRADEBOT_PORT: '50000' }, { dataDir: '/d', token: TOKEN }).TRADEBOT_PORT, '50000');
  assert.equal(buildSidecarEnv({ TRADEBOT_PORT: 'abc' }, { dataDir: '/d', token: TOKEN }).TRADEBOT_PORT, 'abc');
  assert.equal(buildSidecarEnv({}, { dataDir: '/d', token: TOKEN, port: 0 }).TRADEBOT_PORT, '0');
});

test('buildSidecarEnv requires a data dir and a strong token', () => {
  assert.throws(() => buildSidecarEnv({}, { token: TOKEN }), /dataDir/);
  assert.throws(() => buildSidecarEnv({}, { dataDir: '/d', token: 'short' }), /token/);
});
