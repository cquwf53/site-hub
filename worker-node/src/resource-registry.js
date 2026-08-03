const resources = new Map();

export function trackCloseable(resource, label = 'resource', close) {
  if (!resource) return resource;
  const closeResource = close || (value => value.close());
  resources.set(resource, {
    label: String(label),
    close: () => closeResource(resource),
    closing: null,
  });
  return resource;
}

export async function closeTrackedResource(resource) {
  const record = resources.get(resource);
  if (!record) return false;
  if (!record.closing) {
    record.closing = Promise.resolve()
      .then(record.close)
      .finally(() => resources.delete(resource));
  }
  await record.closing;
  return true;
}

function bounded(promise, timeoutMs) {
  return new Promise(resolve => {
    const timer = setTimeout(resolve, timeoutMs);
    Promise.resolve(promise).then(
      () => { clearTimeout(timer); resolve(); },
      () => { clearTimeout(timer); resolve(); },
    );
  });
}

export async function closeAllTrackedResources({ timeoutMs = 3000 } = {}) {
  const boundedTimeout = Math.max(1, Math.min(Number(timeoutMs) || 3000, 10000));
  const pending = [...resources.keys()].map(resource => closeTrackedResource(resource));
  await Promise.allSettled(pending.map(promise => bounded(promise, boundedTimeout)));
}

export function trackedResourceCount() {
  return resources.size;
}
