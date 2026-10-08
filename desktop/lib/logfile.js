'use strict';
/** Append-only log files in <data dir>/logs, rotated (one backup) when they grow too large. */
const fs = require('node:fs');
const path = require('node:path');

const MAX_BYTES = 5 * 1024 * 1024;

function rotateIfLarge(file, maxBytes = MAX_BYTES, fileSystem = fs) {
  try {
    if (fileSystem.statSync(file).size < maxBytes) return false;
  } catch {
    return false; // missing: nothing to rotate
  }
  const backup = `${file}.1`;
  try {
    fileSystem.rmSync(backup, { force: true });
    fileSystem.renameSync(file, backup);
    return true;
  } catch {
    return false;
  }
}

/** Opens `file` for appending (creating its directory, private to the user). Never throws. */
function openLog(file, maxBytes = MAX_BYTES) {
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
    rotateIfLarge(file, maxBytes);
    const stream = fs.createWriteStream(file, { flags: 'a', mode: 0o600 });
    stream.on('error', () => {}); // a full disk must not crash the app
    return stream;
  } catch {
    return null;
  }
}

module.exports = { MAX_BYTES, rotateIfLarge, openLog };
