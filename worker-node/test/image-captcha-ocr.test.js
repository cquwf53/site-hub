import test from 'node:test';
import assert from 'node:assert/strict';
import {
  normalizeCaptchaAnswer,
  normalizeCaptchaImageSource,
  recognizeCaptcha,
} from '../src/checkin/image-captcha-ocr.js';

const DATA_URL = 'data:image/png;base64,iVBORw0KGgo=';

test('normalizeCaptchaAnswer keeps only alphanumerics', () => {
  assert.equal(normalizeCaptchaAnswer('a b\nc'), 'abc');
  assert.equal(normalizeCaptchaAnswer(null), '');
});

test('normalizeCaptchaImageSource handles http urls', () => {
  assert.equal(
    normalizeCaptchaImageSource('https://example.com/c.png'),
    'https://example.com/c.png',
  );
});

test('recognizeCaptcha prefers ddddocr and uppercases/cleans the result', async () => {
  const seen = [];
  const runner = {
    async recognize(base64) {
      seen.push(base64);
      return ' r7v gn ';
    },
  };
  const result = await recognizeCaptcha(DATA_URL, { runner });
  assert.deepEqual(result, { text: 'R7VGN', engine: 'ddddocr' });
  // ddddocr 收到的是去掉 data: 前缀的纯 base64
  assert.equal(seen[0], 'iVBORw0KGgo=');
});

test('recognizeCaptcha falls back to tesseract when ddddocr result is too short', async () => {
  const runner = { async recognize() { return 'ab'; } };
  const fakeCreateWorker = async () => ({
    async setParameters() {},
    async recognize() { return { data: { text: 'x6t4q' } }; },
    async terminate() {},
  });
  const result = await recognizeCaptcha(DATA_URL, {
    runner,
    createWorker: fakeCreateWorker,
  });
  assert.deepEqual(result, { text: 'X6T4Q', engine: 'tesseract' });
});

test('recognizeCaptcha falls back to tesseract when ddddocr is unavailable', async () => {
  const runner = { async recognize() { return null; } };
  const fakeCreateWorker = async () => ({
    async setParameters() {},
    async recognize() { return { data: { text: 'mkgn2' } }; },
    async terminate() {},
  });
  const result = await recognizeCaptcha(DATA_URL, {
    runner,
    createWorker: fakeCreateWorker,
  });
  assert.deepEqual(result, { text: 'MKGN2', engine: 'tesseract' });
});

test('recognizeCaptcha returns none when both engines fail', async () => {
  const runner = { async recognize() { throw new Error('boom'); } };
  const fakeCreateWorker = async () => { throw new Error('no tesseract'); };
  const result = await recognizeCaptcha(DATA_URL, {
    runner,
    createWorker: fakeCreateWorker,
  });
  assert.deepEqual(result, { text: '', engine: 'none' });
});
