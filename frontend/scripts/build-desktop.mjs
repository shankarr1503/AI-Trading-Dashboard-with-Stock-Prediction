#!/usr/bin/env node
/**
 * Static export of the frontend for the desktop app (`npm run build:desktop`).
 *
 * Builds with NEXT_OUTPUT=export and NEXT_PUBLIC_SAME_ORIGIN=true into frontend/out/, which the
 * Python sidecar serves on the same origin as the API. Setting the variables here, rather than
 * inline in package.json, keeps the script working on Windows cmd as well as POSIX shells.
 */
import { spawnSync } from 'node:child_process';
import { existsSync, rmSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const outDir = join(root, 'out');
const nextBin = createRequire(join(root, 'package.json')).resolve('next/dist/bin/next');

const env = {
  ...process.env,
  NEXT_OUTPUT: 'export',
  NEXT_PUBLIC_SAME_ORIGIN: 'true',
  NEXT_TELEMETRY_DISABLED: '1',
};
// Same-origin mode ignores these, but drop them so nothing host-specific ends up in the bundle.
delete env.NEXT_PUBLIC_API_URL;
delete env.NEXT_PUBLIC_WS_URL;

// A stale export could otherwise ship pages that no longer exist.
rmSync(outDir, { recursive: true, force: true });

const result = spawnSync(process.execPath, [nextBin, 'build'], { cwd: root, env, stdio: 'inherit' });
if (result.error) throw result.error;
if (result.status !== 0) process.exit(result.status ?? 1);

const required = [
  'index.html',
  '404.html',
  'dashboard/index.html',
  'bot/index.html',
  'research/index.html',
  'login/index.html',
];
const missing = required.filter((page) => !existsSync(join(outDir, page)));
if (missing.length) {
  console.error(`build:desktop: the export is missing ${missing.join(', ')} in ${outDir}`);
  process.exit(1);
}
console.log(`build:desktop: static export ready in ${outDir}`);
