const SENSITIVE_HEADER = /\b(authorization|proxy-authorization|cookie|set-cookie)\b\s*[:=]\s*[^\r\n,]+/gi;
const SENSITIVE_ASSIGNMENT = /(["']?\b(?:token|access[_-]?token|refresh[_-]?token|secret|password|passwd|api[_-]?key|webhook(?:[_-]?(?:url|endpoint))?|endpoint)["']?\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}]+)/gi;
const JWT = /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/g;

export function redactText(value, maxLength = 2000) {
  let text = String(value ?? '');
  text = text.replace(/https?:\/\/[^\s"'<>]+/gi, raw => {
    try {
      const parsed = new URL(raw);
      const suffix = parsed.search || parsed.hash ? '?[redacted]' : '';
      return `${parsed.origin}${parsed.pathname}${suffix}`;
    } catch {
      return '[redacted-url]';
    }
  });
  text = text.replace(SENSITIVE_HEADER, (_, name) => `${name}=[redacted]`);
  text = text.replace(SENSITIVE_ASSIGNMENT, '$1[redacted]');
  text = text.replace(JWT, '[redacted-token]');
  return text.slice(0, maxLength);
}
