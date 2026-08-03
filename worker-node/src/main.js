import util from 'util';
import { adapterManifests } from './adapter-manifests.js';
import { executeCheckin } from './checkin/runner.js';
import { ddddocrRunner } from './checkin/ddddocr-runner.js';
import { loginSessionManager } from './checkin/login-session-manager.js';
import { terminateTrackedProcesses } from './process-registry.js';
import { closeAllTrackedResources } from './resource-registry.js';
import { registerProfileDir } from './runtime-paths.js';
import { JsonRpcPeer, PROTOCOL_VERSION } from './protocol.js';
import { redactText } from './redact.js';

function stderr(...args) {
  process.stderr.write(`${redactText(util.format(...args), 4000)}\n`);
}

// Adapter code historically uses console.log. Human-readable output must never
// share stdout with the JSONL protocol.
console.log = stderr;
console.info = stderr;
console.warn = stderr;
console.error = stderr;

const peer = new JsonRpcPeer({ input: process.stdin, output: process.stdout });
let activeJob = null;
let handshakeReady = false;
let shuttingDown = false;
let cleanupPromise = null;
let shutdownFallbackTimer = null;
let requestedExitCode = 0;

function asSite(params) {
  const account = params.account || {};
  const site = params.site || {};
  const accountId = String(params.accountId || account.id || '');
  if (!accountId || !site.url) throw new Error('accountId and site.url are required');
  registerProfileDir(accountId, params.profileDir);
  return {
    id: accountId,
    name: account.name ? `${site.name} / ${account.name}` : site.name,
    url: site.url,
    mode: account.mode || params.checkinConfig?.mode || 'playwright',
    config: { ...(params.checkinConfig || {}) },
  };
}

async function cleanupOwnedProcesses() {
  await loginSessionManager.closeAll().catch(error => {
    stderr('[worker] login-session cleanup failed:', error?.message || error);
  });
  try {
    ddddocrRunner.shutdown();
  } catch (error) {
    stderr('[worker] OCR cleanup failed:', error?.message || error);
  }
  await closeAllTrackedResources().catch(error => {
    stderr('[worker] browser/OCR resource cleanup failed:', error?.message || error);
  });
  await terminateTrackedProcesses().catch(error => {
    stderr('[worker] child-process cleanup failed:', error?.message || error);
  });
}

function terminateWorker(exitCode, reason) {
  if (Number(exitCode) !== 0) requestedExitCode = Number(exitCode) || 1;
  shuttingDown = true;
  handshakeReady = false;
  clearTimeout(shutdownFallbackTimer);

  if (activeJob) {
    activeJob.cancelRequested = true;
    activeJob.rejectCancellation?.();
  }
  if (cleanupPromise) return cleanupPromise;

  if (reason) stderr(`[worker] shutting down: ${reason}`);
  cleanupPromise = (async () => {
    // Keep a final bound even if a browser API hangs while closing.
    const forceExit = setTimeout(() => process.exit(requestedExitCode), 12000);
    try {
      await cleanupOwnedProcesses();
    } finally {
      clearTimeout(forceExit);
      peer.close();
      process.exit(requestedExitCode);
    }
  })();
  return cleanupPromise;
}

async function sendCompletion(params) {
  let lastError;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      return await peer.request('job.complete', params, 5000);
    } catch (error) {
      lastError = error;
      await new Promise(resolve => setTimeout(resolve, 250 * (attempt + 1)));
    }
  }
  throw lastError;
}

async function runJob(params) {
  const started = Date.now();
  const jobId = String(params.jobId || '');
  const leaseToken = String(params.leaseToken || '');
  const accountId = String(params.accountId || params.account?.id || '');
  let heartbeat;
  let deadlineTimer;
  let timedOut = false;
  let cancelled = false;
  let recycleExitCode = null;

  try {
    if (params.profileDir) registerProfileDir(accountId, params.profileDir);
    heartbeat = setInterval(() => {
      peer.notify('job.heartbeat', {
        jobId,
        leaseToken,
        accountId,
        phase: 'executing',
        at: new Date().toISOString(),
      });
    }, 10000);
    heartbeat.unref?.();

    const deadlineMs = params.deadlineAt
      ? Math.max(1, new Date(params.deadlineAt).getTime() - Date.now())
      : 180000;
    const deadline = new Promise((_, reject) => {
      deadlineTimer = setTimeout(() => {
        timedOut = true;
        reject(new Error('Job deadline exceeded'));
      }, deadlineMs);
    });
    const cancellation = new Promise((_, reject) => {
      if (activeJob?.jobId === jobId) {
        activeJob.rejectCancellation = () => {
          cancelled = true;
          reject(new Error('Job cancelled'));
        };
      }
    });

    const result = await Promise.race([executeCheckin(params), deadline, cancellation]);
    if (activeJob?.cancelRequested) {
      result.status = 'effect_unknown';
      result.reasonCode = 'cancel_requested';
      result.message = 'Cancellation requested after execution began; remote outcome requires reconciliation';
    }
    await sendCompletion({ jobId, leaseToken, accountId, ...result });
  } catch (error) {
    const result = {
      jobId,
      leaseToken,
      accountId,
      status: activeJob?.cancelRequested || timedOut ? 'effect_unknown' : 'failed',
      reasonCode: activeJob?.cancelRequested ? 'cancel_requested' : timedOut ? 'timeout' : 'adapter_error',
      message: redactText(error?.message || error, 1000),
      durationMs: Date.now() - started,
      artifacts: [],
      metadata: {},
    };
    try {
      await sendCompletion(result);
    } catch (ackError) {
      stderr('[worker] completion was not acknowledged:', ackError.message);
      recycleExitCode = 74;
    }
  } finally {
    clearInterval(heartbeat);
    clearTimeout(deadlineTimer);
    activeJob = null;
    if (shuttingDown) {
      await terminateWorker(requestedExitCode, 'graceful shutdown completed');
    } else if (timedOut || cancelled || recycleExitCode !== null) {
      recycleExitCode ??= timedOut ? 124 : cancelled ? 75 : 74;
      await terminateWorker(recycleExitCode, 'worker recycle required');
    }
  }
}

peer.handle('worker.ping', async () => ({
  ok: true,
  pid: process.pid,
  activeJobId: activeJob?.jobId || null,
  at: new Date().toISOString(),
}));

peer.handle('job.start', async params => {
  if (!handshakeReady) throw new Error('Worker handshake is not complete');
  if (shuttingDown) throw new Error('Worker is shutting down');
  if (activeJob) throw new Error(`Worker busy with ${activeJob.jobId}`);
  if (!params.jobId || !params.leaseToken || !params.accountId) {
    throw new Error('jobId, leaseToken and accountId are required');
  }
  activeJob = { jobId: String(params.jobId), cancelRequested: false };
  void runJob(params);
  return { accepted: true, jobId: activeJob.jobId };
});

peer.handle('job.cancel', async params => {
  if (!activeJob || activeJob.jobId !== String(params.jobId)) return { accepted: false };
  activeJob.cancelRequested = true;
  activeJob.rejectCancellation?.();
  return { accepted: true };
});

peer.handle('session.open', async params => loginSessionManager.startSystemChrome(asSite(params)));
peer.handle('session.status', async params => loginSessionManager.getStatus(asSite(params)));
peer.handle('session.close', async params => ({
  closed: await loginSessionManager.close(String(params.accountId || params.account?.id || '')),
}));

peer.handle('worker.shutdown', async params => {
  shuttingDown = true;
  handshakeReady = false;
  const hadActiveJob = Boolean(activeJob);
  if (activeJob) {
    activeJob.cancelRequested = true;
    activeJob.rejectCancellation?.();
  }
  const graceMs = Math.max(0, Math.min(Number(params.graceMs ?? 20000), 60000));
  shutdownFallbackTimer = setTimeout(
    () => void terminateWorker(0, 'shutdown grace period elapsed'),
    hadActiveJob ? graceMs : 10,
  );
  return { accepted: true };
});

process.on('SIGINT', () => void terminateWorker(0, 'received SIGINT'));
process.on('SIGTERM', () => void terminateWorker(0, 'received SIGTERM'));
process.on('uncaughtException', error => {
  stderr('[worker] uncaught exception:', error?.stack || error?.message || error);
  void terminateWorker(1, 'uncaught exception');
});
process.on('unhandledRejection', error => {
  stderr('[worker] unhandled rejection:', error?.stack || error);
  void terminateWorker(1, 'unhandled rejection');
});

async function performHandshake() {
  try {
    const helloAck = await peer.request('worker.hello', {
      protocol: PROTOCOL_VERSION,
      pid: process.pid,
      build: '0.1.0',
      capabilities: ['checkin', 'login_session'],
      adapters: adapterManifests,
    }, 10000);
    if (Number(helloAck?.protocol) !== PROTOCOL_VERSION) {
      throw new Error(`Protocol mismatch: expected ${PROTOCOL_VERSION}, received ${helloAck?.protocol}`);
    }

    const readyAck = await peer.request('worker.ready', {
      protocol: PROTOCOL_VERSION,
      pid: process.pid,
      at: new Date().toISOString(),
    }, 10000);
    if (readyAck?.accepted !== true) throw new Error('Control plane rejected worker.ready');
    handshakeReady = true;
  } catch (error) {
    stderr('[worker] handshake failed:', error?.message || error);
    await terminateWorker(78, 'handshake failed');
  }
}

void performHandshake();
