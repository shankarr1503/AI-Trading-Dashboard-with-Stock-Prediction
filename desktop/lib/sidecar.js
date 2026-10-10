'use strict';
/**
 * Where the Python sidecar (the local bot server) lives and how it is started.
 *
 *   packaged app   <resources>/backend/tradebot-backend[.exe]   (PyInstaller one-folder build)
 *   development    TRADEBOT_BACKEND_CMD if set (a command line, e.g. "../dist-backend/tradebot-backend/tradebot-backend"),
 *                  otherwise `<python> -m backend.desktop` from the repository root, serving frontend/out
 *
 * Everything here is pure (paths, environment) so it can be unit-tested for every platform.
 */
const path = require('node:path');

const DEFAULT_PORT = 47821;
const EXECUTABLE = 'tradebot-backend';

function pathFor(platform) {
  return platform === 'win32' ? path.win32 : path.posix;
}

function sidecarExecutableName(platform) {
  return platform === 'win32' ? `${EXECUTABLE}.exe` : EXECUTABLE;
}

/**
 * Splits a command line into arguments: whitespace separates them, single or double
 * quotes group them. Backslashes are literal, so Windows paths need no escaping.
 */
function splitCommandLine(commandLine) {
  const args = [];
  let current = '';
  let quote = null;
  let started = false;
  for (const ch of String(commandLine)) {
    if (quote) {
      if (ch === quote) quote = null;
      else current += ch;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
      started = true;
    } else if (/\s/.test(ch)) {
      if (started) {
        args.push(current);
        current = '';
        started = false;
      }
    } else {
      current += ch;
      started = true;
    }
  }
  if (quote) throw new Error(`unterminated ${quote} quote in ${JSON.stringify(commandLine)}`);
  if (started) args.push(current);
  return args;
}

/** The Python interpreter for running the sidecar from source. */
function findPython({ platform, env, repoRoot, fileExists }) {
  if (env.TRADEBOT_PYTHON) return env.TRADEBOT_PYTHON;
  const p = pathFor(platform);
  const candidates =
    platform === 'win32'
      ? [p.join(repoRoot, '.venv', 'Scripts', 'python.exe'), p.join(repoRoot, 'venv', 'Scripts', 'python.exe')]
      : [p.join(repoRoot, '.venv', 'bin', 'python'), p.join(repoRoot, 'venv', 'bin', 'python')];
  const found = candidates.find((candidate) => fileExists(candidate));
  if (found) return found;
  return platform === 'win32' ? 'python' : 'python3';
}

/**
 * Decides how to start the sidecar. Returns
 *   {kind, command, args, cwd, env, mustExist, description}
 * where env holds extra variables for the child and mustExist says the command is
 * a file path that has to be there (the packaged build).
 */
function resolveSidecar({ isPackaged, resourcesPath, platform, env = {}, repoRoot, dataDir, fileExists = () => false }) {
  const p = pathFor(platform);
  if (isPackaged) {
    const command = p.join(resourcesPath, 'backend', sidecarExecutableName(platform));
    return {
      kind: 'bundled',
      command,
      args: [],
      cwd: dataDir || p.dirname(command),
      env: {},
      mustExist: true,
      description: command,
    };
  }
  const custom = (env.TRADEBOT_BACKEND_CMD || '').trim();
  if (custom) {
    const [command, ...args] = splitCommandLine(custom);
    if (!command) throw new Error('TRADEBOT_BACKEND_CMD is empty');
    return {
      kind: 'custom',
      command: p.isAbsolute(command) || !/[\\/]/.test(command) ? command : p.resolve(repoRoot, command),
      args,
      cwd: repoRoot,
      env: {},
      mustExist: false,
      description: custom,
    };
  }
  const python = findPython({ platform, env, repoRoot, fileExists });
  const extra = {
    PYTHONPATH: env.PYTHONPATH ? `${repoRoot}${p.delimiter}${env.PYTHONPATH}` : repoRoot,
  };
  if (!env.TRADEBOT_STATIC_DIR) extra.TRADEBOT_STATIC_DIR = p.join(repoRoot, 'frontend', 'out');
  return {
    kind: 'python',
    command: python,
    args: ['-m', 'backend.desktop'],
    cwd: repoRoot,
    env: extra,
    mustExist: false,
    description: `${python} -m backend.desktop (from ${repoRoot})`,
  };
}

// Never handed down to the sidecar: Electron's own switches, and a control token
// the shell did not generate for this launch.
const DROPPED = new Set(['ELECTRON_RUN_AS_NODE', 'ELECTRON_NO_ATTACH_CONSOLE', 'TRADEBOT_CONTROL_TOKEN', 'TRADEBOT_SMOKE']);

/** The sidecar's environment: the shell's environment plus the launch contract variables. */
function buildSidecarEnv(baseEnv, { port, dataDir, token, extraEnv = {} }) {
  if (!dataDir) throw new Error('dataDir is required');
  if (!token || token.length < 32) throw new Error('a control token of at least 32 characters is required');
  const env = {};
  for (const [key, value] of Object.entries(baseEnv || {})) {
    if (value === undefined || DROPPED.has(key.toUpperCase()) || key.toUpperCase().startsWith('TRADEBOT_SMOKE')) continue;
    env[key] = value;
  }
  Object.assign(env, extraEnv);
  // An invalid TRADEBOT_PORT is passed through: the sidecar reports it as TRADEBOT_ERROR.
  env.TRADEBOT_PORT = String(port ?? (baseEnv && baseEnv.TRADEBOT_PORT) ?? DEFAULT_PORT);
  env.TRADEBOT_DATA_DIR = dataDir;
  env.TRADEBOT_CONTROL_TOKEN = token;
  env.PYTHONIOENCODING = 'utf-8';
  env.PYTHONUNBUFFERED = '1';
  return env;
}

/**
 * The note shown when the sidecar could not get the port it was asked for (another
 * program holds it) and serves on another one, or null. The Chrome extension and
 * any open dashboard tab still point at the old address, and whatever holds that
 * port may pose as this app: the extension's pairing check stops it there.
 */
function portChangeNotice(requestedPort, url) {
  const wanted = Number(requestedPort);
  let actual;
  try {
    actual = Number(new URL(url).port);
  } catch {
    return null;
  }
  if (!Number.isInteger(wanted) || wanted <= 0 || !actual || actual === wanted) return null;
  return {
    title: `Port ${wanted} is in use by another program`,
    body:
      `The bot server runs at ${url} instead. Point the Chrome extension at the new address ` +
      `(Find the desktop app in its options). If you do not know what uses port ${wanted}, do not sign in there.`,
  };
}

module.exports = {
  DEFAULT_PORT,
  portChangeNotice,
  sidecarExecutableName,
  splitCommandLine,
  findPython,
  resolveSidecar,
  buildSidecarEnv,
};
