import test from 'node:test';
import assert from 'node:assert/strict';

import { adapterManifests } from '../src/adapter-manifests.js';

function schemaAtPath(schema, dottedPath) {
  return dottedPath.split('.').reduce(
    (current, part) => current?.properties?.[part],
    schema,
  );
}

test('every declared secret field is represented by a structured account schema path', () => {
  for (const manifest of adapterManifests) {
    for (const secretField of manifest.secretFields) {
      assert.ok(
        schemaAtPath(manifest.accountConfigSchema, secretField),
        `${manifest.adapterId} is missing schema path ${secretField}`,
      );
    }
  }
});

test('HTTP adapters model token-bearing request bodies as objects', () => {
  for (const adapterId of ['api', 'cookie']) {
    const manifest = adapterManifests.find((item) => item.adapterId === adapterId);
    assert.equal(manifest.accountConfigSchema.properties.body.type, 'object');
    assert.equal(
      manifest.accountConfigSchema.properties.body.properties.token.type,
      'string',
    );
    assert.ok(manifest.secretFields.includes('body.token'));
  }
});
