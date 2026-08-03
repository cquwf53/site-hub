import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';

const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'auto-checkin-login-'));
process.env.SITEHUB_DATA_DIR = tempDir;
process.env.SITEHUB_PROFILE_DIR = path.join(tempDir, 'profiles');

const { isAuthenticatedUrl } = await import(
  '../src/checkin/login-session-manager.js'
);


test('XKool login detection only accepts its authenticated dashboard', () => {
  assert.equal(
    isAuthenticatedUrl(
      'https://new.xkool.cfd/dashboard/overview',
      'https://new.xkool.cfd/dashboard/overview',
    ),
    true,
  );
  assert.equal(
    isAuthenticatedUrl(
      'https://new.xkool.cfd/dashboard/overview',
      'https://new.xkool.cfd/sign-in?redirect=%2Fdashboard%2Foverview',
    ),
    false,
  );
  assert.equal(
    isAuthenticatedUrl(
      'https://new.xkool.cfd/dashboard/overview',
      'https://example.com/dashboard/overview',
    ),
    false,
  );
});

test.after(() => {

  fs.rmSync(tempDir, { recursive: true, force: true });
});
