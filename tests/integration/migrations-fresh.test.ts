// Guards against a stale copy of the migrations SQL: the sibling drproj-app tree is the only source.
import { createHash } from 'node:crypto';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const SOURCE = fileURLToPath(new URL('../../../drproj-app/database/migrations/sql', import.meta.url));
const INSTALLED = fileURLToPath(new URL('./node_modules/@drproj/migrations/sql', import.meta.url));

function treeHash(root: string): string {
  const h = createHash('sha256');
  const walk = (dir: string) => {
    for (const name of readdirSync(dir).sort()) {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) walk(full);
      else h.update(full.slice(root.length)).update('\0').update(readFileSync(full)).update('\0');
    }
  };
  walk(root);
  return h.digest('hex');
}

describe('migrations SQL', () => {
  it('sibling drproj-app checkout exists', () => {
    expect(existsSync(SOURCE), `${SOURCE} missing: check out drproj-app next to drproj-mgmt`).toBe(true);
  });

  it('installed @drproj/migrations/sql (if any) matches ../drproj-app', () => {
    if (!existsSync(INSTALLED)) return; // symlinked or absent installs have nothing to drift
    expect(treeHash(INSTALLED), 'stale node_modules copy: run `pnpm install --force`').toBe(treeHash(SOURCE));
  });
});
