import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import zlib from 'node:zlib';

import { buildZip, collectFiles, referencedFiles } from '../scripts/pack.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

/** Minimal ZIP reader (central directory) to check what buildZip wrote. */
function readZip(buf) {
  const eocd = buf.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]));
  assert.ok(eocd >= 0, 'end of central directory present');
  const count = buf.readUInt16LE(eocd + 10);
  let p = buf.readUInt32LE(eocd + 16);
  const out = new Map();
  for (let i = 0; i < count; i += 1) {
    assert.equal(buf.readUInt32LE(p), 0x02014b50);
    const method = buf.readUInt16LE(p + 10);
    const crc = buf.readUInt32LE(p + 16);
    const csize = buf.readUInt32LE(p + 20);
    const usize = buf.readUInt32LE(p + 24);
    const nlen = buf.readUInt16LE(p + 28);
    const offset = buf.readUInt32LE(p + 42);
    const name = buf.subarray(p + 46, p + 46 + nlen).toString('utf8');
    const lnlen = buf.readUInt16LE(offset + 26);
    const lextra = buf.readUInt16LE(offset + 28);
    const raw = buf.subarray(offset + 30 + lnlen + lextra, offset + 30 + lnlen + lextra + csize);
    const data = method === 8 ? zlib.inflateRawSync(raw) : raw;
    assert.equal(data.length, usize);
    assert.equal(zlib.crc32 ? zlib.crc32(data) >>> 0 : crc, crc);
    out.set(name, data);
    p += 46 + nlen;
  }
  return out;
}

test('only runtime files are packaged', () => {
  const files = collectFiles();
  for (const f of files) {
    assert.ok(!/^(tests|scripts|dist|node_modules)\//.test(f), `${f} must not ship`);
    assert.ok(!['package.json', 'README.md'].includes(f), `${f} must not ship`);
  }
  for (const required of ['manifest.json', 'background.js', 'popup.html', 'popup.js', 'options.html', 'options.js',
    'lib/api.js', 'lib/tickers.js', 'icons/icon16.png', 'icons/icon128.png']) {
    assert.ok(files.includes(required), `${required} missing`);
  }
});

test('everything the manifest, pages and imports reference is packaged', () => {
  const files = collectFiles();
  const refs = referencedFiles(files);
  const present = new Set(files);
  assert.deepEqual(refs.filter((r) => !present.has(r.path)), []);
  // The scanner does see multi-line imports and page assets.
  assert.ok(refs.some((r) => r.from === 'popup.js' && r.path === 'lib/format.js'));
  assert.ok(refs.some((r) => r.from === 'popup.html' && r.path === 'popup.js'));
  assert.ok(refs.some((r) => r.from === 'lib/poller.js' && r.path === 'icons/icon128.png'));
});

test('buildZip round-trips and is deterministic', () => {
  const files = collectFiles();
  const entries = files.map((name) => ({ name, data: readFileSync(join(ROOT, name)) }));
  const a = buildZip(entries);
  const b = buildZip(entries);
  assert.ok(a.equals(b), 'same input, same bytes');
  const read = readZip(a);
  assert.deepEqual([...read.keys()], files);
  for (const { name, data } of entries) assert.ok(read.get(name).equals(data), `${name} content`);
  const manifest = JSON.parse(read.get('manifest.json'));
  assert.equal(manifest.manifest_version, 3);
});

test('manifest declares exactly the agreed permissions', () => {
  const m = JSON.parse(readFileSync(join(ROOT, 'manifest.json'), 'utf8'));
  assert.equal(m.name, 'AI Trading Bot — Companion');
  assert.equal(m.version, '1.0.0');
  assert.deepEqual(m.permissions, ['storage', 'alarms', 'notifications', 'activeTab']);
  assert.deepEqual(m.host_permissions, ['http://127.0.0.1/*', 'http://localhost/*']);
  assert.deepEqual(m.optional_host_permissions, ['https://*/*']);
  assert.equal(m.background.type, 'module');
  assert.equal(m.options_ui.open_in_tab, true);
  assert.ok(!('content_scripts' in m), 'no content scripts');
  assert.ok(!('externally_connectable' in m));
  assert.ok(!('web_accessible_resources' in m));
  assert.match(m.content_security_policy.extension_pages, /script-src 'self';/);
  assert.deepEqual(Object.keys(m.icons), ['16', '32', '48', '128']);
});
