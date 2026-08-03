import test from 'node:test';
import assert from 'node:assert/strict';

import { redactText } from '../src/redact.js';

test('redaction removes URL query strings, fragments and assigned secrets', () => {
  const output = redactText(
    'request https://example.test/path?a=plain-secret#token failed; Authorization: Bearer auth-secret\n'
      + 'Cookie=session-456\nwebhook_url=https://hooks.example.test/private/key\nX-Api-Key=api-secret',
  );
  assert.match(output, /https:\/\/example\.test\/path\?\[redacted\]/);
  assert.doesNotMatch(output, /plain-secret|token failed|auth-secret|session-456|hooks\.example|api-secret/);
  assert.match(output, /Authorization=\[redacted\]/i);
  assert.match(output, /Cookie=\[redacted\]/i);
  assert.match(output, /webhook_url=\[redacted\]/i);
  assert.match(output, /X-Api-Key=\[redacted\]/i);
});
