import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

test('worker uses JSONL stdout, responds to ping, and shuts down', async t => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sitehub-worker-'));
  const child = spawn(process.execPath, ['src/main.js'], {
    cwd: path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..'),
    env: {
      ...process.env,
      SITEHUB_DATA_DIR: tempDir,
      SITEHUB_PROFILE_DIR: path.join(tempDir, 'profiles'),
      SITEHUB_ARTIFACT_DIR: path.join(tempDir, 'artifacts'),
      SITEHUB_DISABLE_DDDDOCR: '1',
    },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  t.after(() => {
    if (child.exitCode === null) child.kill();
    fs.rmSync(tempDir, { recursive: true, force: true });
  });

  const lines = readline.createInterface({ input: child.stdout });
  const seen = [];
  const waitFor = predicate => new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('worker response timeout')), 5000);
    const onLine = line => {
      const message = JSON.parse(line);
      seen.push(message);
      if (predicate(message)) {
        clearTimeout(timer);
        lines.off('line', onLine);
        resolve(message);
      }
    };
    lines.on('line', onLine);
  });

  const hello = await waitFor(message => message.method === 'worker.hello');
  assert.equal(hello.params.protocol, 1);
  child.stdin.write(`${JSON.stringify({
    jsonrpc: '2.0', id: hello.id, result: { protocol: 1, instanceId: 'test-instance' },
  })}\n`);
  const ready = await waitFor(message => message.method === 'worker.ready');
  assert.equal(ready.params.protocol, 1);
  child.stdin.write(`${JSON.stringify({
    jsonrpc: '2.0', id: ready.id, result: { accepted: true },
  })}\n`);
  child.stdin.write('{"jsonrpc":"2.0","id":"p-1","method":"worker.ping","params":{}}\n');
  const pong = await waitFor(message => message.id === 'p-1');
  assert.equal(pong.result.ok, true);
  assert.equal(seen.some(message => message.method === 'worker.hello'), true);

  child.stdin.write('{"jsonrpc":"2.0","id":"p-2","method":"worker.shutdown","params":{"graceMs":1}}\n');
  const ack = await waitFor(message => message.id === 'p-2');
  assert.equal(ack.result.accepted, true);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('worker did not exit')), 5000);
    child.once('exit', () => { clearTimeout(timer); resolve(); });
  });
});

test('worker rejects a mismatched protocol before announcing ready', async t => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sitehub-worker-version-'));
  const child = spawn(process.execPath, ['src/main.js'], {
    cwd: path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..'),
    env: {
      ...process.env,
      SITEHUB_DATA_DIR: tempDir,
      SITEHUB_PROFILE_DIR: path.join(tempDir, 'profiles'),
      SITEHUB_ARTIFACT_DIR: path.join(tempDir, 'artifacts'),
      SITEHUB_DISABLE_DDDDOCR: '1',
    },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  t.after(() => {
    if (child.exitCode === null) child.kill();
    fs.rmSync(tempDir, { recursive: true, force: true });
  });

  const lines = readline.createInterface({ input: child.stdout });
  const messages = [];
  lines.on('line', line => messages.push(JSON.parse(line)));
  const hello = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('worker hello timeout')), 5000);
    lines.on('line', function onLine(line) {
      const message = JSON.parse(line);
      if (message.method !== 'worker.hello') return;
      clearTimeout(timer);
      lines.off('line', onLine);
      resolve(message);
    });
  });
  const exited = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('worker did not reject protocol')), 5000);
    child.once('exit', code => {
      clearTimeout(timer);
      resolve(code);
    });
  });
  child.stdin.write(`${JSON.stringify({
    jsonrpc: '2.0', id: hello.id, result: { protocol: 999 },
  })}\n`);
  assert.notEqual(await exited, 0);
  assert.equal(messages.some(message => message.method === 'worker.ready'), false);
});

test('worker exits when the control plane rejects ready', async t => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sitehub-worker-ready-'));
  const child = spawn(process.execPath, ['src/main.js'], {
    cwd: path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..'),
    env: {
      ...process.env,
      SITEHUB_DATA_DIR: tempDir,
      SITEHUB_PROFILE_DIR: path.join(tempDir, 'profiles'),
      SITEHUB_ARTIFACT_DIR: path.join(tempDir, 'artifacts'),
      SITEHUB_DISABLE_DDDDOCR: '1',
    },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  t.after(() => {
    if (child.exitCode === null) child.kill();
    fs.rmSync(tempDir, { recursive: true, force: true });
  });
  const lines = readline.createInterface({ input: child.stdout });
  const waitFor = predicate => new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('worker response timeout')), 5000);
    lines.on('line', function onLine(line) {
      const message = JSON.parse(line);
      if (!predicate(message)) return;
      clearTimeout(timer);
      lines.off('line', onLine);
      resolve(message);
    });
  });

  const hello = await waitFor(message => message.method === 'worker.hello');
  child.stdin.write(`${JSON.stringify({
    jsonrpc: '2.0', id: hello.id, result: { protocol: 1 },
  })}\n`);
  const ready = await waitFor(message => message.method === 'worker.ready');
  const exited = new Promise(resolve => child.once('exit', resolve));
  child.stdin.write(`${JSON.stringify({
    jsonrpc: '2.0', id: ready.id, result: { accepted: false },
  })}\n`);
  assert.notEqual(await exited, 0);
});

test('unhandled rejection closes tracked children before fatal exit', async t => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sitehub-worker-fatal-'));
  const pidFile = path.join(tempDir, 'owned-child.pid');
  const closeMarker = path.join(tempDir, 'closeable.closed');
  const workerDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const script = [
    "import fs from 'node:fs';",
    "import { spawn } from 'node:child_process';",
    "import { trackChildProcess } from './src/process-registry.js';",
    "import { trackCloseable } from './src/resource-registry.js';",
    "import './src/main.js';",
    "const owned = trackChildProcess(spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore', windowsHide: true }), 'fatal-test-child');",
    "trackCloseable({ close: async () => fs.writeFileSync(process.env.SITEHUB_FATAL_CLOSE_MARKER, 'closed') }, 'fatal-test-resource');",
    "fs.writeFileSync(process.env.SITEHUB_FATAL_PID_FILE, String(owned.pid));",
    "setTimeout(() => Promise.reject(new Error('fatal cleanup test')), 500);",
  ].join('\n');
  const child = spawn(process.execPath, ['--input-type=module', '--eval', script], {
    cwd: workerDir,
    env: {
      ...process.env,
      SITEHUB_DATA_DIR: tempDir,
      SITEHUB_PROFILE_DIR: path.join(tempDir, 'profiles'),
      SITEHUB_ARTIFACT_DIR: path.join(tempDir, 'artifacts'),
      SITEHUB_DISABLE_DDDDOCR: '1',
      SITEHUB_FATAL_PID_FILE: pidFile,
      SITEHUB_FATAL_CLOSE_MARKER: closeMarker,
    },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  const ownedPid = () => Number(fs.existsSync(pidFile) ? fs.readFileSync(pidFile, 'utf8') : 0);
  const processExists = pid => {
    if (!pid) return false;
    try { process.kill(pid, 0); return true; } catch (error) { return error.code === 'EPERM'; }
  };
  t.after(() => {
    if (child.exitCode === null) child.kill();
    const pid = ownedPid();
    if (processExists(pid)) {
      try { process.kill(pid); } catch { /* test cleanup */ }
    }
    fs.rmSync(tempDir, { recursive: true, force: true });
  });

  const lines = readline.createInterface({ input: child.stdout });
  const waitFor = predicate => new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('worker response timeout')), 5000);
    lines.on('line', function onLine(line) {
      const message = JSON.parse(line);
      if (!predicate(message)) return;
      clearTimeout(timer);
      lines.off('line', onLine);
      resolve(message);
    });
  });
  const hello = await waitFor(message => message.method === 'worker.hello');
  child.stdin.write(`${JSON.stringify({
    jsonrpc: '2.0', id: hello.id, result: { protocol: 1 },
  })}\n`);
  const ready = await waitFor(message => message.method === 'worker.ready');
  child.stdin.write(`${JSON.stringify({
    jsonrpc: '2.0', id: ready.id, result: { accepted: true },
  })}\n`);
  const [code] = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('fatal worker did not exit')), 5000);
    child.once('exit', (...args) => {
      clearTimeout(timer);
      resolve(args);
    });
  });
  assert.equal(code, 1);
  assert.equal(processExists(ownedPid()), false);
  assert.equal(fs.readFileSync(closeMarker, 'utf8'), 'closed');
});
