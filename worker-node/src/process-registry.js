import { spawn } from 'child_process';

const trackedProcesses = new Map();

function processExists(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function waitForExit(child, timeoutMs) {
  if (!child || !processExists(child.pid)) return Promise.resolve();
  return new Promise(resolve => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.off?.('exit', finish);
      child.off?.('close', finish);
      resolve();
    };
    const timer = setTimeout(finish, Math.max(0, timeoutMs));
    timer.unref?.();
    child.once?.('exit', finish);
    child.once?.('close', finish);
  });
}

function taskkill(pid, force) {
  return new Promise(resolve => {
    const args = ['/PID', String(pid), '/T'];
    if (force) args.push('/F');
    let killer;
    try {
      killer = spawn('taskkill.exe', args, {
        stdio: 'ignore',
        windowsHide: true,
      });
    } catch {
      resolve();
      return;
    }
    killer.once('error', resolve);
    killer.once('exit', resolve);
  });
}

/**
 * Register a child process owned by the worker. Only exact spawned PIDs are
 * recorded, so shutdown never falls back to killing processes by image name.
 */
export function trackChildProcess(child, label = 'child') {
  if (!child || typeof child.once !== 'function') return child;
  trackedProcesses.set(child, { label: String(label), pid: child.pid || null });
  const forget = () => trackedProcesses.delete(child);
  child.once('exit', forget);
  child.once('error', () => {
    // Spawn failures have no live PID. A later kill error must not make us
    // forget a process that may still require the force-cleanup pass.
    if (!processExists(child.pid)) forget();
  });
  return child;
}

/**
 * Best-effort bounded cleanup for Chrome, OCR, and other worker children.
 * Windows uses taskkill on the exact PID so descendants are included.
 */
export async function terminateTrackedProcesses({ graceMs = 1500 } = {}) {
  const children = [...trackedProcesses.keys()];
  if (!children.length) return;
  const boundedGraceMs = Math.max(0, Math.min(Number(graceMs) || 0, 10000));

  if (process.platform === 'win32') {
    await Promise.allSettled(children.map(child => (
      processExists(child.pid) ? taskkill(child.pid, false) : Promise.resolve()
    )));
  }
  // taskkill can be unavailable or policy-restricted. Always retain the
  // direct ChildProcess handle as a same-owner fallback.
  for (const child of children) {
    if (!processExists(child.pid)) continue;
    try { child.kill('SIGTERM'); } catch { /* best effort */ }
  }

  await Promise.allSettled(children.map(child => waitForExit(child, boundedGraceMs)));
  const remaining = children.filter(child => processExists(child.pid));

  if (process.platform === 'win32') {
    await Promise.allSettled(remaining.map(child => taskkill(child.pid, true)));
  }
  for (const child of remaining) {
    if (!processExists(child.pid)) continue;
    try { child.kill('SIGKILL'); } catch { /* best effort */ }
  }

  await Promise.allSettled(remaining.map(child => waitForExit(child, 1000)));
  for (const child of children) {
    if (!processExists(child.pid)) trackedProcesses.delete(child);
  }
}

export function trackedProcessCount() {
  return trackedProcesses.size;
}
