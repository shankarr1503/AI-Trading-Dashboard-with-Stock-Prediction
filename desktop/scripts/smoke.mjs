#!/usr/bin/env node
/**
 * End-to-end self-test of the desktop app (TRADEBOT_SMOKE=1, see main.js) on a
 * throwaway profile and data folder:
 *
 *   npm run smoke                                        development: electron . + the sidecar from source
 *   npm run smoke -- --app dist/linux-unpacked/ai-trading-bot     a packaged app (.exe, .app or binary)
 *
 * Options: --keep (keep the temporary folder), --timeout SECONDS (default 480),
 *          -- ARGS... (extra arguments for the app, e.g. --no-sandbox).
 *
 * Passes when the app prints SMOKE_OK, its result file says ok, it exits 0, and
 * the sidecar's log shows the graceful shutdown requested through the control API.
 * On Linux without a display it runs under xvfb-run when available. As root on
 * Linux it adds --no-sandbox, without which Chromium refuses to start.
 */
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const desktopDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

function usage(message) {
  if (message) console.error(`smoke: ${message}`);
  console.error('usage: node scripts/smoke.mjs [--app PATH] [--keep] [--timeout SECONDS] [-- APP_ARGS...]');
  process.exit(2);
}

const argv = process.argv.slice(2);
const options = { app: null, keep: false, timeout: 480, extra: [] };
for (let i = 0; i < argv.length; i += 1) {
  const arg = argv[i];
  if (arg === '--') {
    options.extra = argv.slice(i + 1);
    break;
  } else if (arg === '--app') {
    options.app = argv[++i] || usage('--app needs a path');
  } else if (arg === '--keep') {
    options.keep = true;
  } else if (arg === '--timeout') {
    options.timeout = Number(argv[++i]);
    if (!(options.timeout > 0)) usage('--timeout needs a number of seconds');
  } else if (arg === '--help' || arg === '-h') {
    usage();
  } else {
    usage(`unknown argument ${arg}`);
  }
}

function onPath(name) {
  return (process.env.PATH || '')
    .split(path.delimiter)
    .some((dir) => dir && fs.existsSync(path.join(dir, name)));
}

/** A macOS .app bundle runs through its Contents/MacOS executable. */
function resolveApp(appPath) {
  const resolved = path.resolve(appPath);
  if (resolved.endsWith('.app') && fs.statSync(resolved).isDirectory()) {
    const macos = path.join(resolved, 'Contents', 'MacOS');
    const [binary] = fs.readdirSync(macos);
    return path.join(macos, binary);
  }
  return resolved;
}

let command;
let args;
if (options.app) {
  command = resolveApp(options.app);
  if (!fs.existsSync(command)) usage(`no such app: ${command}`);
  args = [...options.extra];
} else {
  command = createRequire(import.meta.url)('electron'); // the path of the Electron binary
  args = [desktopDir, ...options.extra];
}
if (process.platform === 'linux' && process.getuid?.() === 0 && !args.includes('--no-sandbox')) {
  console.warn('smoke: running as root, adding --no-sandbox (Chromium refuses to run as root with its sandbox)');
  args.push('--no-sandbox');
}
if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY) {
  if (!onPath('xvfb-run')) usage('no display: run under xvfb-run, or install it (apt-get install xvfb)');
  args = ['-a', command, ...args];
  command = 'xvfb-run';
}

const workDir = fs.mkdtempSync(path.join(os.tmpdir(), 'tradebot-smoke-'));
const userData = path.join(workDir, 'profile');
const resultFile = path.join(workDir, 'smoke-result.json');
const env = {
  ...process.env,
  TRADEBOT_SMOKE: '1',
  TRADEBOT_SMOKE_USER_DATA: userData,
  TRADEBOT_SMOKE_RESULT: resultFile,
};
delete env.ELECTRON_RUN_AS_NODE;

console.log(`smoke: ${command} ${args.join(' ')}`);
console.log(`smoke: profile and data in ${userData}`);
const started = Date.now();
const child = spawn(command, args, { env, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
let sawOk = false;
child.stdout.on('data', (chunk) => {
  process.stdout.write(chunk);
  if (/^SMOKE_OK /m.test(chunk.toString())) sawOk = true;
});
child.stderr.on('data', (chunk) => process.stderr.write(chunk));

let timedOut = false;
const timer = setTimeout(() => {
  timedOut = true;
  console.error(`smoke: no exit after ${options.timeout} s, killing the app`);
  child.kill('SIGKILL');
}, options.timeout * 1000);

const exitCode = await new Promise((resolve) => {
  child.on('error', (err) => {
    console.error(`smoke: cannot start ${command}: ${err.message}`);
    resolve(null);
  });
  child.on('close', (code, signal) => resolve(signal ? `signal ${signal}` : code));
});
clearTimeout(timer);

function readText(file) {
  try {
    return fs.readFileSync(file, 'utf8');
  } catch {
    return '';
  }
}

function tail(file, lines = 40) {
  const text = readText(file).trimEnd();
  return text ? text.split('\n').slice(-lines).join('\n') : '(empty or missing)';
}

let result = null;
try {
  result = JSON.parse(readText(resultFile));
} catch {
  /* missing or unreadable */
}
const backendLog = readText(path.join(userData, 'logs', 'backend.log'));
const problems = [];
if (timedOut) problems.push('timed out');
if (exitCode !== 0) problems.push(`the app exited with ${exitCode}`);
if (!sawOk) problems.push('no SMOKE_OK line on stdout');
if (!result || result.ok !== true) problems.push(`result file: ${result ? JSON.stringify(result) : 'missing'}`);
if (!/Shutdown requested \(control API\)/.test(backendLog)) problems.push('backend.log has no "Shutdown requested (control API)"');
if (!/Stopped \(control API\)/.test(backendLog)) problems.push('backend.log has no "Stopped (control API)"');

const seconds = ((Date.now() - started) / 1000).toFixed(1);
if (problems.length) {
  console.error(`\nsmoke: FAILED after ${seconds} s:\n  - ${problems.join('\n  - ')}`);
  for (const name of ['desktop.log', 'desktop-backend.log', 'backend.log']) {
    console.error(`\n----- ${name} (tail) -----\n${tail(path.join(userData, 'logs', name))}`);
  }
  console.error(`\nsmoke: kept ${workDir} for inspection`);
  process.exit(1);
}
console.log(`\nsmoke: PASSED in ${seconds} s (${result.url}, sidecar exit code ${result.backendExit.code}, graceful shutdown logged)`);
if (options.keep) console.log(`smoke: kept ${workDir}`);
else fs.rmSync(workDir, { recursive: true, force: true });
