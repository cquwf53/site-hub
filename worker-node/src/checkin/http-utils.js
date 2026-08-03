const DEFAULT_MAX_BYTES = 1024 * 1024;

function validateHttpUrl(value) {
  const parsed = new URL(value);
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error('Only HTTP and HTTPS targets are allowed');
  }
  return parsed;
}

function withoutSensitiveCrossOriginHeaders(headers) {
  const cleaned = new Headers(headers || {});
  for (const name of ['authorization', 'cookie', 'proxy-authorization']) cleaned.delete(name);
  return cleaned;
}

async function fetchWithRedirectLimit(url, options, signal, maxRedirects) {
  let target = validateHttpUrl(url);
  let requestOptions = { ...options, redirect: 'manual', signal };

  for (let redirectCount = 0; ; redirectCount += 1) {
    const response = await fetch(target, requestOptions);
    if (![301, 302, 303, 307, 308].includes(response.status)) return response;

    const location = response.headers.get('location');
    if (!location) return response;
    if (redirectCount >= maxRedirects) {
      await response.body?.cancel().catch(() => {});
      throw new Error(`Redirect limit exceeded (${maxRedirects})`);
    }

    const next = validateHttpUrl(new URL(location, target));
    const nextOptions = { ...requestOptions };
    if (next.origin !== target.origin) {
      nextOptions.headers = withoutSensitiveCrossOriginHeaders(nextOptions.headers);
    }
    const method = String(nextOptions.method || 'GET').toUpperCase();
    if (response.status === 303 || ((response.status === 301 || response.status === 302) && method === 'POST')) {
      nextOptions.method = 'GET';
      delete nextOptions.body;
      const headers = new Headers(nextOptions.headers || {});
      headers.delete('content-length');
      headers.delete('content-type');
      nextOptions.headers = headers;
    }
    await response.body?.cancel().catch(() => {});
    target = next;
    requestOptions = nextOptions;
  }
}

export async function fetchTextWithLimits(url, options = {}, limits = {}) {
  const timeoutMs = Math.max(1000, Math.min(Number(limits.timeoutMs || 30000), 120000));
  const maxBytes = Math.max(1024, Math.min(Number(limits.maxBytes || DEFAULT_MAX_BYTES), 4 * DEFAULT_MAX_BYTES));
  const maxRedirects = Math.max(0, Math.min(Number(limits.maxRedirects ?? 5), 10));
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new Error('Request timeout')), timeoutMs);
  try {
    const response = await fetchWithRedirectLimit(url, options, controller.signal, maxRedirects);
    const declared = Number(response.headers.get('content-length') || 0);
    if (declared > maxBytes) throw new Error(`Response exceeds ${maxBytes} bytes`);
    const reader = response.body?.getReader();
    if (!reader) return { response, text: '' };
    const chunks = [];
    let size = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maxBytes) {
        await reader.cancel();
        throw new Error(`Response exceeds ${maxBytes} bytes`);
      }
      chunks.push(value);
    }
    const merged = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) {
      merged.set(chunk, offset);
      offset += chunk.byteLength;
    }
    return { response, text: new TextDecoder().decode(merged) };
  } finally {
    clearTimeout(timer);
  }
}

export function parseResponsePayload(text) {
  try { return JSON.parse(text); } catch { return text; }
}

export function assessBusinessSuccess(payload, text, successKeyword = 'success') {
  if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
    for (const key of ['success', 'ok']) {
      if (typeof payload[key] === 'boolean') return payload[key];
    }
    if (typeof payload.code === 'number') return payload.code === 0 || payload.code === 200;
    if (typeof payload.status === 'string') {
      const status = payload.status.toLowerCase();
      if (['success', 'ok', 'completed'].includes(status)) return true;
      if (['failed', 'error', 'denied'].includes(status)) return false;
    }
  }
  const keyword = String(successKeyword || '').trim().toLowerCase();
  return Boolean(keyword) && String(text || '').toLowerCase().includes(keyword);
}

export function isAlreadyDone(payload, text) {
  const value = typeof payload === 'string' ? payload : `${payload?.message || ''} ${text || ''}`;
  return /(already|checked\s*in|signed\s*in|已签到|今日已签|已经签到)/i.test(value);
}
