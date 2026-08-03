import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';

const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'auto-checkin-nodeseek-'));
process.env.SITEHUB_DATA_DIR = tempDir;
process.env.SITEHUB_PROFILE_DIR = path.join(tempDir, 'profiles');

const { isNodeSeekSite } = await import('../src/checkin/nodeseek-checker.js');


test('NodeSeek adapter handles only the NodeSeek hostnames', () => {
  assert.equal(isNodeSeekSite({
    url: 'https://www.nodeseek.com/categories/daily',
  }), true);
  assert.equal(isNodeSeekSite({ url: 'https://nodeseek.com/board' }), true);
  assert.equal(isNodeSeekSite({ url: 'https://www.nodeloc.com/latest' }), false);
  assert.equal(isNodeSeekSite({ url: 'not-a-url' }), false);
});

test.after(() => {

  fs.rmSync(tempDir, { recursive: true, force: true });
});
