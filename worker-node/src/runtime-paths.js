import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const projectRoot = path.resolve(__dirname, '..', '..');

export const DATA_DIR = path.resolve(
  process.env.SITEHUB_DATA_DIR || path.join(projectRoot, 'data'),
);
export const SCREENSHOTS_DIR = path.resolve(
  process.env.SITEHUB_ARTIFACT_DIR || path.join(DATA_DIR, 'artifacts', 'screenshots'),
);
export const PROFILES_DIR = path.resolve(
  process.env.SITEHUB_PROFILE_DIR || path.join(DATA_DIR, 'browser-profiles'),
);

const profileOverrides = new Map();

function ensureInside(root, candidate) {
  const relative = path.relative(root, candidate);
  if (relative.startsWith('..') || path.isAbsolute(relative)) {
    throw new Error('Profile directory must stay inside the SiteHub profile root');
  }
}

export function registerProfileDir(accountId, candidate) {
  if (!candidate) return;
  const resolved = path.resolve(candidate);
  ensureInside(PROFILES_DIR, resolved);
  profileOverrides.set(String(accountId), resolved);
}

export function profileDirFor(accountId) {
  const key = String(accountId);
  return profileOverrides.get(key) || path.join(PROFILES_DIR, key);
}

fs.mkdirSync(DATA_DIR, { recursive: true });
fs.mkdirSync(SCREENSHOTS_DIR, { recursive: true });
fs.mkdirSync(PROFILES_DIR, { recursive: true });
