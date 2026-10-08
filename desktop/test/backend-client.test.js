'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { requestJson } = require('../lib/backend-client');

async function withServer(handler, fn) {
  const server = http.createServer(handler);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    return await fn(`http://127.0.0.1:${server.address().port}`);
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
}

test('sends the control token and parses JSON', async () => {
  await withServer(
    (req, res) => {
      assert.equal(req.method, 'POST');
      assert.equal(req.url, '/api/desktop/shutdown');
      assert.equal(req.headers['x-desktop-token'], 'tok');
      res.writeHead(202, { 'Content-Type': 'application/json' });
      res.end('{"stopping": true}');
    },
    async (base) => {
      const res = await requestJson(`${base}/api/desktop/shutdown`, { method: 'POST', headers: { 'X-Desktop-Token': 'tok' } });
      assert.deepEqual(res, { status: 202, data: { stopping: true } });
    },
  );
});

test('returns non-JSON bodies as text and empty bodies as null', async () => {
  await withServer(
    (req, res) => {
      res.writeHead(req.url === '/text' ? 403 : 204);
      res.end(req.url === '/text' ? 'Forbidden' : undefined);
    },
    async (base) => {
      assert.deepEqual(await requestJson(`${base}/text`), { status: 403, data: 'Forbidden' });
      assert.deepEqual(await requestJson(`${base}/empty`), { status: 204, data: null });
    },
  );
});

test('times out', async () => {
  await withServer(
    () => {
      /* never answers */
    },
    async (base) => {
      await assert.rejects(requestJson(`${base}/slow`, { timeoutMs: 200 }), /timed out/);
    },
  );
});

test('rejects connection errors and non-http URLs', async () => {
  const closed = await withServer(() => {}, async (base) => base);
  await assert.rejects(requestJson(`${closed}/health`, { timeoutMs: 2000 }), /ECONNREFUSED/);
  await assert.rejects(requestJson('https://127.0.0.1:1/'), /only http/);
  await assert.rejects(requestJson('nonsense'), /Invalid URL/);
});
