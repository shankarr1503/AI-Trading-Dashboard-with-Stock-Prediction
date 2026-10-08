// Server URL handling: normalisation, the http-only-on-loopback rule and the
// host permission an origin needs. Pure functions (chrome.* is passed in).

export const DEFAULT_SERVER_URL = 'http://127.0.0.1:47821';

// Hosts covered by the manifest's host_permissions (any port). The desktop app
// only listens on 127.0.0.1; "localhost" is accepted for convenience.
const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost']);

export class ServerUrlError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ServerUrlError';
  }
}

export function isLoopbackHost(hostname) {
  return LOOPBACK_HOSTS.has(String(hostname || '').toLowerCase());
}

/**
 * Normalise what the user typed into a base URL without a trailing slash:
 *   "127.0.0.1:47821"          → "http://127.0.0.1:47821"
 *   "trading.example.com/"     → "https://trading.example.com"
 *   "https://x.example/app/"   → "https://x.example/app"   (a path prefix is kept)
 * Plain http is only allowed for 127.0.0.1 / localhost: credentials must never
 * cross the network unencrypted. Throws ServerUrlError with a user-facing message.
 */
export function normalizeServerUrl(input) {
  let raw = String(input ?? '').trim();
  if (!raw) throw new ServerUrlError('Enter the server URL, e.g. http://127.0.0.1:47821');
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(raw)) {
    const host = raw.split(/[/:?#]/)[0];
    raw = `${isLoopbackHost(host) ? 'http' : 'https'}://${raw}`;
  }
  let url;
  try {
    url = new URL(raw);
  } catch {
    throw new ServerUrlError('That is not a valid URL');
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new ServerUrlError('Only http:// (on this computer) and https:// servers are supported');
  }
  if (url.username || url.password) throw new ServerUrlError('Do not put credentials in the server URL');
  if (url.search || url.hash) throw new ServerUrlError('The server URL must not contain ? or #');
  if (!url.hostname) throw new ServerUrlError('The server URL needs a host name');
  if (url.protocol === 'http:' && !isLoopbackHost(url.hostname)) {
    throw new ServerUrlError(
      'Use https:// for servers on other machines (plain http is only allowed for 127.0.0.1 and localhost)');
  }
  const path = url.pathname.replace(/\/+$/, '');
  return `${url.protocol}//${url.host}${path}`;
}

/** Same as normalizeServerUrl, but returns null instead of throwing. */
export function tryNormalizeServerUrl(input) {
  try {
    return normalizeServerUrl(input);
  } catch {
    return null;
  }
}

/** Host permission pattern for a (normalised) server URL, or null when the manifest already covers it. */
export function permissionPatternFor(baseUrl) {
  const url = new URL(baseUrl);
  if (url.protocol === 'http:' && isLoopbackHost(url.hostname)) return null;
  // Match patterns ignore the port, so one pattern covers the host on any port.
  return `${url.protocol}//${url.hostname}/*`;
}

/** Does the extension currently hold the host permission this server needs? */
export async function hasServerPermission(baseUrl, chromeApi = globalThis.chrome) {
  const pattern = permissionPatternFor(baseUrl);
  if (!pattern) return true;
  try {
    return await chromeApi.permissions.contains({ origins: [pattern] });
  } catch {
    return false;
  }
}

// The desktop app takes 47821, or the next free port up to 47841.
export const DESKTOP_PORTS = Object.freeze(Array.from({ length: 21 }, (_, i) => 47821 + i));

/**
 * Look for running desktop apps on this computer: every loopback port of the
 * desktop range whose /health answers like this backend. Resolves with their
 * base URLs, lowest port first ([] when none is running).
 */
export async function findDesktopServers({ fetchImpl, ports = DESKTOP_PORTS, timeoutMs = 2000 } = {}) {
  const doFetch = fetchImpl || globalThis.fetch.bind(globalThis);
  const probe = async (port) => {
    const url = `http://127.0.0.1:${port}`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await doFetch(`${url}/health`, {
        signal: controller.signal, credentials: 'omit', cache: 'no-store', redirect: 'error',
      });
      if (!res.ok) return null;
      const body = JSON.parse(await res.text());
      return body && body.status === 'healthy' && /^AI Trading/.test(String(body.service || '')) ? url : null;
    } catch {
      return null;
    } finally {
      clearTimeout(timer);
    }
  };
  return (await Promise.all(ports.map(probe))).filter(Boolean);
}

/** URL of a page of the web dashboard served by the same server (trailing-slash export). */
export function dashboardUrl(baseUrl, page = 'bot') {
  const clean = String(page).replace(/^\/+|\/+$/g, '');
  return clean ? `${baseUrl}/${clean}/` : `${baseUrl}/`;
}
