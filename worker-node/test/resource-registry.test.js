import test from 'node:test';
import assert from 'node:assert/strict';

import {
  closeAllTrackedResources,
  closeTrackedResource,
  trackCloseable,
  trackedResourceCount,
} from '../src/resource-registry.js';

test('tracked browser and OCR resources close once and are released', async () => {
  let closes = 0;
  const resource = trackCloseable({
    close: async () => { closes += 1; },
  }, 'test-resource');
  assert.equal(trackedResourceCount(), 1);

  await Promise.all([
    closeTrackedResource(resource),
    closeAllTrackedResources(),
  ]);
  assert.equal(closes, 1);
  assert.equal(trackedResourceCount(), 0);
});
