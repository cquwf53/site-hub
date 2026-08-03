import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';

const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'auto-checkin-api123nhh-'));
process.env.SITEHUB_DATA_DIR = tempDir;
process.env.SITEHUB_PROFILE_DIR = path.join(tempDir, 'profiles');

const { isApi123NhhSite } = await import(
  '../src/checkin/api123nhh-checker.js'
);


test('api.123nhh.com adapter only handles the configured hostname', () => {
  assert.equal(isApi123NhhSite({
    url: 'https://api.123nhh.com/console',
  }), true);
  assert.equal(isApi123NhhSite({
    url: 'https://sub.100xlabs.space/dashboard',
  }), false);
  assert.equal(isApi123NhhSite({ url: 'not-a-url' }), false);
});

test.after(() => {

  fs.rmSync(tempDir, { recursive: true, force: true });
});
