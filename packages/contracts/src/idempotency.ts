import { z } from 'zod';

export const IDEMPOTENCY_KEY_PATTERN = /^[A-Za-z0-9_-]{8,128}$/;
export const IDEMPOTENCY_KEY_HEADER = 'Idempotency-Key';

export const IdempotencyKey = z.string().regex(IDEMPOTENCY_KEY_PATTERN);
