'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { LineBuffer, parseProtocolLine, validateReadyInfo } = require('../lib/protocol');

const READY = 'TRADEBOT_READY {"url": "http://127.0.0.1:47821", "port": 47821, "data_dir": "/tmp/d", "version": "1.0.0"}';

function feed(chunks) {
  const buffer = new LineBuffer();
  const lines = [];
  for (const chunk of chunks) lines.push(...buffer.push(chunk));
  lines.push(...buffer.end());
  return lines;
}

test('LineBuffer reassembles a line split across chunks at every position', () => {
  const text = `${READY}\nnext line\n`;
  for (let cut = 0; cut <= text.length; cut += 1) {
    const lines = feed([Buffer.from(text.slice(0, cut)), Buffer.from(text.slice(cut))]);
    assert.deepEqual(lines, [READY, 'next line'], `cut at ${cut}`);
  }
});

test('LineBuffer handles one byte per chunk and CRLF line endings', () => {
  const bytes = Buffer.from(`first\r\n${READY}\r\nlast`);
  const chunks = [...bytes].map((b) => Buffer.from([b]));
  assert.deepEqual(feed(chunks), ['first', READY, 'last']);
});

test('LineBuffer keeps a UTF-8 character split between chunks intact', () => {
  const bytes = Buffer.from('TRADEBOT_ERROR Données verrouillées €\n', 'utf8');
  const euro = bytes.indexOf(0xe2); // first byte of the 3-byte €
  const lines = feed([bytes.subarray(0, euro + 1), bytes.subarray(euro + 1, euro + 2), bytes.subarray(euro + 2)]);
  assert.deepEqual(lines, ['TRADEBOT_ERROR Données verrouillées €']);
});

test('LineBuffer returns nothing for an incomplete line until it ends', () => {
  const buffer = new LineBuffer();
  assert.deepEqual(buffer.push('TRADEBOT_RE'), []);
  assert.deepEqual(buffer.push('ADY {}'), []);
  assert.deepEqual(buffer.end(), ['TRADEBOT_READY {}']);
  assert.deepEqual(buffer.end(), []);
});

test('LineBuffer hands over a runaway line instead of buffering forever', () => {
  const buffer = new LineBuffer({ maxLineLength: 10 });
  assert.deepEqual(buffer.push('x'.repeat(25)), ['x'.repeat(25)]);
  assert.deepEqual(buffer.push('ok\n'), ['ok']);
});

test('parseProtocolLine parses TRADEBOT_READY', () => {
  assert.deepEqual(parseProtocolLine(READY), {
    type: 'ready',
    info: { url: 'http://127.0.0.1:47821', port: 47821, dataDir: '/tmp/d', version: '1.0.0' },
  });
  // stray whitespace, a BOM and a trailing CR are tolerated
  assert.equal(parseProtocolLine(`\uFEFF  ${READY}\r`).type, 'ready');
});

test('parseProtocolLine parses TRADEBOT_ERROR', () => {
  assert.deepEqual(parseProtocolLine('TRADEBOT_ERROR AI Trading Bot is already running'), {
    type: 'error',
    message: 'AI Trading Bot is already running',
  });
  assert.deepEqual(parseProtocolLine('TRADEBOT_ERROR'), { type: 'error', message: 'Unknown startup error' });
});

test('parseProtocolLine ignores lines that are not part of the protocol', () => {
  for (const line of ['', 'INFO: started', 'TRADEBOT_READYNESS {}', 'TRADEBOT_ERRORS x', ' some TRADEBOT_READY {}']) {
    assert.equal(parseProtocolLine(line), null, line);
  }
});

test('parseProtocolLine flags unusable READY payloads', () => {
  assert.equal(parseProtocolLine('TRADEBOT_READY not json').type, 'invalid');
  assert.equal(parseProtocolLine('TRADEBOT_READY {"url": "http://127.0.0.1:1"}').type, 'invalid');
});

test('validateReadyInfo only accepts a local http URL matching the port', () => {
  assert.equal(validateReadyInfo({ url: 'http://localhost:47822', port: 47822 }).ok, true);
  assert.equal(validateReadyInfo({ url: 'http://127.0.0.1:47821/', port: 47821 }).info.url, 'http://127.0.0.1:47821');
  const bad = [
    null,
    [],
    { url: 'http://127.0.0.1:47821', port: '47821' },
    { url: 'http://127.0.0.1:47821', port: 0 },
    { url: 'http://127.0.0.1:47821', port: 70000 },
    { url: 'http://127.0.0.1:47822', port: 47821 },
    { url: 'https://127.0.0.1:47821', port: 47821 },
    { url: 'http://evil.example:47821', port: 47821 },
    { url: 'http://127.0.0.1.evil.example:47821', port: 47821 },
    { url: 'file:///etc/passwd', port: 47821 },
    { url: 'not a url', port: 47821 },
  ];
  for (const info of bad) assert.equal(validateReadyInfo(info).ok, false, JSON.stringify(info));
});
