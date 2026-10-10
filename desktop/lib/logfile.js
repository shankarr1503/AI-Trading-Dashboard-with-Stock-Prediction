'use strict';
/**
 * Append-only log files in <data dir>/logs with one backup (<file>.1). They are
 * rotated when opened and, because the app runs in the tray for weeks, also while
 * they are written: a log never grows much beyond MAX_BYTES (plus the backup).
 */
const fs = require('node:fs');
const path = require('node:path');

const MAX_BYTES = 5 * 1024 * 1024;

/** Moves `file` to `file`.1 (replacing an older backup). False when that is not possible. */
function rotate(file, fileSystem = fs) {
  const backup = `${file}.1`;
  try {
    fileSystem.rmSync(backup, { force: true });
    fileSystem.renameSync(file, backup);
    return true;
  } catch {
    return false;
  }
}

function rotateIfLarge(file, maxBytes = MAX_BYTES, fileSystem = fs) {
  try {
    if (fileSystem.statSync(file).size < maxBytes) return false;
  } catch {
    return false; // missing: nothing to rotate
  }
  return rotate(file, fileSystem);
}

/**
 * A log file that rotates itself once `maxBytes` have been written to it. write()
 * and end() never throw (a full disk must not crash the app).
 */
class RotatingLog {
  constructor(file, maxBytes = MAX_BYTES, fileSystem = fs) {
    this.file = file;
    this.maxBytes = maxBytes;
    this.fs = fileSystem;
    this.stream = null;
    this.bytes = 0;
    this.rotations = 0;
    this.ended = false;
    this.closing = new Set(); // streams of rotated files still flushing
    rotateIfLarge(file, maxBytes, fileSystem);
    this.openStream();
  }

  openStream() {
    // Opened synchronously, so the file exists (and can be rotated) right away.
    const fd = this.fs.openSync(this.file, 'a', 0o600);
    const stream = this.fs.createWriteStream(this.file, { fd });
    stream.on('error', () => {});
    this.stream = stream;
    try {
      this.bytes = this.fs.statSync(this.file).size;
    } catch {
      this.bytes = 0;
    }
  }

  write(data) {
    if (this.ended || !this.stream) return false;
    const size = typeof data === 'string' ? Buffer.byteLength(data) : data.length;
    if (this.bytes > 0 && this.bytes + size > this.maxBytes) this.rotateNow();
    this.bytes += size;
    try {
      return this.stream.write(data);
    } catch {
      return false;
    }
  }

  rotateNow() {
    // Renaming a file that is still open is fine on every platform (Node opens files
    // with FILE_SHARE_DELETE on Windows): writes still pending land in the backup.
    if (!rotate(this.file, this.fs)) {
      this.bytes = 0; // in use by another program: try again after another maxBytes
      return;
    }
    const previous = this.stream;
    try {
      this.openStream();
      this.rotations += 1;
    } catch {
      this.stream = previous; // keep writing to the backup rather than nowhere
      this.bytes = 0;
      return;
    }
    const flushed = new Promise((resolve) => previous.end(resolve));
    this.closing.add(flushed);
    flushed.then(() => this.closing.delete(flushed));
  }

  /** Closes the log; `callback` runs once everything written so far is flushed. */
  end(callback) {
    this.ended = true;
    const current = this.stream ? new Promise((resolve) => this.stream.end(resolve)) : Promise.resolve();
    Promise.all([current, ...this.closing]).then(() => callback && callback());
  }
}

/** Opens `file` for appending (creating its directory, private to the user). Never throws. */
function openLog(file, maxBytes = MAX_BYTES) {
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
    return new RotatingLog(file, maxBytes);
  } catch {
    return null;
  }
}

module.exports = { MAX_BYTES, RotatingLog, rotate, rotateIfLarge, openLog };
