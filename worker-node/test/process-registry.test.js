import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';

import {
  terminateTrackedProcesses,
  trackChildProcess,
  trackedProcessCount,
} from '../src/process-registry.js';

test('tracked child processes are terminated by exact ownership cleanup', async t => {
  const child = trackChildProcess(spawn(
    process.execPath,
    ['-e', 'setInterval(() => {}, 1000)'],
    { stdio: 'ignore', windowsHide: true },
  ), 'test-child');
  t.after(() => {
    if (child.exitCode === null) child.kill();
  });
  await once(child, 'spawn');
  assert.equal(trackedProcessCount(), 1);

  const exit = once(child, 'exit');
  await terminateTrackedProcesses({ graceMs: 250 });
  await exit;
  assert.equal(trackedProcessCount(), 0);
});
