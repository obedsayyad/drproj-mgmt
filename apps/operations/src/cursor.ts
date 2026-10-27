const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export interface CursorPos {
  createdAt: string;
  id: string;
}

export const encodeCursor = (p: CursorPos): string => Buffer.from(JSON.stringify([p.createdAt, p.id])).toString('base64url');

/** Returns null for anything that is not a cursor this API issued. */
export function decodeCursor(raw: string): CursorPos | null {
  if (raw.length > 200) return null;
  try {
    const v: unknown = JSON.parse(Buffer.from(raw, 'base64url').toString('utf8'));
    if (Array.isArray(v) && v.length === 2 && typeof v[0] === 'string' && typeof v[1] === 'string' && ISO.test(v[0]) && UUID.test(v[1])) {
      return { createdAt: v[0], id: v[1] };
    }
  } catch {
    // fall through
  }
  return null;
}

export const isUuid = (s: string) => UUID.test(s);
