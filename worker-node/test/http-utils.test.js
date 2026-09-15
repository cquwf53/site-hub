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

test('cross-origin redirects never forward header or body credentials', async t => {
  for (const status of [301, 302, 303, 307, 308]) {
    const requests = [];
    t.mock.method(globalThis, 'fetch', async (url, options) => {
      requests.push({ url: String(url), options });
      return new Response('', { status, headers: { location: 'https://other.test/capture' } });
    });
    await assert.rejects(fetchTextWithLimits('https://trusted.test/checkin', {
      method: 'POST',
      headers: { 'X-Api-Key': 'test-key', 'X-Custom-Secret': 'test-secret' },
      body: '{"token":"test-token"}',
    }), /Cross-origin redirect blocked/);
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, 'https://trusted.test/checkin');
    t.mock.restoreAll();
  }
});

test('same-origin redirect preserves a POST only when HTTP requires it', async t => {
  const requests = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    requests.push({ url: String(url), ...options });
    return requests.length === 1
      ? new Response('', { status: 307, headers: { location: '/final' } })
      : new Response('{"success":true}');
  });
  await fetchTextWithLimits('https://trusted.test/start', { method: 'POST', body: 'proof' });
  assert.equal(requests[1].url, 'https://trusted.test/final');
  assert.equal(requests[1].method, 'POST');
  assert.equal(requests[1].body, 'proof');
});
