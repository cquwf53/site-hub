import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';

const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'auto-checkin-btschool-'));
process.env.SITEHUB_DATA_DIR = tempDir;
process.env.SITEHUB_PROFILE_DIR = path.join(tempDir, 'profiles');

const { isBtschoolSite } = await import(
  '../src/checkin/btschool-checker.js'
);


test('BTSchool adapter only handles the configured hostname', () => {
  assert.equal(isBtschoolSite({
    url: 'https://pt.btschool.club/torrents.php',
  }), true);
  assert.equal(isBtschoolSite({
    url: 'https://www.52pojie.cn/',
  }), false);
  assert.equal(isBtschoolSite({ url: 'not-a-url' }), false);
});

test.after(() => {

  fs.rmSync(tempDir, { recursive: true, force: true });
});
