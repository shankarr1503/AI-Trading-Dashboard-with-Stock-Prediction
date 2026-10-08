'use strict';
/**
 * AI Trading Bot — Electron shell.
 *
 * Starts the Python sidecar (the local bot server: REST/WebSocket API + the static
 * dashboard on http://127.0.0.1:<port>, plus the trading bot loop), shows the
 * dashboard in a locked-down window, and keeps running in the tray when the window
 * is closed so the bot keeps trading. Quitting is explicit and graceful: the
 * sidecar is asked to stop over its control API and lets a running bot cycle finish.
 *
 * Sidecar contract: backend/desktop.py. Pure helpers (tested with node --test): lib/.
 *
 * TRADEBOT_SMOKE=1 runs a self-test: start the sidecar, load the dashboard, check the
 * window guards, print SMOKE_OK <url>, quit through the normal graceful path and exit
 * 0 only if the sidecar exited 0 (see scripts/smoke.mjs).
 */
const {
  app,
  BrowserWindow,
  Menu,
  Notification,
  Tray,
  clipboard,
  dialog,
  nativeImage,
  powerMonitor,
  powerSaveBlocker,
  session,
  shell,
} = require('electron');
const { spawn } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { autostartPath, desktopEntry } = require('./lib/autostart');
const { requestJson } = require('./lib/backend-client');
const { openLog } = require('./lib/logfile');
const { classifyNavigation, isSafeExternalUrl, originOf } = require('./lib/navigation');
const { browserDataDir, defaultDataDir } = require('./lib/paths');
const { PreferencesStore } = require('./lib/preferences');
const { LineBuffer, parseProtocolLine } = require('./lib/protocol');
const { buildSidecarEnv, resolveSidecar } = require('./lib/sidecar');
const { describeExit, formatStatus, quitWarning } = require('./lib/status');

const APP_NAME = 'AI Trading Bot';
const APP_ID = 'com.aitradingbot.desktop';
const READY_TIMEOUT_MS = 120_000; // first start: migrations, Python imports
const SHUTDOWN_WAIT_MS = 160_000; // the sidecar lets a running bot cycle finish (≤150 s) + HTTP grace
const KILL_WAIT_MS = 10_000;
const CLOSE_GRACE_MS = 3_000; // after 'exit', how long to wait for the last stdout data
const INFO_POLL_MS = 30_000;
const OUTPUT_TAIL_LINES = 40;
const IS_MAC = process.platform === 'darwin';
const IS_WIN = process.platform === 'win32';
const REPO_ROOT = path.resolve(__dirname, '..'); // development: the sidecar runs from source here
const SMOKE = process.env.TRADEBOT_SMOKE === '1';
const SMOKE_TIMEOUT_MS = Number(process.env.TRADEBOT_SMOKE_TIMEOUT_MS) || 420_000;

class StartError extends Error {
  constructor(message, { cancelled = false } = {}) {
    super(message);
    this.cancelled = cancelled;
  }
}

// ─── State ────────────────────────────────────────────────────────────────────

const state = {
  phase: 'stopped', // starting | running | stopping | stopped
  child: null,
  token: null, // per-launch control token (X-Desktop-Token)
  url: null, // http://127.0.0.1:<port> from TRADEBOT_READY
  version: null,
  info: null, // last GET /api/desktop/info
  lastError: null, // last TRADEBOT_ERROR message
  outputTail: [], // last lines of sidecar output, for error dialogs
};

let dataDir = null;
let logsDir = null;
let desktopLog = null;
let prefs = null;
let mainWindow = null;
let splashWindow = null;
let tray = null;
let powerBlockerId = null;
let infoTimer = null;
let restarting = false;
let quitPhase = 'none'; // none | confirming | stopping | done
let systemShuttingDown = false;
const smoke = { failed: false, external: [], page: null, info: null };

// ─── Logging ──────────────────────────────────────────────────────────────────

for (const stream of [process.stdout, process.stderr]) {
  if (stream && typeof stream.on === 'function') stream.on('error', () => {}); // closed pipe: ignore
}

function log(level, message) {
  const line = `${new Date().toISOString()} [${level}] ${message}`;
  if (desktopLog) desktopLog.write(`${line}\n`);
  if (!app.isPackaged || SMOKE || level === 'error') {
    try {
      (level === 'error' ? process.stderr : process.stdout).write(`${line}\n`);
    } catch {
      /* no console */
    }
  }
}

function say(line) {
  // Machine-readable smoke-test output (SMOKE_OK / SMOKE_FAIL).
  try {
    process.stdout.write(`${line}\n`);
  } catch {
    /* no console */
  }
  if (desktopLog) desktopLog.write(`${new Date().toISOString()} [smoke] ${line}\n`);
}

process.on('uncaughtException', (err) => {
  log('error', `uncaught exception: ${err && err.stack ? err.stack : err}`);
  if (SMOKE) smokeFail(`uncaught exception: ${err && err.message}`);
});
process.on('unhandledRejection', (reason) => {
  log('error', `unhandled rejection: ${reason && reason.stack ? reason.stack : reason}`);
});

// ─── Helpers ──────────────────────────────────────────────────────────────────

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const isOpen = (win) => Boolean(win && !win.isDestroyed());
const asset = (name) => path.join(__dirname, 'assets', name);
const statusPage = path.join(__dirname, 'pages', 'status.html');

const SECURE_WEB_PREFERENCES = Object.freeze({
  contextIsolation: true,
  sandbox: true,
  nodeIntegration: false,
  nodeIntegrationInWorker: false,
  nodeIntegrationInSubFrames: false,
  webviewTag: false,
  webSecurity: true,
  allowRunningInsecureContent: false,
  navigateOnDragDrop: false,
  safeDialogs: true,
  spellcheck: false,
});

function truncate(text, max = 200) {
  const s = String(text);
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

/** Starts a helper program (editor, file manager); resolves false if it cannot be started. */
function launchDetached(command, args) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(command, args, { detached: true, stdio: 'ignore' });
    } catch {
      resolve(false);
      return;
    }
    child.once('error', () => resolve(false));
    child.once('spawn', () => {
      child.unref();
      resolve(true);
    });
  });
}

function openExternal(url) {
  if (!isSafeExternalUrl(url)) {
    log('warn', `refused to open ${truncate(url)}`);
    return;
  }
  if (SMOKE) {
    smoke.external.push(url); // the self-test checks the hand-off without starting a browser
    return;
  }
  shell.openExternal(url).catch((err) => log('warn', `could not open ${truncate(url)}: ${err.message}`));
}

function notify(title, body) {
  if (SMOKE) return;
  try {
    if (Notification.isSupported()) {
      new Notification({ title, body, icon: IS_MAC ? undefined : asset('icon.png'), silent: true }).show();
      return;
    }
  } catch (err) {
    log('warn', `notification failed: ${err.message}`);
  }
  dialog.showMessageBox({ type: 'info', title: APP_NAME, message: title, detail: body }).catch(() => {});
}

// ─── Sidecar process ──────────────────────────────────────────────────────────

function setPhase(phase) {
  state.phase = phase;
  updatePowerBlocker();
  rebuildMenus();
}

function rememberOutput(line) {
  if (!line.trim()) return;
  state.outputTail.push(truncate(line, 400));
  if (state.outputTail.length > OUTPUT_TAIL_LINES) state.outputTail.shift();
}

/** Resolves {code, signal} once the process has exited and its output is drained. */
function trackExit(child) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (result) => {
      if (!done) {
        done = true;
        resolve(result);
      }
    };
    child.once('close', (code, signal) => finish({ code, signal }));
    child.once('exit', (code, signal) => setTimeout(() => finish({ code, signal }), CLOSE_GRACE_MS));
    child.once('error', (error) => {
      if (child.pid === undefined) finish({ code: null, signal: null, error }); // never started
    });
  });
}

async function waitForExit(child, ms) {
  let timer;
  const timeout = new Promise((resolve) => {
    timer = setTimeout(() => resolve(null), ms);
  });
  const result = await Promise.race([child.exited, timeout]);
  clearTimeout(timer);
  return result;
}

function signalChild(child, signal) {
  try {
    if (child.exitCode === null && child.signalCode === null) child.kill(signal);
  } catch (err) {
    log('warn', `could not signal the bot server: ${err.message}`);
  }
}

/** Spawns the sidecar; resolves with the TRADEBOT_READY info, rejects with a StartError. */
function startBackend() {
  if (state.child) return Promise.reject(new StartError('The bot server is already running.'));
  let spec;
  try {
    spec = resolveSidecar({
      isPackaged: app.isPackaged,
      resourcesPath: process.resourcesPath,
      platform: process.platform,
      env: process.env,
      repoRoot: REPO_ROOT,
      dataDir,
      fileExists: (file) => fs.existsSync(file),
    });
  } catch (err) {
    return Promise.reject(new StartError(`Invalid TRADEBOT_BACKEND_CMD: ${err.message}`));
  }
  if (spec.mustExist && !fs.existsSync(spec.command)) {
    return Promise.reject(new StartError(`The bot server program is missing:\n${spec.command}\n\nPlease reinstall ${APP_NAME}.`));
  }
  try {
    fs.mkdirSync(dataDir, { recursive: true, mode: 0o700 });
  } catch (err) {
    return Promise.reject(new StartError(`Cannot create the data folder ${dataDir}: ${err.message}`));
  }

  const token = crypto.randomBytes(32).toString('hex');
  const env = buildSidecarEnv(process.env, { dataDir, token, extraEnv: spec.env });
  const output = openLog(path.join(logsDir, 'desktop-backend.log'));
  const record = (data) => output && output.write(data);
  record(`\n===== ${new Date().toISOString()} starting: ${spec.description} =====\n`);
  log('info', `starting the bot server: ${spec.description}`);

  let child;
  try {
    child = spawn(spec.command, spec.args, {
      cwd: spec.cwd,
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true, // the sidecar is a console program: no console window on Windows
    });
  } catch (err) {
    if (output) output.end();
    return Promise.reject(new StartError(`Could not start the bot server (${spec.command}): ${err.message}`));
  }
  child.expectedExit = false;
  child.readySeen = false;
  child.exited = trackExit(child);
  Object.assign(state, { child, token, url: null, version: null, info: null, lastError: null, outputTail: [] });
  setPhase('starting');

  const stdoutLines = new LineBuffer();
  const stderrLines = new LineBuffer();

  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      log('error', `no TRADEBOT_READY within ${READY_TIMEOUT_MS / 1000} s; stopping the bot server`);
      fail(new StartError(`The bot server did not start within ${READY_TIMEOUT_MS / 1000} seconds.`));
      child.expectedExit = true;
      signalChild(child, 'SIGTERM');
      setTimeout(() => signalChild(child, 'SIGKILL'), KILL_WAIT_MS).unref();
    }, READY_TIMEOUT_MS);
    function fail(error) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(error);
    }

    const onStdoutLine = (line) => {
      const message = parseProtocolLine(line);
      if (!message) {
        rememberOutput(line);
      } else if (message.type === 'ready') {
        if (settled || child.readySeen || state.child !== child) return;
        settled = true;
        clearTimeout(timer);
        child.readySeen = true;
        Object.assign(state, { url: message.info.url, version: message.info.version });
        log('info', `bot server ready at ${message.info.url} (version ${message.info.version}, data ${message.info.dataDir})`);
        setPhase('running');
        startInfoPolling();
        resolve(message.info);
      } else if (message.type === 'error') {
        state.lastError = message.message;
        log('error', `bot server reported: ${message.message}`);
        fail(new StartError(message.message));
      } else {
        log('warn', `ignoring an unusable TRADEBOT_READY line: ${message.reason}`);
        rememberOutput(line);
      }
    };

    child.stdout.on('data', (chunk) => {
      record(chunk);
      for (const line of stdoutLines.push(chunk)) onStdoutLine(line);
    });
    child.stderr.on('data', (chunk) => {
      record(chunk);
      for (const line of stderrLines.push(chunk)) rememberOutput(line);
    });
    child.on('error', (err) => {
      log('error', `bot server process error: ${err.message}`);
      fail(new StartError(`Could not start the bot server (${spec.command}): ${err.message}`));
    });

    child.exited.then(({ code, signal, error }) => {
      for (const line of stdoutLines.end()) onStdoutLine(line);
      for (const line of stderrLines.end()) rememberOutput(line);
      const how = error ? `failed to start: ${error.message}` : describeExit(code, signal);
      record(`===== ${new Date().toISOString()} bot server ${how} =====\n`);
      if (output) output.end();
      log(child.expectedExit && !error ? 'info' : 'warn', `bot server ${how}`);
      const current = state.child === child;
      if (current) {
        state.child = null;
        state.info = null;
        stopInfoPolling();
        setPhase('stopped');
      }
      fail(
        new StartError(child.expectedExit ? 'Cancelled.' : state.lastError || `The bot server exited during startup (${how}).`, {
          cancelled: child.expectedExit,
        }),
      );
      if (current && child.readySeen && !child.expectedExit) onUnexpectedExit(code, signal);
    });
  });
}

/**
 * Graceful stop: POST /api/desktop/shutdown (the sidecar finishes a running bot
 * cycle, then exits 0), wait up to SHUTDOWN_WAIT_MS, then kill as a last resort.
 */
async function stopBackend(reason) {
  const child = state.child;
  if (!child) return { code: 0, signal: null, alreadyStopped: true };
  child.expectedExit = true;
  stopInfoPolling();
  setPhase('stopping');
  log('info', `stopping the bot server (${reason})`);

  let asked = false;
  if (child.readySeen && state.url && state.token) {
    try {
      const res = await requestJson(`${state.url}/api/desktop/shutdown`, {
        method: 'POST',
        headers: { 'X-Desktop-Token': state.token },
        timeoutMs: 15_000,
      });
      asked = res.status === 202;
      if (!asked) log('warn', `the shutdown request was answered with HTTP ${res.status}`);
    } catch (err) {
      log('warn', `the shutdown request failed: ${err.message}`);
    }
  }
  if (!asked) signalChild(child, 'SIGTERM'); // POSIX: the sidecar stops gracefully on SIGTERM too

  let result = await waitForExit(child, SHUTDOWN_WAIT_MS);
  if (!result) {
    log('error', `the bot server did not stop within ${SHUTDOWN_WAIT_MS / 1000} s; killing it`);
    signalChild(child, 'SIGKILL');
    result = (await waitForExit(child, KILL_WAIT_MS)) || { code: null, signal: 'SIGKILL' };
  }
  log('info', `the bot server stopped (${describeExit(result.code, result.signal)})`);
  return result;
}

/** Last resort when the app exits without the graceful path (the sidecar also watches us). */
function killChildNow() {
  if (state.child) signalChild(state.child, 'SIGTERM');
}

async function fetchInfo() {
  if (state.phase !== 'running' || !state.url || !state.token) return null;
  try {
    const res = await requestJson(`${state.url}/api/desktop/info`, {
      headers: { 'X-Desktop-Token': state.token },
      timeoutMs: 8000,
    });
    if (res.status !== 200 || !res.data || typeof res.data !== 'object') {
      log('warn', `GET /api/desktop/info answered HTTP ${res.status}`);
      return null;
    }
    const changed = JSON.stringify(res.data) !== JSON.stringify(state.info);
    state.info = res.data;
    if (changed) rebuildMenus();
    return res.data;
  } catch (err) {
    log('warn', `GET /api/desktop/info failed: ${err.message}`);
    return null;
  }
}

function startInfoPolling() {
  stopInfoPolling();
  fetchInfo();
  infoTimer = setInterval(fetchInfo, INFO_POLL_MS);
}

function stopInfoPolling() {
  if (infoTimer) clearInterval(infoTimer);
  infoTimer = null;
}

// ─── Start / restart / failures ───────────────────────────────────────────────

/**
 * Error dialog with Retry / Open Logs / [Leave Stopped] / Quit. Returns 'retry', 'leave'
 * or 'quit'. Esc means Quit at startup (nothing to show without a server) and Leave
 * Stopped after a crash (the tray menu can restart it later).
 */
async function askAfterFailure({ message, detail, retryLabel, offerLeave = false }) {
  const tail = state.outputTail
    .slice(-8)
    .map((line) => truncate(line, 240))
    .join('\n');
  const choices = ['retry', 'logs', ...(offerLeave ? ['leave'] : []), 'quit'];
  const labels = { retry: retryLabel, logs: 'Open Logs', leave: 'Leave Stopped', quit: 'Quit' };
  for (;;) {
    const { response } = await dialog.showMessageBox({
      type: 'error',
      title: APP_NAME,
      message,
      detail: tail ? `${detail}\n\nLast output:\n${tail}` : detail,
      buttons: choices.map((choice) => labels[choice]),
      defaultId: 0,
      cancelId: choices.indexOf(offerLeave ? 'leave' : 'quit'),
      noLink: true,
    });
    const choice = choices[response] || 'quit';
    if (choice === 'logs') {
      await openLogs();
      continue;
    }
    return choice;
  }
}

/** Starts the sidecar (asking to retry on failure); shows the dashboard when showWindow. */
async function bootBackend({ showWindow }) {
  for (;;) {
    if (quitPhase !== 'none') return false;
    if (state.child) {
      // Already started from elsewhere (tray menu while a dialog was open).
      if (showWindow) showDashboard();
      return state.phase === 'running';
    }
    if (showWindow) {
      showStatus('Starting AI Trading Bot…', 'Starting the local trading server. The first start can take up to a minute.');
    }
    try {
      await startBackend();
      if (showWindow) showDashboard();
      return true;
    } catch (err) {
      if (err.cancelled || quitPhase !== 'none') return false;
      log('error', `the bot server failed to start: ${err.message}`);
      if (SMOKE) {
        smokeFail(`the bot server failed to start: ${err.message}`);
        return false;
      }
      closeSplash();
      const choice = await askAfterFailure({
        message: 'The bot server could not start.',
        detail: err.message,
        retryLabel: 'Try Again',
      });
      if (choice !== 'retry') {
        app.quit();
        return false;
      }
      if (state.child) await stopBackend('retry'); // e.g. the one that timed out is still exiting
      showWindow = true;
    }
  }
}

async function restartBackend() {
  if (restarting || quitPhase !== 'none' || state.phase === 'starting' || state.phase === 'stopping') return;
  restarting = true;
  rebuildMenus();
  const hadWindow = isOpen(mainWindow);
  try {
    if (hadWindow) showStatus('Restarting the bot server…', 'A trading cycle in progress finishes first.');
    await stopBackend('restart');
  } finally {
    restarting = false;
  }
  await bootBackend({ showWindow: hadWindow });
}

async function onUnexpectedExit(code, signal) {
  const how = describeExit(code, signal);
  log('error', `the bot server stopped unexpectedly (${how})`);
  if (quitPhase !== 'none' || restarting) return;
  if (SMOKE) {
    smokeFail(`the bot server stopped unexpectedly (${how})`);
    return;
  }
  if (isOpen(mainWindow)) showStatus('The bot server stopped', 'The trading bot is not running.', { busy: false });
  const choice = await askAfterFailure({
    message: 'The bot server stopped unexpectedly.',
    detail: `The local server ${how}. The trading bot is not running until it is restarted.`,
    retryLabel: 'Restart',
    offerLeave: true,
  });
  if (quitPhase !== 'none') return;
  if (choice === 'retry') await bootBackend({ showWindow: true });
  else if (choice === 'quit') app.quit();
  else rebuildMenus(); // left stopped: Bot → Restart Bot Server starts it again
}

// ─── Windows ──────────────────────────────────────────────────────────────────

function hardenWebContents(contents) {
  contents.setWindowOpenHandler(({ url }) => {
    const verdict = classifyNavigation(url, state.url);
    if (verdict === 'allow' && isOpen(mainWindow)) {
      mainWindow.loadURL(url).catch(() => {}); // same-origin pop-ups open in the app window
    } else if (verdict === 'external') {
      openExternal(url);
    } else {
      log('warn', `blocked window.open(${truncate(url)})`);
    }
    return { action: 'deny' };
  });
  const guard = (event, url) => {
    const verdict = classifyNavigation(url, state.url);
    if (verdict === 'allow') return;
    event.preventDefault();
    if (verdict === 'external') openExternal(url);
    else log('warn', `blocked navigation to ${truncate(url)}`);
  };
  contents.on('will-navigate', guard);
  contents.on('will-redirect', guard);
  contents.on('will-attach-webview', (event) => event.preventDefault());
}

function configureSession(ses) {
  // The dashboard needs no camera, microphone, location, notifications, MIDI, ...
  // Writing to the clipboard from the app itself is the only thing allowed.
  const allowed = (permission, origin) => permission === 'clipboard-sanitized-write' && !!state.url && origin === originOf(state.url);
  ses.setPermissionRequestHandler((_contents, permission, callback, details) => {
    const ok = allowed(permission, originOf(details && details.requestingUrl));
    if (!ok) log('info', `denied a permission request: ${permission}`);
    callback(ok);
  });
  ses.setPermissionCheckHandler((_contents, permission, requestingOrigin) => allowed(permission, requestingOrigin));
}

function createMainWindow() {
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    show: false,
    title: APP_NAME,
    backgroundColor: '#0a0b0d',
    icon: IS_MAC ? undefined : asset('icon.png'),
    webPreferences: { ...SECURE_WEB_PREFERENCES },
  });
  win.once('ready-to-show', () => {
    if (win.isDestroyed()) return;
    win.show();
    closeSplash();
  });
  win.on('close', () => {
    if (quitPhase === 'none') maybeShowTrayNotice();
  });
  win.on('closed', () => {
    if (mainWindow === win) mainWindow = null;
  });
  win.webContents.on('render-process-gone', (_event, details) => {
    log('error', `the dashboard renderer stopped: ${details.reason}`);
    if (SMOKE && details.reason !== 'clean-exit') smokeFail(`the renderer stopped (${details.reason})`);
  });
  win.webContents.on('did-fail-load', (_event, code, description, url, isMainFrame) => {
    if (isMainFrame && code !== -3) log('warn', `failed to load ${truncate(url)}: ${description} (${code})`);
  });
  return win;
}

function showDashboard() {
  if (quitPhase !== 'none') return;
  if (state.phase !== 'running' || !state.url) {
    if (isOpen(splashWindow)) splashWindow.focus();
    return;
  }
  const target = `${state.url}/`;
  // "/" replaces itself with /dashboard/ right away, which aborts (ERR_ABORTED) the first load.
  const load = () =>
    mainWindow.loadURL(target).catch((err) => {
      if (!/ERR_ABORTED/.test(err.message)) log('warn', `could not load ${target}: ${err.message}`);
    });
  if (!isOpen(mainWindow)) {
    mainWindow = createMainWindow();
    load();
    return;
  }
  if (originOf(mainWindow.webContents.getURL()) !== state.url) load();
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
  closeSplash();
}

/** Progress / status text: in the app window if it is open, else in a small splash window. */
function showStatus(title, detail, { busy = true, closable = true } = {}) {
  const query = { title, detail, busy: busy ? '1' : '0' };
  if (isOpen(mainWindow)) {
    mainWindow.loadFile(statusPage, { query }).catch(() => {});
    return;
  }
  if (!isOpen(splashWindow)) {
    const win = new BrowserWindow({
      width: 460,
      height: 300,
      resizable: false,
      maximizable: false,
      fullscreenable: false,
      frame: false,
      show: false,
      center: true,
      title: APP_NAME,
      backgroundColor: '#12151c',
      icon: IS_MAC ? undefined : asset('icon.png'),
      webPreferences: { ...SECURE_WEB_PREFERENCES },
    });
    win.once('ready-to-show', () => !win.isDestroyed() && win.show());
    win.on('closed', () => {
      if (splashWindow === win) splashWindow = null;
    });
    splashWindow = win;
  }
  try {
    splashWindow.setClosable(closable); // Windows and macOS only
  } catch {
    /* not supported here */
  }
  splashWindow.loadFile(statusPage, { query }).catch(() => {});
}

function closeSplash() {
  if (isOpen(splashWindow)) splashWindow.destroy();
  splashWindow = null;
}

function maybeShowTrayNotice() {
  if (SMOKE || prefs.get('trayNoticeShown')) return;
  prefs.set('trayNoticeShown', true);
  const where = !tray ? 'in the background' : IS_MAC ? 'in the menu bar' : 'in the system tray';
  notify(
    `${APP_NAME} is still running`,
    `The app keeps running ${where} so the trading bot can keep working. ` +
      `Open the dashboard again from there${tray ? '' : ' or by starting the app again'}; choose Quit to stop it.`,
  );
}

// ─── Tray and menus ───────────────────────────────────────────────────────────

function statusText() {
  if (quitPhase === 'stopping') return 'Stopping safely…';
  return formatStatus(state.phase, state.info);
}

function controlItems() {
  const busy = quitPhase !== 'none';
  const running = state.phase === 'running' && !!state.url;
  return [
    { label: 'Open Dashboard', enabled: running && !busy, click: () => showDashboard() },
    {
      label: state.url ? `Copy Server URL (${state.url})` : 'Copy Server URL',
      enabled: running,
      click: copyServerUrl,
    },
    { type: 'separator' },
    { label: 'Open Data Folder', click: () => openDataFolder() },
    { label: 'Edit Settings…', click: () => editSettings() },
    { label: 'View Logs', click: () => openLogs() },
    { type: 'separator' },
    {
      label: 'Restart Bot Server',
      enabled: !busy && !restarting && (state.phase === 'running' || state.phase === 'stopped'),
      click: () => (state.phase === 'stopped' ? bootBackend({ showWindow: true }) : restartBackend()),
    },
    { type: 'separator' },
    {
      label: 'Start at Login',
      type: 'checkbox',
      enabled: loginItemSupported(),
      checked: loginItemEnabled(),
      click: (item) => setLoginItem(item.checked),
    },
    {
      label: 'Keep Computer Awake While Running',
      type: 'checkbox',
      checked: prefs ? prefs.get('keepAwake') : true,
      click: (item) => setKeepAwake(item.checked),
    },
  ];
}

function rebuildMenus() {
  if (!app.isReady()) return;
  const status = statusText();
  const botMenu = [
    ...controlItems(),
    ...(IS_MAC ? [] : [{ type: 'separator' }, { label: 'Quit', accelerator: 'Ctrl+Q', click: () => app.quit() }]),
  ];
  const template = [
    ...(IS_MAC ? [{ role: 'appMenu' }] : []),
    { label: 'Bot', submenu: botMenu },
    { role: 'editMenu' },
    { role: 'viewMenu' },
    { role: 'windowMenu' },
    {
      role: 'help',
      submenu: [
        { label: `${APP_NAME} ${app.getVersion()}`, enabled: false },
        { label: status, enabled: false },
        { type: 'separator' },
        { label: 'View Logs', click: () => openLogs() },
        { label: 'Open Data Folder', click: () => openDataFolder() },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
  if (tray) {
    tray.setToolTip(`${APP_NAME} — ${status}`);
    tray.setContextMenu(
      Menu.buildFromTemplate([
        { label: status, enabled: false },
        { type: 'separator' },
        ...controlItems(),
        { type: 'separator' },
        { label: `Quit ${APP_NAME}`, enabled: quitPhase !== 'stopping', click: () => app.quit() },
      ]),
    );
  }
}

function createTray() {
  try {
    const image = nativeImage.createFromPath(asset(IS_MAC ? 'trayTemplate.png' : 'tray.png'));
    if (IS_MAC) image.setTemplateImage(true);
    tray = new Tray(image);
    tray.setToolTip(APP_NAME);
    if (!IS_MAC) tray.on('click', () => showDashboard());
  } catch (err) {
    log('warn', `no tray icon: ${err.message}`);
    tray = null;
  }
}

function copyServerUrl() {
  if (!state.url) return;
  clipboard.writeText(state.url);
  notify('Server URL copied', `${state.url} — paste it into the Chrome extension's options to connect it to this app.`);
}

async function openPathOrReport(target) {
  const error = await shell.openPath(target);
  if (error) {
    log('warn', `could not open ${target}: ${error}`);
    dialog.showMessageBox({ type: 'warning', title: APP_NAME, message: `Could not open ${target}`, detail: error }).catch(() => {});
  }
}

async function openDataFolder() {
  fs.mkdirSync(dataDir, { recursive: true, mode: 0o700 });
  await openPathOrReport(dataDir);
}

async function openLogs() {
  fs.mkdirSync(logsDir, { recursive: true, mode: 0o700 });
  await openPathOrReport(logsDir);
}

async function openTextFile(file) {
  // .env has no default editor on Windows and often none on macOS: use the plain text editors.
  if (IS_WIN && (await launchDetached('notepad.exe', [file]))) return true;
  if (IS_MAC && (await launchDetached('open', ['-t', file]))) return true;
  const error = await shell.openPath(file);
  if (!error) return true;
  log('warn', `could not open ${file}: ${error}`);
  shell.showItemInFolder(file);
  return false;
}

async function editSettings() {
  const file = path.join(dataDir, 'settings.env');
  if (!fs.existsSync(file)) {
    await dialog.showMessageBox({
      type: 'info',
      title: APP_NAME,
      message: 'The settings file does not exist yet.',
      detail: 'It is created the first time the bot server starts.',
    });
    return;
  }
  const opened = await openTextFile(file);
  const { response } = await dialog.showMessageBox({
    type: 'info',
    title: APP_NAME,
    message: opened ? 'settings.env is open in your text editor.' : `Open ${file} in a text editor.`,
    detail:
      'Save your changes, then restart the bot server to apply them (Bot → Restart Bot Server).\n\n' +
      'Keep this file private: it holds your API keys.',
    buttons: ['Restart Bot Server Now', 'Later'],
    defaultId: 1,
    cancelId: 1,
    noLink: true,
  });
  if (response === 0) restartBackend();
}

// Start at login: Windows and macOS through Electron; Linux through an XDG autostart
// entry for the AppImage. Never for development builds or the self-test.
function loginItemSupported() {
  if (!app.isPackaged || SMOKE) return false;
  if (IS_WIN || IS_MAC) return true;
  return process.platform === 'linux' && !!process.env.APPIMAGE;
}

function loginItemEnabled() {
  if (!loginItemSupported()) return false;
  try {
    if (process.platform === 'linux') return fs.existsSync(autostartPath(process.env, os.homedir()));
    return app.getLoginItemSettings(IS_WIN ? { args: ['--hidden'] } : undefined).openAtLogin;
  } catch {
    return false;
  }
}

function setLoginItem(enabled) {
  try {
    if (process.platform === 'linux') {
      const file = autostartPath(process.env, os.homedir());
      if (enabled) {
        fs.mkdirSync(path.dirname(file), { recursive: true });
        fs.writeFileSync(
          file,
          desktopEntry({ name: APP_NAME, exec: process.env.APPIMAGE, args: ['--hidden'], comment: 'Start the AI Trading Bot in the tray' }),
        );
      } else {
        fs.rmSync(file, { force: true });
      }
    } else if (IS_WIN) {
      app.setLoginItemSettings({ openAtLogin: enabled, args: ['--hidden'] });
    } else {
      app.setLoginItemSettings({ openAtLogin: enabled });
    }
    log('info', `start at login ${enabled ? 'enabled' : 'disabled'}`);
  } catch (err) {
    log('error', `could not change start at login: ${err.message}`);
    dialog.showErrorBox(APP_NAME, `Could not change "Start at Login": ${err.message}`);
  }
  rebuildMenus();
}

function launchedHidden() {
  if (process.argv.includes('--hidden')) return true;
  try {
    return IS_MAC && app.getLoginItemSettings().wasOpenedAtLogin === true;
  } catch {
    return false;
  }
}

function setKeepAwake(enabled) {
  prefs.set('keepAwake', enabled);
  updatePowerBlocker();
  rebuildMenus();
}

function updatePowerBlocker() {
  // Keep the computer from sleeping while the sidecar runs (including a graceful stop
  // that waits for a trading cycle), if the user wants that.
  const want = Boolean(prefs && prefs.get('keepAwake') && state.child);
  if (want && powerBlockerId === null) {
    powerBlockerId = powerSaveBlocker.start('prevent-app-suspension');
  } else if (!want && powerBlockerId !== null) {
    if (powerSaveBlocker.isStarted(powerBlockerId)) powerSaveBlocker.stop(powerBlockerId);
    powerBlockerId = null;
  }
}

// ─── Quit ─────────────────────────────────────────────────────────────────────

function onBeforeQuit(event) {
  if (quitPhase === 'done') return;
  event.preventDefault();
  beginQuit().catch((err) => {
    log('error', `quit failed: ${err && err.stack ? err.stack : err}`);
    finishQuit({ code: null, signal: null });
  });
}

async function beginQuit() {
  if (quitPhase === 'stopping') {
    if (isOpen(splashWindow)) splashWindow.focus();
    return;
  }
  if (quitPhase !== 'none') return; // the confirmation is already showing
  quitPhase = 'confirming';

  if (state.child && state.phase === 'running' && !systemShuttingDown) {
    const info = await fetchInfo(); // what is at stake, straight from the server
    const warning = quitWarning(info);
    if (warning && !SMOKE) {
      const options = {
        type: 'warning',
        title: APP_NAME,
        message: warning.message,
        detail: warning.detail,
        buttons: ['Cancel', 'Quit Anyway'],
        defaultId: 0,
        cancelId: 0,
        noLink: true,
      };
      const parent = isOpen(mainWindow) && mainWindow.isVisible() ? mainWindow : null;
      const { response } = parent ? await dialog.showMessageBox(parent, options) : await dialog.showMessageBox(options);
      if (response !== 1) {
        quitPhase = 'none';
        rebuildMenus();
        return;
      }
    }
  }

  quitPhase = 'stopping';
  rebuildMenus();
  if (isOpen(mainWindow)) mainWindow.destroy(); // the dashboard does not need to watch the shutdown
  let result = { code: 0, signal: null };
  if (state.child) {
    showStatus(
      'Stopping safely…',
      'Waiting for the trading bot to finish its current cycle and save its state. This can take up to 2½ minutes.',
      { closable: false },
    );
    result = await stopBackend('quit');
  }
  finishQuit(result);
}

function finishQuit(result) {
  quitPhase = 'done';
  stopInfoPolling();
  updatePowerBlocker();
  if (SMOKE) {
    completeSmoke(result);
    return;
  }
  closeSplash();
  app.quit();
}

// ─── Self-test (TRADEBOT_SMOKE=1) ─────────────────────────────────────────────

function writeSmokeResult(result) {
  const file = process.env.TRADEBOT_SMOKE_RESULT || path.join(app.getPath('userData'), 'smoke-result.json');
  try {
    fs.writeFileSync(file, `${JSON.stringify(result, null, 2)}\n`);
  } catch (err) {
    log('error', `could not write ${file}: ${err.message}`);
  }
}

function smokeFail(reason) {
  if (smoke.failed) return;
  smoke.failed = true;
  say(`SMOKE_FAIL ${reason}`);
  writeSmokeResult({ ok: false, reason, url: state.url, page: smoke.page });
  if (quitPhase === 'stopping') {
    // A graceful stop is already running and ends in completeSmoke(), which sees the failure.
    setTimeout(() => app.exit(1), 30_000);
    return;
  }
  quitPhase = 'stopping';
  setTimeout(() => app.exit(1), SHUTDOWN_WAIT_MS + KILL_WAIT_MS + 5_000);
  stopBackend('self-test failed')
    .catch(() => {})
    .finally(() => app.exit(1));
}

function completeSmoke(result) {
  const ok = !smoke.failed && result && result.code === 0;
  writeSmokeResult({
    ok,
    reason: ok ? null : smoke.failed ? 'see SMOKE_FAIL' : `the bot server ${describeExit(result.code, result.signal)}`,
    url: state.url,
    page: smoke.page,
    info: smoke.info,
    backendExit: { code: result.code, signal: result.signal },
  });
  say(ok ? 'SMOKE_DONE the bot server stopped cleanly (exit code 0)' : `SMOKE_FAIL the bot server ${describeExit(result.code, result.signal)}`);
  app.exit(ok ? 0 : 1);
}

function evalIn(contents, code, timeoutMs = 5000) {
  let timer;
  return Promise.race([
    contents.executeJavaScript(code, true),
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('executeJavaScript timed out')), timeoutMs);
    }),
  ]).finally(() => clearTimeout(timer));
}

async function waitForDashboard(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    if (isOpen(mainWindow)) {
      try {
        last = await evalIn(
          mainWindow.webContents,
          `({ title: document.title, path: location.pathname, ready: document.readyState,
              buttons: document.querySelectorAll('button').length,
              hasChartTab: Array.from(document.querySelectorAll('button')).some((b) => b.textContent.trim() === 'Chart') })`,
        );
        if (last && last.ready === 'complete' && last.path.startsWith('/dashboard') && /AI Trading/.test(last.title) && last.hasChartTab) {
          return last;
        }
      } catch {
        /* navigating: try again */
      }
    }
    await delay(500);
  }
  throw new Error(`the dashboard did not load within ${timeoutMs / 1000} s (last state: ${JSON.stringify(last)})`);
}

async function runSmoke() {
  smoke.page = await waitForDashboard(90_000);
  say(`SMOKE_PAGE ${JSON.stringify(smoke.page)}`);
  const contents = mainWindow.webContents;

  // Pop-ups are denied and external links go to the system browser instead.
  const windowsBefore = BrowserWindow.getAllWindows().length;
  const popupBlocked = await evalIn(contents, `window.open('https://example.com/smoke-popup', '_blank') === null`);
  await evalIn(contents, `setTimeout(() => { location.href = 'https://example.com/smoke-navigation'; }, 0); true`);
  await delay(1500);
  const where = await evalIn(contents, 'location.origin + location.pathname');
  const checks = {
    popupBlocked: popupBlocked === true && BrowserWindow.getAllWindows().length === windowsBefore,
    navigationBlocked: where.startsWith(`${state.url}/dashboard`),
    externalHandedOff:
      smoke.external.includes('https://example.com/smoke-popup') && smoke.external.includes('https://example.com/smoke-navigation'),
  };
  const failed = Object.keys(checks).filter((name) => !checks[name]);
  if (failed.length) throw new Error(`window guard checks failed: ${failed.join(', ')} (${JSON.stringify({ where, external: smoke.external })})`);

  // Closing the window keeps the app and the bot server running; the dashboard reopens.
  mainWindow.close();
  await delay(1000);
  if (state.phase !== 'running' || !state.child) throw new Error('closing the window stopped the bot server');
  showDashboard();
  smoke.page = await waitForDashboard(60_000);

  // The control API answers with this launch's token.
  const info = await fetchInfo();
  if (!info || info.url !== state.url) throw new Error(`GET /api/desktop/info returned ${JSON.stringify(info)}`);
  smoke.info = info;
  say(`SMOKE_INFO ${JSON.stringify(info)}`);
  say(`SMOKE_OK ${state.url}`);
  app.quit(); // the real quit path: GET info, POST shutdown, wait for the sidecar to exit
}

// ─── Startup ──────────────────────────────────────────────────────────────────

async function onReady() {
  configureSession(session.defaultSession);
  powerMonitor.on('shutdown', (event) => {
    // System shutdown / restart (Linux, macOS): stop gracefully, without questions.
    systemShuttingDown = true;
    if (quitPhase === 'none') {
      event.preventDefault();
      app.quit();
    }
  });
  createTray();
  rebuildMenus();
  if (SMOKE) setTimeout(() => smokeFail(`timed out after ${SMOKE_TIMEOUT_MS / 1000} s`), SMOKE_TIMEOUT_MS);

  const hidden = !SMOKE && launchedHidden();
  const started = await bootBackend({ showWindow: !hidden });
  if (started && SMOKE) runSmoke().catch((err) => smokeFail(err.message));
}

function main() {
  const userData = app.getPath('userData');
  // The sidecar keeps trading.db, settings.env, secret.key and logs/ here. TRADEBOT_DATA_DIR
  // overrides it (never in the self-test, which must not touch real data).
  dataDir = !SMOKE && process.env.TRADEBOT_DATA_DIR ? path.resolve(process.env.TRADEBOT_DATA_DIR) : userData;
  logsDir = path.join(dataDir, 'logs');
  desktopLog = openLog(path.join(logsDir, 'desktop.log'));
  prefs = new PreferencesStore(path.join(userData, 'desktop-preferences.json'));
  prefs.load();
  if (IS_WIN) app.setAppUserModelId(APP_ID);
  log(
    'info',
    `${APP_NAME} ${app.getVersion()} starting (Electron ${process.versions.electron}, ${process.platform}-${process.arch}, ` +
      `packaged: ${app.isPackaged}, data: ${dataDir}${SMOKE ? ', self-test' : ''})`,
  );

  app.on('second-instance', () => showDashboard());
  app.on('activate', () => showDashboard()); // macOS: Dock icon clicked
  app.on('window-all-closed', () => {
    /* keep running in the tray: quitting is explicit */
  });
  app.on('before-quit', onBeforeQuit);
  app.on('will-quit', killChildNow);
  app.on('web-contents-created', (_event, contents) => hardenWebContents(contents));
  process.on('exit', killChildNow);

  app.whenReady().then(onReady, (err) => log('error', `startup failed: ${err.stack || err}`));
}

if (SMOKE) {
  // A throwaway profile and data folder unless one is given: never the user's real data.
  const dir = process.env.TRADEBOT_SMOKE_USER_DATA || fs.mkdtempSync(path.join(os.tmpdir(), 'tradebot-smoke-'));
  fs.mkdirSync(dir, { recursive: true });
  app.setPath('userData', dir);
} else if (process.platform === 'linux' && !app.commandLine.hasSwitch('user-data-dir')) {
  // The sidecar's default data folder (see lib/paths.js); Windows and macOS already agree.
  app.setPath('userData', defaultDataDir(process.platform, process.env, os.homedir()));
}
// Chromium's caches, cookies and local storage in a subfolder of the data folder.
app.setPath('sessionData', browserDataDir(app.getPath('userData')));
app.setPath('crashDumps', path.join(browserDataDir(app.getPath('userData')), 'Crashpad'));
// Every renderer is sandboxed (each window also sets sandbox: true). Not when the user
// starts the app with --no-sandbox (e.g. an AppImage on a system without unprivileged
// user namespaces, or as root): enableSandbox() would force the sandbox back onto the
// network service and renderers, which then cannot start at all.
if (!app.commandLine.hasSwitch('no-sandbox')) app.enableSandbox();

if (!app.requestSingleInstanceLock()) {
  app.quit(); // the running instance shows its window (second-instance)
} else {
  main();
}
