'use strict';
/**
 * What the app window may navigate to. The dashboard is served by the local
 * sidecar on one origin (http://127.0.0.1:<port>); everything else is either
 * handed to the system browser (http/https) or refused.
 */

function originOf(url) {
  try {
    const parsed = new URL(String(url));
    return parsed.origin === 'null' ? null : parsed.origin;
  } catch {
    return null;
  }
}

/**
 * 'allow'     same origin as the app (http on the local server)
 * 'external'  an http(s) page elsewhere: open it in the default browser
 * 'deny'      anything else (file:, data:, javascript:, custom schemes, garbage)
 */
function classifyNavigation(target, appUrl) {
  let parsed;
  try {
    parsed = new URL(String(target));
  } catch {
    return 'deny';
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return 'deny';
  if (parsed.username || parsed.password) return 'deny'; // http://user@host tricks
  const appOrigin = originOf(appUrl);
  if (appOrigin && parsed.origin === appOrigin) return 'allow';
  return 'external';
}

/** Only plain http(s) URLs are ever passed to shell.openExternal. */
function isSafeExternalUrl(target) {
  try {
    const parsed = new URL(String(target));
    return (parsed.protocol === 'http:' || parsed.protocol === 'https:') && !parsed.username && !parsed.password;
  } catch {
    return false;
  }
}

module.exports = { originOf, classifyNavigation, isSafeExternalUrl };
