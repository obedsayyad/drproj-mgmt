import { createHash, timingSafeEqual } from 'node:crypto';
import type { RequestHandler } from 'express';
import { Code, HttpProblem } from './problem.js';

const MUTATING = new Set(['POST', 'PATCH', 'PUT', 'DELETE']);
const digest = (s: string) => createHash('sha256').update(s).digest();

/**
 * Shared operator password for mutations (T31). Same credential Caddy checks; enforcing it here too
 * means reaching the container port directly does not bypass it. GETs stay open (docs/06).
 */
export function operatorAuth(password: string | null): RequestHandler {
  const expected = password === null ? null : digest(`operator:${password}`);
  return (req, _res, next) => {
    if (!expected || !MUTATING.has(req.method)) return next();
    const header = req.get('authorization') ?? '';
    const m = /^Basic ([A-Za-z0-9+/=]+)$/.exec(header);
    const given = m ? Buffer.from(m[1]!, 'base64').toString('utf8') : '';
    if (given && timingSafeEqual(digest(given), expected)) return next();
    next(new HttpProblem(401, Code.Unauthorized, 'Operator password required for changes.', { 'WWW-Authenticate': 'Basic realm="ops", charset="UTF-8"' }));
  };
}
