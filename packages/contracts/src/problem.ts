import { z } from 'zod';

export const ProblemCode = {
  MaintenanceWritesBlocked: 'MAINTENANCE_WRITES_BLOCKED',
  DependencyUnavailable: 'DEPENDENCY_UNAVAILABLE',
  IdempotencyKeyReused: 'IDEMPOTENCY_KEY_REUSED',
  IdempotencyInFlight: 'IDEMPOTENCY_IN_FLIGHT',
} as const;

/** RFC 9457 problem details (application/problem+json). */
export const Problem = z.looseObject({
  type: z.string(),
  title: z.string(),
  status: z.int(),
  detail: z.string().optional(),
  instance: z.string().optional(),
  code: z.string(),
  errors: z.array(z.object({ field: z.string().optional(), message: z.string().optional() })).optional(),
});
export type Problem = z.infer<typeof Problem>;
