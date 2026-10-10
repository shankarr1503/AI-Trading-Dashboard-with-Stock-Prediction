'use strict';
/**
 * The desktop shell's own preferences (userData/desktop-preferences.json). The bot's
 * settings live in settings.env, which the sidecar owns; "Start at login" is read
 * from the operating system, not stored here.
 */
const fs = require('node:fs');
const path = require('node:path');

const DEFAULTS = Object.freeze({
  keepAwake: true, // powerSaveBlocker 'prevent-app-suspension' while the bot server runs
  trayNoticeShown: false, // the one-time "still running in the tray" notification
  lastVersion: '', // app version of the last start: the browser cache is cleared when it changes
});

/** Parses the stored JSON; unknown keys and wrongly typed values fall back to the defaults. */
function parsePreferences(text) {
  const prefs = { ...DEFAULTS };
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    return prefs;
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return prefs;
  for (const key of Object.keys(DEFAULTS)) {
    if (typeof data[key] === typeof DEFAULTS[key]) prefs[key] = data[key];
  }
  return prefs;
}

function serializePreferences(prefs) {
  const out = {};
  for (const key of Object.keys(DEFAULTS)) out[key] = key in prefs ? prefs[key] : DEFAULTS[key];
  return `${JSON.stringify(out, null, 2)}\n`;
}

class PreferencesStore {
  constructor(file, fileSystem = fs) {
    this.file = file;
    this.fs = fileSystem;
    this.values = { ...DEFAULTS };
  }

  load() {
    try {
      this.values = parsePreferences(this.fs.readFileSync(this.file, 'utf8'));
    } catch {
      this.values = { ...DEFAULTS };
    }
    return this.values;
  }

  get(key) {
    if (!(key in DEFAULTS)) throw new Error(`unknown preference ${key}`);
    return this.values[key];
  }

  set(key, value) {
    if (!(key in DEFAULTS)) throw new Error(`unknown preference ${key}`);
    if (typeof value !== typeof DEFAULTS[key]) throw new TypeError(`${key} must be a ${typeof DEFAULTS[key]}`);
    this.values = { ...this.values, [key]: value };
    this.save();
  }

  save() {
    this.fs.mkdirSync(path.dirname(this.file), { recursive: true });
    const tmp = `${this.file}.${process.pid}.tmp`;
    this.fs.writeFileSync(tmp, serializePreferences(this.values), 'utf8');
    this.fs.renameSync(tmp, this.file);
  }
}

module.exports = { DEFAULTS, parsePreferences, serializePreferences, PreferencesStore };
