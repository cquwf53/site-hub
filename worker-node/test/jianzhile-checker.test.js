import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';

const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'auto-checkin-jianzhile-'));
process.env.SITEHUB_DATA_DIR = tempDir;
process.env.SITEHUB_PROFILE_DIR = path.join(tempDir, 'profiles');
process.env.CHECKIN_TESSERACT_CACHE = path.join(tempDir, 'tesseract-cache');

const { isJianzhileSite } = await import('../src/checkin/jianzhile-checker.js');
const {
  normalizeCaptchaAnswer,
  normalizeCaptchaImageSource,
  recognizeCaptchaText,
} = await import('../src/checkin/image-captcha-ocr.js');


test('jianzhile.vip adapter only handles the configured hostname', () => {
  assert.equal(isJianzhileSite({
    url: 'https://jianzhile.vip/console/personal',
  }), true);
  assert.equal(isJianzhileSite({
    url: 'https://www.jianzhile.vip/console/personal',
  }), true);
  assert.equal(isJianzhileSite({
    url: 'https://api.123nhh.com/console',
  }), false);
  assert.equal(isJianzhileSite({ url: 'not-a-url' }), false);
});

test('captcha answer normalizer strips noise characters', () => {
  assert.equal(normalizeCaptchaAnswer(' D H T D 4 '), 'DHTD4');
  assert.equal(normalizeCaptchaAnswer('ab-12!'), 'ab12');
  assert.equal(normalizeCaptchaAnswer(''), '');
});

test('captcha image source accepts data URL and raw base64', () => {
  assert.equal(
    normalizeCaptchaImageSource('data:image/png;base64,abc'),
    'data:image/png;base64,abc',
  );
  assert.equal(
    normalizeCaptchaImageSource('abc123'),
    'data:image/png;base64,abc123',
  );
  assert.equal(normalizeCaptchaImageSource(''), null);
});

test('recognizeCaptchaText uses injected OCR worker', async () => {
  const terminateCalls = [];
  const fakeCreateWorker = async () => ({
    async setParameters() {},
    async recognize() {
      return { data: { text: ' D1T0 4 ' } };
    },
    async terminate() {
      terminateCalls.push(true);
    },
  });

  const answer = await recognizeCaptchaText('data:image/png;base64,xx', {
    createWorker: fakeCreateWorker,
  });
  assert.equal(answer, 'D1T04');
  assert.equal(terminateCalls.length, 1);
});

test.after(() => {

  fs.rmSync(tempDir, { recursive: true, force: true });
});
