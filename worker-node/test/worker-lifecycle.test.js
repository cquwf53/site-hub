import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';

const workerDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

async function startSlowServer(t) {
  const server = http.createServer((_request, response) => {
    setTimeout(() => {
      if (!response.destroyed) {
        response.writeHead(200, { 'content-type': 'application/json' });
        response.end('{"success":true}');
      }
    }, 5000).unref?.();
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => server.close());
  return `http://127.0.0.1:${server.address().port}/checkin`;
}

function startWorker(t) {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sitehub-worker-life-'));
  const child = spawn(process.execPath, ['src/main.js'], {
    cwd: workerDir,
    env: {
      ...process.env,
      SITEHUB_DATA_DIR: tempDir,
      SITEHUB_PROFILE_DIR: path.join(tempDir, 'profiles'),
      SITEHUB_ARTIFACT_DIR: path.join(tempDir, 'artifacts'),
      SITEHUB_DISABLE_DDDDOCR: '1',
    },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  const waiters = new Set();
  const lines = readline.createInterface({ input: child.stdout });
  lines.on('line', line => {
    let message;
    try { message = JSON.parse(line); } catch { return; }
    if (message.method === 'worker.hello' && message.id !== undefined) {
      child.stdin.write(`${JSON.stringify({
        jsonrpc: '2.0', id: message.id, result: { protocol: 1, instanceId: 'test-instance' },
      })}\n`);
    }
    if (message.method === 'worker.ready' && message.id !== undefined) {
      child.stdin.write(`${JSON.stringify({
        jsonrpc: '2.0', id: message.id, result: { accepted: true },
      })}\n`);
    }
    for (const waiter of [...waiters]) {
      if (waiter.predicate(message)) {
        clearTimeout(waiter.timer);
        waiters.delete(waiter);
        waiter.resolve(message);
      }
    }
  });
  const waitFor = (predicate, timeoutMs = 5000) => new Promise((resolve, reject) => {
    const waiter = { predicate, resolve, reject, timer: null };
    waiter.timer = setTimeout(() => {
      waiters.delete(waiter);
      reject(new Error('worker response timeout'));
    }, timeoutMs);
    waiters.add(waiter);
  });
  t.after(() => {
    if (child.exitCode === null) child.kill();
    fs.rmSync(tempDir, { recursive: true, force: true });
  });
  return { child, waitFor };
}

function startPayload(url, deadlineAt) {
  return {
    jobId: 'job-1',
    leaseToken: 'lease-1',
    accountId: 'account-1',
    deadlineAt,
    site: { id: 'site-1', name: 'Test', url },
    account: { id: 'account-1', adapter: 'api', mode: 'api' },
    checkinConfig: { adapter: 'api', method: 'GET', checkinUrl: url },
  };
}

async function acknowledgeCompletion(child, waitFor) {
  const completion = await waitFor(message => message.method === 'job.complete');
  child.stdin.write(`${JSON.stringify({
    jsonrpc: '2.0',
    id: completion.id,
    result: { acknowledged: true },
  })}\n`);
  return completion;
}

test('cancelling an active job reports an unknown remote effect and recycles the worker', async t => {
  const url = await startSlowServer(t);
  const { child, waitFor } = startWorker(t);
  await waitFor(message => message.method === 'worker.ready');

  child.stdin.write(`${JSON.stringify({
    jsonrpc: '2.0', id: 'start', method: 'job.start',
    params: startPayload(url, new Date(Date.now() + 10000).toISOString()),
  })}\n`);
  assert.equal((await waitFor(message => message.id === 'start')).result.accepted, true);

  child.stdin.write('{"jsonrpc":"2.0","id":"cancel","method":"job.cancel","params":{"jobId":"job-1"}}\n');
  const completionPromise = acknowledgeCompletion(child, waitFor);
  assert.equal((await waitFor(message => message.id === 'cancel')).result.accepted, true);
  const completion = await completionPromise;
  assert.equal(completion.params.status, 'effect_unknown');
  assert.equal(completion.params.reasonCode, 'cancel_requested');
  const [code] = await once(child, 'exit');
  assert.equal(code, 75);
});

test('deadline expiry reports timeout before recycling the worker', async t => {
  const url = await startSlowServer(t);
  const { child, waitFor } = startWorker(t);
  await waitFor(message => message.method === 'worker.ready');

  // The deadline can already be very close by the time the spawned worker
  // handles job.start, so register both waiters before writing the request.
  // Otherwise job.complete may immediately follow the start acknowledgement
  // and be dropped before acknowledgeCompletion begins listening.
  const startResponse = waitFor(message => message.id === 'start');
  const completionPromise = acknowledgeCompletion(child, waitFor);
  child.stdin.write(`${JSON.stringify({
    jsonrpc: '2.0', id: 'start', method: 'job.start',
    params: startPayload(url, new Date(Date.now() + 150).toISOString()),
  })}\n`);
  await startResponse;
  const completion = await completionPromise;
  assert.equal(completion.params.status, 'effect_unknown');
  assert.equal(completion.params.reasonCode, 'timeout');
  const [code] = await once(child, 'exit');
  assert.equal(code, 124);
});
