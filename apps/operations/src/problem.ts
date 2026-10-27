import type { Response } from 'express';
import { ProblemCode, type Problem } from '@drproj/contracts';

export const Code = {
  ...ProblemCode,
  ValidationFailed: 'VALIDATION_FAILED',
  NotFound: 'NOT_FOUND',
  VersionConflict: 'VERSION_CONFLICT',
  PayloadTooLarge: 'PAYLOAD_TOO_LARGE',
  RateLimited: 'RATE_LIMITED',
  Unauthorized: 'UNAUTHORIZED',
  PolicyInvalid: 'POLICY_INVALID',
  PolicyActivationInProgress: 'POLICY_ACTIVATION_IN_PROGRESS',
  PolicyNotInitialized: 'POLICY_NOT_INITIALIZED',
  OperationConflict: 'OPERATION_CONFLICT',
  BackupNotCaptured: 'BACKUP_NOT_CAPTURED',
  RestoreNotVerified: 'RESTORE_NOT_VERIFIED',
  RestoreSizeUnknown: 'RESTORE_SIZE_UNKNOWN',
  RestoreInsufficientHeadroom: 'RESTORE_INSUFFICIENT_HEADROOM',
  TestBoundsExceeded: 'TEST_BOUNDS_EXCEEDED',
  TestNotRunning: 'TEST_NOT_RUNNING',
  TooManyStreams: 'TOO_MANY_STREAMS',
  InternalError: 'INTERNAL_ERROR',
} as const;
export type Code = (typeof Code)[keyof typeof Code];

const TITLES: Record<number, string> = {
  400: 'Bad Request',
  401: 'Unauthorized',
  404: 'Not Found',
  409: 'Conflict',
  413: 'Payload Too Large',
  422: 'Unprocessable Content',
  429: 'Too Many Requests',
  500: 'Internal Server Error',
  503: 'Service Unavailable',
};

export class HttpProblem extends Error {
  constructor(
    readonly status: number,
    readonly code: Code,
    readonly detail?: string,
    readonly headers: Record<string, string> = {},
    readonly errors?: Problem['errors'],
  ) {
    super(detail ?? code);
  }
}

export function sendProblem(res: Response, p: HttpProblem, instance?: string): void {
  const body: Problem = {
    type: `https://ops.cproj.obed.one/problems/${p.code.toLowerCase().replaceAll('_', '-')}`,
    title: TITLES[p.status] ?? 'Error',
    status: p.status,
    code: p.code,
    ...(p.detail === undefined ? {} : { detail: p.detail }),
    ...(instance === undefined ? {} : { instance }),
    ...(p.errors === undefined ? {} : { errors: p.errors }),
  };
  for (const [k, v] of Object.entries(p.headers)) res.setHeader(k, v);
  res.status(p.status).type('application/problem+json').send(JSON.stringify(body));
}
