import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';

const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'auto-checkin-xkool-'));
process.env.SITEHUB_DATA_DIR = tempDir;
process.env.SITEHUB_PROFILE_DIR = path.join(tempDir, 'profiles');

const { isXkoolSite } = await import('../src/checkin/xkool-checker.js');


test('XKool adapter only handles the configured XKool hostname', () => {
  assert.equal(isXkoolSite({
    url: 'https://new.xkool.cfd/dashboard/overview',
  }), true);
  assert.equal(isXkoolSite({
    url: 'https://sub.100xlabs.space/dashboard',
  }), false);
  assert.equal(isXkoolSite({ url: 'not-a-url' }), false);
});

test.after(() => {

  fs.rmSync(tempDir, { recursive: true, force: true });
});
