'use strict';
/**
 * Minimal JSON client for the sidecar's control API on 127.0.0.1. Node's http
 * module never routes through HTTP(S)_PROXY, which is what we want for loopback.
 */
const http = require('node:http');

const MAX_BODY = 1024 * 1024;

function requestJson(url, { method = 'GET', headers = {}, timeoutMs = 5000, body } = {}) {
  return new Promise((resolve, reject) => {
    let target;
    try {
      target = new URL(url);
    } catch (err) {
      reject(err);
      return;
    }
    if (target.protocol !== 'http:') {
      reject(new Error(`only http URLs are supported, got ${target.protocol}`));
      return;
    }
    const payload = body === undefined ? null : Buffer.from(JSON.stringify(body));
    const req = http.request(
      target,
      {
        method,
        headers: {
          Accept: 'application/json',
          ...(payload ? { 'Content-Type': 'application/json', 'Content-Length': payload.length } : {}),
          ...headers,
        },
        timeout: timeoutMs,
        agent: false,
      },
      (res) => {
        const chunks = [];
        let size = 0;
        res.on('data', (chunk) => {
          size += chunk.length;
          if (size > MAX_BODY) {
            req.destroy(new Error('response too large'));
            return;
          }
          chunks.push(chunk);
        });
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          let data = null;
          if (text) {
            try {
              data = JSON.parse(text);
            } catch {
              data = text;
            }
          }
          resolve({ status: res.statusCode, data });
        });
        res.on('error', reject);
      },
    );
    req.on('timeout', () => req.destroy(new Error(`request to ${target.pathname} timed out after ${timeoutMs} ms`)));
    req.on('error', reject);
    if (payload) req.write(payload);
    req.end();
  });
}

module.exports = { requestJson };
