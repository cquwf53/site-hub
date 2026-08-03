import test from 'node:test';
import assert from 'node:assert/strict';
import { PassThrough } from 'node:stream';

import { JsonRpcPeer } from '../src/protocol.js';

test('JSON-RPC peer handles a request and emits one JSON line', async () => {
  const input = new PassThrough();
  const output = new PassThrough();
  const peer = new JsonRpcPeer({ input, output });
  peer.handle('sum', ({ a, b }) => a + b);

  const line = new Promise(resolve => output.once('data', data => resolve(data.toString('utf8'))));
  input.write('{"jsonrpc":"2.0","id":"p-1","method":"sum","params":{"a":2,"b":3}}\n');
  assert.deepEqual(JSON.parse(await line), { jsonrpc: '2.0', id: 'p-1', result: 5 });
  peer.close();
});

test('JSON-RPC peer returns a parse error without echoing input', async () => {
  const input = new PassThrough();
  const output = new PassThrough();
  const peer = new JsonRpcPeer({ input, output });
  const line = new Promise(resolve => output.once('data', data => resolve(data.toString('utf8'))));
  input.write('{secret-not-json}\n');
  const response = JSON.parse(await line);
  assert.equal(response.error.code, -32700);
  assert.equal(line.toString().includes('secret-not-json'), false);
  peer.close();
});

test('JSON-RPC peer rejects oversized input without reflecting it', async () => {
  const input = new PassThrough();
  const output = new PassThrough();
  const peer = new JsonRpcPeer({ input, output });
  const line = new Promise(resolve => output.once('data', data => resolve(data.toString('utf8'))));
  input.write(`${'x'.repeat(1024 * 1024 + 1)}\n`);
  const response = JSON.parse(await line);
  assert.equal(response.error.code, -32600);
  assert.equal(response.error.message, 'Request too large');
  peer.close();
});

test('outbound JSON-RPC requests have a bounded acknowledgement timeout', async () => {
  const input = new PassThrough();
  const output = new PassThrough();
  const peer = new JsonRpcPeer({ input, output, requestTimeoutMs: 20 });
  output.resume();
  await assert.rejects(peer.request('never.acknowledged'), /timed out/);
  peer.close();
});
