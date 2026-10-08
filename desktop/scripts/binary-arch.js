'use strict';
/**
 * Reads an executable's header to tell its format and CPU architecture, so a
 * package never ships a sidecar built for another platform or architecture
 * (e.g. an x64 PyInstaller build inside the arm64 macOS app).
 */

const ELF_MACHINES = { 0x03: 'ia32', 0x28: 'armv7l', 0x3e: 'x64', 0xb7: 'arm64' };
const MACHO_CPUS = { 0x7: 'ia32', 0xc: 'armv7l', 0x01000007: 'x64', 0x0100000c: 'arm64' };
const PE_MACHINES = { 0x014c: 'ia32', 0x01c4: 'armv7l', 0x8664: 'x64', 0xaa64: 'arm64' };

/** Returns {format: 'elf'|'macho'|'pe', arch} or null when the bytes are not a known executable. */
function detectExecutableArch(buffer) {
  const buf = Buffer.from(buffer);
  if (buf.length >= 20 && buf[0] === 0x7f && buf[1] === 0x45 && buf[2] === 0x4c && buf[3] === 0x46) {
    const machine = buf[5] === 2 ? buf.readUInt16BE(18) : buf.readUInt16LE(18);
    return { format: 'elf', arch: ELF_MACHINES[machine] || `unknown-0x${machine.toString(16)}` };
  }
  if (buf.length >= 8) {
    const magicBE = buf.readUInt32BE(0);
    if (magicBE === 0xcafebabe || magicBE === 0xcafebabf) return { format: 'macho', arch: 'universal' };
    const magicLE = buf.readUInt32LE(0);
    if (magicLE === 0xfeedfacf || magicLE === 0xfeedface) {
      const cpu = buf.readUInt32LE(4);
      return { format: 'macho', arch: MACHO_CPUS[cpu] || `unknown-0x${cpu.toString(16)}` };
    }
  }
  if (buf.length >= 0x40 && buf[0] === 0x4d && buf[1] === 0x5a) {
    const offset = buf.readUInt32LE(0x3c);
    if (offset + 6 <= buf.length && buf.readUInt32BE(offset) === 0x50450000) {
      const machine = buf.readUInt16LE(offset + 4);
      return { format: 'pe', arch: PE_MACHINES[machine] || `unknown-0x${machine.toString(16)}` };
    }
  }
  return null;
}

const FORMAT_BY_PLATFORM = { win32: 'pe', darwin: 'macho', mas: 'macho', linux: 'elf' };

/** null when `detected` can run as `platform`/`arch`, else the reason it cannot. */
function mismatch(detected, platform, arch) {
  const format = FORMAT_BY_PLATFORM[platform];
  if (!detected) return 'it is not a recognised executable';
  if (format && detected.format !== format) return `it is a ${detected.format} executable, not ${format} (${platform})`;
  if (detected.arch === 'universal' || arch === 'universal') return null;
  if (arch && detected.arch !== arch) return `it is built for ${detected.arch}, not ${arch}`;
  return null;
}

module.exports = { detectExecutableArch, mismatch };
