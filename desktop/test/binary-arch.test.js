'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { detectExecutableArch, mismatch } = require('../scripts/binary-arch');

function elf(machine, bigEndian = false) {
  const b = Buffer.alloc(64);
  b.set([0x7f, 0x45, 0x4c, 0x46, 2, bigEndian ? 2 : 1]);
  if (bigEndian) b.writeUInt16BE(machine, 18);
  else b.writeUInt16LE(machine, 18);
  return b;
}
function macho(cpu) {
  const b = Buffer.alloc(32);
  b.writeUInt32LE(0xfeedfacf, 0);
  b.writeUInt32LE(cpu, 4);
  return b;
}
function pe(machine) {
  const b = Buffer.alloc(256);
  b.write('MZ', 0, 'latin1');
  b.writeUInt32LE(0x80, 0x3c);
  b.writeUInt32BE(0x50450000, 0x80);
  b.writeUInt16LE(machine, 0x84);
  return b;
}

test('detects ELF, Mach-O and PE architectures', () => {
  assert.deepEqual(detectExecutableArch(elf(0x3e)), { format: 'elf', arch: 'x64' });
  assert.deepEqual(detectExecutableArch(elf(0xb7)), { format: 'elf', arch: 'arm64' });
  assert.deepEqual(detectExecutableArch(macho(0x01000007)), { format: 'macho', arch: 'x64' });
  assert.deepEqual(detectExecutableArch(macho(0x0100000c)), { format: 'macho', arch: 'arm64' });
  assert.deepEqual(detectExecutableArch(Buffer.from([0xca, 0xfe, 0xba, 0xbe, 0, 0, 0, 2])), { format: 'macho', arch: 'universal' });
  assert.deepEqual(detectExecutableArch(pe(0x8664)), { format: 'pe', arch: 'x64' });
  assert.deepEqual(detectExecutableArch(pe(0xaa64)), { format: 'pe', arch: 'arm64' });
  assert.equal(detectExecutableArch(Buffer.from('#!/bin/sh\necho hi\n')), null);
  assert.equal(detectExecutableArch(Buffer.alloc(0)), null);
});

test('detects the running Node binary', () => {
  const header = Buffer.alloc(4096);
  const fd = fs.openSync(process.execPath, 'r');
  fs.readSync(fd, header, 0, header.length, 0);
  fs.closeSync(fd);
  const detected = detectExecutableArch(header);
  assert.equal(mismatch(detected, process.platform, process.arch), null, JSON.stringify(detected));
});

test('mismatch explains a wrong platform or architecture', () => {
  assert.equal(mismatch({ format: 'macho', arch: 'arm64' }, 'darwin', 'arm64'), null);
  assert.equal(mismatch({ format: 'macho', arch: 'universal' }, 'darwin', 'x64'), null);
  assert.match(mismatch({ format: 'macho', arch: 'x64' }, 'darwin', 'arm64'), /built for x64, not arm64/);
  assert.match(mismatch({ format: 'elf', arch: 'x64' }, 'win32', 'x64'), /elf executable, not pe/);
  assert.match(mismatch(null, 'linux', 'x64'), /not a recognised executable/);
});
