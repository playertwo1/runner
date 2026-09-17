import { register } from 'node:module';
import { describe, it } from 'node:test';
import assert from 'node:assert';

register('./test-loader.mjs', import.meta.url);

globalThis.describe = describe;
globalThis.it = it;
globalThis.expect = (actual) => ({
  toBe: (expected) => assert.strictEqual(actual, expected),
  toEqual: (expected) => assert.deepStrictEqual(actual, expected),
  toContain: (expected) => assert.ok(actual && actual.includes(expected), `Expected '${actual}' to contain '${expected}'`),
});
