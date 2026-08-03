import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { once } from 'node:events';

import {
  assessBusinessSuccess,
  fetchTextWithLimits,
  isAlreadyDone,
} from '../src/checkin/http-utils.js';

test('explicit false business flags override the word success', () => {
  const payload = { success: false, message: 'success is not available' };
  assert.equal(assessBusinessSuccess(payload, JSON.stringify(payload), 'success'), false);
});

test('structured success and already-done outcomes are recognized', () => {
  assert.equal(assessBusinessSuccess({ ok: true }, '{"ok":true}'), true);
  assert.equal(isAlreadyDone({ message: '今日已签到' }, ''), true);
});

test('non-HTTP targets are rejected before any request is made', async () => {
  await assert.rejects(
    fetchTextWithLimits('file:///C:/Windows/win.ini'),
    /Only HTTP and HTTPS/,
  );
});

test('redirect count is bounded', async t => {
  const server = http.createServer((_request, response) => {
    response.writeHead(302, { Location: '/again' });
    response.end();
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => server.close());
  const address = server.address();

  await assert.rejects(
    fetchTextWithLimits(`http://127.0.0.1:${address.port}/start`, {}, { maxRedirects: 2 }),
    /Redirect limit exceeded \(2\)/,
  );
});
