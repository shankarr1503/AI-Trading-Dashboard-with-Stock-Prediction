#!/usr/bin/env node
// Package the extension for the Chrome Web Store (or for sharing):
//
//   node extension/scripts/pack.mjs [--out DIR] [--list]
//
// Writes <out>/ai-trading-bot-extension-<manifest version>.zip (default out:
// extension/dist) with only the runtime files: manifest, pages, scripts,
// styles and icons. Tests, scripts, docs and package.json are left out. Before
// writing, every file the manifest, the HTML pages and the ES module imports
// reference is checked to be in the package. No dependencies: the ZIP is
// written with node:zlib, with fixed timestamps so builds are reproducible.

import { existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { dirname, extname, join, posix, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import zlib from 'node:zlib';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const EXCLUDED_DIRS = new Set(['tests', 'scripts', 'dist', 'node_modules', '.git']);
const EXCLUDED_FILES = new Set(['package.json', 'package-lock.json', 'README.md']);
const RUNTIME_EXTENSIONS = new Set(['.json', '.html', '.js', '.mjs', '.css', '.png', '.svg', '.ico', '.woff2']);
const FIXED_DATE = new Date(Date.UTC(2026, 0, 1, 0, 0, 0));

function fail(message) {
  console.error(`pack: ${message}`);
  process.exit(1);
}

function parseArgs(argv) {
  const args = { out: join(ROOT, 'dist'), list: false };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '--out') args.out = resolve(argv[++i] || fail('--out needs a directory'));
    else if (a.startsWith('--out=')) args.out = resolve(a.slice(6));
    else if (a === '--list') args.list = true;
    else if (a === '-h' || a === '--help') {
      console.log('usage: node extension/scripts/pack.mjs [--out DIR] [--list]');
      process.exit(0);
    } else fail(`unknown argument ${a}`);
  }
  return args;
}

/** Runtime files, as POSIX paths relative to the extension root, sorted. */
export function collectFiles(root = ROOT) {
  const out = [];
  const walk = (dir) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (entry.name.startsWith('.')) continue;
      const full = join(dir, entry.name);
      const rel = relative(root, full).split(sep).join('/');
      if (entry.isDirectory()) {
        if (!EXCLUDED_DIRS.has(entry.name)) walk(full);
      } else if (entry.isFile()) {
        if (EXCLUDED_FILES.has(rel)) continue;
        if (!RUNTIME_EXTENSIONS.has(extname(entry.name).toLowerCase())) continue;
        if (extname(entry.name) === '.json' && rel !== 'manifest.json') continue;
        out.push(rel);
      }
    }
  };
  walk(root);
  return out.sort();
}

/** Every local file the package needs, with where it was referenced from. */
export function referencedFiles(files, root = ROOT) {
  const refs = [];
  const add = (path, from) => refs.push({ path: posix.normalize(path), from });
  const manifest = JSON.parse(readFileSync(join(root, 'manifest.json'), 'utf8'));
  for (const icon of Object.values(manifest.icons || {})) add(icon, 'manifest icons');
  for (const icon of Object.values((manifest.action && manifest.action.default_icon) || {})) add(icon, 'manifest action');
  if (manifest.action && manifest.action.default_popup) add(manifest.action.default_popup, 'manifest action');
  if (manifest.options_ui) add(manifest.options_ui.page, 'manifest options_ui');
  if (manifest.background) add(manifest.background.service_worker, 'manifest background');

  for (const file of files) {
    const text = extname(file) === '.html' || extname(file) === '.js' || extname(file) === '.mjs'
      ? readFileSync(join(root, file), 'utf8') : '';
    const base = posix.dirname(file);
    if (extname(file) === '.html') {
      for (const m of text.matchAll(/\b(?:src|href)\s*=\s*["']([^"'#?]+)["']/g)) {
        if (!/^[a-z]+:/i.test(m[1])) add(posix.join(base, m[1]), file);
      }
    } else if (text) {
      for (const m of text.matchAll(/(?:^|[\s;])(?:import|export)\s[^'"]*?from\s*['"](\.{1,2}\/[^'"]+)['"]|import\s*['"](\.{1,2}\/[^'"]+)['"]/gm)) {
        add(posix.join(base, m[1] || m[2]), file);
      }
      for (const m of text.matchAll(/getURL\(\s*['"]([^'"]+)['"]\s*\)/g)) add(m[1], file);
    }
  }
  return refs;
}

// ─── ZIP writer ───────────────────────────────────────────────────────────────

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(buf) {
  if (typeof zlib.crc32 === 'function') return zlib.crc32(buf) >>> 0;
  let c = 0xffffffff;
  for (const byte of buf) c = CRC_TABLE[(c ^ byte) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function dosDateTime(d) {
  const time = (d.getUTCHours() << 11) | (d.getUTCMinutes() << 5) | Math.floor(d.getUTCSeconds() / 2);
  const date = ((d.getUTCFullYear() - 1980) << 9) | ((d.getUTCMonth() + 1) << 5) | d.getUTCDate();
  return { time, date };
}

export function buildZip(entries) {
  const { time, date } = dosDateTime(FIXED_DATE);
  const locals = [];
  const centrals = [];
  let offset = 0;
  for (const { name, data } of entries) {
    const nameBuf = Buffer.from(name, 'utf8');
    const deflated = zlib.deflateRawSync(data, { level: 9 });
    const useDeflate = deflated.length < data.length;
    const body = useDeflate ? deflated : data;
    const crc = crc32(data);

    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);              // version needed
    local.writeUInt16LE(0x0800, 6);          // UTF-8 names
    local.writeUInt16LE(useDeflate ? 8 : 0, 8);
    local.writeUInt16LE(time, 10);
    local.writeUInt16LE(date, 12);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(body.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBuf.length, 26);
    local.writeUInt16LE(0, 28);
    locals.push(local, nameBuf, body);

    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE((3 << 8) | 20, 4);   // made by: Unix, spec 2.0
    central.writeUInt16LE(20, 6);
    central.writeUInt16LE(0x0800, 8);
    central.writeUInt16LE(useDeflate ? 8 : 0, 10);
    central.writeUInt16LE(time, 12);
    central.writeUInt16LE(date, 14);
    central.writeUInt32LE(crc, 16);
    central.writeUInt32LE(body.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(nameBuf.length, 28);
    central.writeUInt16LE(0, 30);              // extra
    central.writeUInt16LE(0, 32);              // comment
    central.writeUInt16LE(0, 34);              // disk
    central.writeUInt16LE(0, 36);              // internal attributes
    central.writeUInt32LE((0o100644 << 16) >>> 0, 38);   // -rw-r--r--
    central.writeUInt32LE(offset, 42);
    centrals.push(central, nameBuf);
    offset += local.length + nameBuf.length + body.length;
  }
  const centralSize = centrals.reduce((n, b) => n + b.length, 0);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(0, 4);
  end.writeUInt16LE(0, 6);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(centralSize, 12);
  end.writeUInt32LE(offset, 16);
  end.writeUInt16LE(0, 20);
  return Buffer.concat([...locals, ...centrals, end]);
}

// ─── Main ─────────────────────────────────────────────────────────────────────

function main() {
  const args = parseArgs(process.argv.slice(2));
  const manifestPath = join(ROOT, 'manifest.json');
  if (!existsSync(manifestPath)) fail(`no manifest.json in ${ROOT}`);
  let manifest;
  try {
    manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
  } catch (e) {
    fail(`manifest.json is not valid JSON: ${e.message}`);
  }
  if (manifest.manifest_version !== 3) fail('manifest_version must be 3');
  if (!/^\d+(\.\d+){0,3}$/.test(String(manifest.version))) fail(`invalid version "${manifest.version}"`);

  const files = collectFiles();
  const present = new Set(files);
  const missing = referencedFiles(files).filter((r) => !present.has(r.path));
  if (missing.length) {
    fail(`referenced but not packaged:\n${missing.map((m) => `  ${m.path} (from ${m.from})`).join('\n')}`);
  }

  const entries = files.map((name) => ({ name, data: readFileSync(join(ROOT, name)) }));
  const zip = buildZip(entries);
  mkdirSync(args.out, { recursive: true });
  const target = join(args.out, `ai-trading-bot-extension-${manifest.version}.zip`);
  writeFileSync(target, zip);
  if (args.list) for (const f of files) console.log(`  ${f}`);
  const size = statSync(target).size;
  console.log(`${target} (${files.length} files, ${(size / 1024).toFixed(1)} KiB)`);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
