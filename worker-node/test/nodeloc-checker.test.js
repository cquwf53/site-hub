import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';

const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'auto-checkin-nodeloc-'));
process.env.SITEHUB_DATA_DIR = tempDir;
process.env.SITEHUB_PROFILE_DIR = path.join(tempDir, 'profiles');

const { isNodeLocSite } = await import('../src/checkin/nodeloc-checker.js');


test('NodeLoc adapter handles only the NodeLoc hostnames', () => {
  assert.equal(isNodeLocSite({ url: 'https://www.nodeloc.com/latest' }), true);
  assert.equal(isNodeLocSite({ url: 'https://nodeloc.com/' }), true);
  assert.equal(isNodeLocSite({ url: 'https://www.nodeseek.com/' }), false);
  assert.equal(isNodeLocSite({ url: 'not-a-url' }), false);
});

test.after(() => {

  fs.rmSync(tempDir, { recursive: true, force: true });
});
