import test from 'node:test';
import assert from 'node:assert/strict';

const { buildMousePath } = await import('../src/checkin/human-mouse.js');

test('mouse path ends exactly on the target without jitter', () => {
  const path = buildMousePath({ x: 0, y: 0 }, { x: 300, y: 200 });
  const last = path[path.length - 1];
  assert.equal(last.x, 300);
  assert.equal(last.y, 200);
});

test('mouse path length stays within human-like bounds', () => {
  for (const target of [{ x: 10, y: 5 }, { x: 1200, y: 600 }]) {
    const path = buildMousePath({ x: 0, y: 0 }, target);
    assert.ok(path.length >= 12 && path.length <= 40);
    for (const point of path) {
      assert.ok(Number.isFinite(point.x) && Number.isFinite(point.y));
    }
  }
});

test('mouse path is deterministic with an injected random source', () => {
  const alwaysMin = min => min;
  const first = buildMousePath({ x: 5, y: 5 }, { x: 400, y: 300 }, alwaysMin);
  const second = buildMousePath({ x: 5, y: 5 }, { x: 400, y: 300 }, alwaysMin);
  assert.deepEqual(first, second);
});
