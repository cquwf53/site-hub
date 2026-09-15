import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import {
  dedicatedAdapters,
  describeArtifact,
  executeCheckin,
  resolveCheckinRunner,
} from '../src/checkin/runner.js';

test('dedicated adapter ids remain stable', () => {
  assert.deepEqual(
    dedicatedAdapters.map(item => item.id),
    ['xkool', 'nodeloc', 'nodeseek', 'hundredxlabs', 'api123nhh', 'jianzhile', 'btschool', '52pojie'],
  );
});

test('an explicit generic adapter does not get hostname rematched', () => {
  const runner = resolveCheckinRunner({ url: 'https://www.nodeloc.com/latest', mode: 'playwright' }, 'playwright-generic');
  assert.equal(runner.id, 'playwright-generic');
});

function params(overrides = {}) {
  return {
    accountId: '7bf7d50e-133e-4698-a57c-22e6e4ea028a',
    site: { id: 'site-1', name: 'Test', url: 'https://example.test/checkin' },
    account: {
      id: '7bf7d50e-133e-4698-a57c-22e6e4ea028a',
      name: 'Primary',
      adapter: 'fake',
    },
    checkinConfig: {},
    ...overrides,
  };
}

test('reconcile never runs an adapter that cannot check remote state', async () => {
  let runs = 0;
  const result = await executeCheckin(params({ reconcile: true }), {
    resolveRunner: () => ({
      id: 'fake',
      run: async () => {
        runs += 1;
        return { status: 'success', message: 'should not run' };
      },
    }),
  });
  assert.equal(runs, 0);
  assert.equal(result.status, 'effect_unknown');
  assert.equal(result.reasonCode, 'reconcile_unavailable');
});

test('reconcile skips a confirmed prior effect without running again', async () => {
  let runs = 0;
  const result = await executeCheckin(params({ reconcile: true }), {
    resolveRunner: () => ({
      id: 'fake',
      checkAlreadyDone: async () => ({ done: true, conclusive: true }),
      run: async () => {
        runs += 1;
        return { status: 'success' };
      },
    }),
  });
  assert.equal(runs, 0);
  assert.equal(result.status, 'skipped');
  assert.equal(result.reasonCode, 'reconciled');
});

test('reconcile runs only after a conclusive not-done result', async () => {
  let runs = 0;
  const result = await executeCheckin(params({ reconcile: true }), {
    resolveRunner: () => ({
      id: 'fake',
      checkAlreadyDone: async () => false,
      run: async () => {
        runs += 1;
        return { status: 'success', message: 'done', duration_ms: 1 };
      },
    }),
  });
  assert.equal(runs, 1);
  assert.equal(result.status, 'success');
});

test('artifacts expose only controlled relative metadata', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'sitehub-artifact-root-'));
  const outsideRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'sitehub-artifact-outside-'));
  t.after(() => {
    fs.rmSync(root, { recursive: true, force: true });
    fs.rmSync(outsideRoot, { recursive: true, force: true });
  });
  const bytes = Buffer.from('proof');
  const proofPath = path.join(root, 'nested', 'proof.png');
  fs.mkdirSync(path.dirname(proofPath), { recursive: true });
  fs.writeFileSync(proofPath, bytes);

  const artifact = await describeArtifact(proofPath, root);
  assert.deepEqual(artifact, {
    kind: 'screenshot',
    role: 'screenshot',
    relativePath: 'nested/proof.png',
    sha256: createHash('sha256').update(bytes).digest('hex'),
    sizeBytes: bytes.length,
  });
  assert.equal('path' in artifact, false);

  const outsidePath = path.join(outsideRoot, 'outside.png');
  fs.writeFileSync(outsidePath, bytes);
  assert.equal(await describeArtifact(outsidePath, root), null);
});

test('a pre-navigation infrastructure failure remains safe to retry', async () => {
  const result = await executeCheckin(params(), {
    resolveRunner: () => ({
      id: 'fake', tracksRemoteEffect: true,
      run: async () => { throw new Error('browser launch failed'); },
    }),
  });
  assert.equal(result.status, 'failed');
  assert.equal(result.metadata.remoteEffect, 'not_started');
  assert.match(result.metadata.nextAction, /安全重试/);
});

test('a lost result after execution starts remains unknown', async () => {
  const result = await executeCheckin(params(), {
    resolveRunner: () => ({
      id: 'fake', tracksRemoteEffect: true,
      run: async site => {
        site.markRemoteEffect();
        throw new Error('response connection lost');
      },
    }),
  });
  assert.equal(result.status, 'effect_unknown');
  assert.equal(result.metadata.remoteEffect, 'possible');
});

test('blocked reconciliation never upgrades old uncertainty into a safe retry', async () => {
  const result = await executeCheckin(params({ reconcile: true }), {
    resolveRunner: () => ({ id: 'fake', tracksRemoteEffect: true, run: async () => assert.fail('must not execute') }),
  });
  assert.equal(result.metadata.remoteEffect, 'possible');
  assert.equal(result.status, 'effect_unknown');
});

test('a profile occupied by login prevents check-in before any adapter action', async () => {
  const { acquireProfile } = await import('../src/profile-locks.js');
  const input = params();
  const release = acquireProfile(input.accountId, 'login');
  try {
    const result = await executeCheckin(input, {
      resolveRunner: () => ({ id: 'fake', run: async () => assert.fail('must not execute') }),
    });
    assert.equal(result.status, 'failed');
    assert.equal(result.reasonCode, 'profile_busy');
    assert.equal(result.metadata.remoteEffect, 'not_started');
  } finally { release(); }
});
