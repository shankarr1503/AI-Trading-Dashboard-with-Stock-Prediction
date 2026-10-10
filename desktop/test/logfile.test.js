'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { RotatingLog, openLog, rotateIfLarge } = require('../lib/logfile');

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'logfile-'));
}

const ended = (log) => new Promise((resolve) => log.end(resolve));

test('a log that is written for a long time rotates itself (one backup), not only when opened', async () => {
  const dir = tmpDir();
  try {
    const file = path.join(dir, 'logs', 'desktop-backend.log');
    const log = openLog(file, 1000);
    assert.ok(log instanceof RotatingLog);
    const line = `${'x'.repeat(99)}\n`; // 100 bytes
    for (let i = 0; i < 95; i += 1) log.write(line); // 9.5 KB through a 1 KB cap
    await ended(log);
    assert.ok(log.rotations >= 8, `rotated ${log.rotations} times`);
    assert.deepEqual(fs.readdirSync(path.dirname(file)).sort(), ['desktop-backend.log', 'desktop-backend.log.1']);
    assert.ok(fs.statSync(file).size <= 1000);
    assert.ok(fs.statSync(`${file}.1`).size <= 1000);
    assert.equal(fs.statSync(file).size + fs.statSync(`${file}.1`).size, 1500); // the newest 15 lines are kept
    if (process.platform !== 'win32') assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('an existing large log is rotated when opened; writes after end() are ignored', async () => {
  const dir = tmpDir();
  try {
    const file = path.join(dir, 'desktop.log');
    fs.writeFileSync(file, 'y'.repeat(2000));
    assert.equal(rotateIfLarge(path.join(dir, 'missing.log'), 10), false);
    const log = openLog(file, 1000);
    assert.equal(fs.statSync(`${file}.1`).size, 2000);
    log.write('first\n');
    await ended(log);
    assert.equal(log.write('late\n'), false);
    assert.equal(fs.readFileSync(file, 'utf8'), 'first\n');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('when the file cannot be rotated (in use), writing goes on and is retried later', () => {
  const calls = [];
  const fakeStream = { on() {}, write: (d) => calls.push(String(d)) || true, end: (cb) => cb && cb() };
  const fakeFs = {
    statSync: () => ({ size: 0 }),
    openSync: () => 3,
    createWriteStream: () => fakeStream,
    rmSync: () => {},
    renameSync: () => {
      throw new Error('EBUSY');
    },
  };
  const log = new RotatingLog('/logs/x.log', 10, fakeFs);
  for (let i = 0; i < 5; i += 1) log.write('123456');
  assert.equal(calls.length, 5);
  assert.equal(log.rotations, 0);
});

test('openLog never throws', () => {
  const dir = tmpDir();
  try {
    const blocker = path.join(dir, 'file');
    fs.writeFileSync(blocker, '');
    assert.equal(openLog(path.join(blocker, 'logs', 'x.log')), null); // a file where the folder should be
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
