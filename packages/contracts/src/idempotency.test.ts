import { describe, expect, it } from 'vitest';
import { IdempotencyKey } from './idempotency.js';

describe('IdempotencyKey', () => {
  it.each([
    ['a'.repeat(8), true],
    ['a'.repeat(128), true],
    ['abc_DEF-123', true],
    ['a'.repeat(7), false],
    ['a'.repeat(129), false],
    ['has space1', false],
    ['semi;colon1', false],
    ['ünïcode-key', false],
    ['newline-key\n', false],
  ])('%j -> %s', (key, ok) => {
    expect(IdempotencyKey.safeParse(key).success).toBe(ok);
  });
});
