'use strict';
/**
 * electron-builder beforePack hook: refuse to package without the sidecar, or
 * with one built for another platform/architecture. The sidecar is shipped from
 * ../dist-backend/tradebot-backend (extraResources → <resources>/backend), built by
 * `python packaging/build_backend.py` on the same OS and architecture.
 *
 * TRADEBOT_SKIP_BACKEND_CHECK=1 skips the check (packaging experiments only).
 */
const fs = require('node:fs');
const path = require('node:path');
const { detectExecutableArch, mismatch } = require('./binary-arch');

// electron-builder's Arch enum
const ARCH_NAMES = { 0: 'ia32', 1: 'x64', 2: 'armv7l', 3: 'arm64', 4: 'universal' };
const BACKEND_DIR = path.resolve(__dirname, '..', '..', 'dist-backend', 'tradebot-backend');

function readHeader(file, bytes = 4096) {
  const fd = fs.openSync(file, 'r');
  try {
    const buffer = Buffer.alloc(bytes);
    const read = fs.readSync(fd, buffer, 0, bytes, 0);
    return buffer.subarray(0, read);
  } finally {
    fs.closeSync(fd);
  }
}

exports.default = async function beforePack(context) {
  if (process.env.TRADEBOT_SKIP_BACKEND_CHECK === '1') {
    console.warn('  • TRADEBOT_SKIP_BACKEND_CHECK=1: not checking the bundled bot server');
    return;
  }
  const platform = context.electronPlatformName;
  const arch = ARCH_NAMES[context.arch] || String(context.arch);
  const executable = path.join(BACKEND_DIR, platform === 'win32' ? 'tradebot-backend.exe' : 'tradebot-backend');
  if (!fs.existsSync(executable)) {
    throw new Error(
      `The bot server (sidecar) is missing: ${executable}\n` +
        'Build it first, from the repository root:\n' +
        '  (cd frontend && npm ci && npm run build:desktop)\n' +
        '  python packaging/build_backend.py',
    );
  }
  const detected = detectExecutableArch(readHeader(executable));
  const problem = mismatch(detected, platform, arch);
  if (problem) {
    throw new Error(`Cannot package ${executable} for ${platform}-${arch}: ${problem}. Build the sidecar on a ${platform}-${arch} machine.`);
  }
  console.log(`  • bundling the bot server ${executable} (${detected.format}, ${detected.arch})`);
};
