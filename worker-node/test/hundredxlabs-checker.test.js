import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';

const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'auto-checkin-100xlabs-'));
process.env.SITEHUB_DATA_DIR = tempDir;
process.env.SITEHUB_PROFILE_DIR = path.join(tempDir, 'profiles');

const { isHundredXLabsSite } = await import(
  '../src/checkin/hundredxlabs-checker.js'
);


test('100xLabs adapter only handles the configured hostname', () => {
  assert.equal(isHundredXLabsSite({
    url: 'https://sub.100xlabs.space/dashboard',
  }), true);
  assert.equal(isHundredXLabsSite({
    url: 'https://new.xkool.cfd/dashboard/overview',
  }), false);
  assert.equal(isHundredXLabsSite({ url: 'not-a-url' }), false);
});

test.after(() => {

  fs.rmSync(tempDir, { recursive: true, force: true });
});
