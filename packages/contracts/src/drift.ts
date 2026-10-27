// Compile-time guard: tsc fails if the zod schemas diverge from docs/spec/openapi.yaml.
import type { z } from 'zod';
import type { components } from './generated/openapi.js';
import type { PolicyConfiguration, PolicyConfigurationPatch, PolicyPatch } from './policy.js';

type Equal<A, B> = (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2 ? true : false;
type Expect<T extends true> = T;
type Schemas = components['schemas'];

/**
 * openapi-typescript renders `integer` with min/max (and enum-of-one) as `number`, so zod's narrower
 * literals can never be Equal to it. Two checks instead: zod's output is assignable to the spec type
 * (zod is never looser than the spec), and both have exactly the same deep key paths (nothing added,
 * dropped or renamed on either side). Optional keys count as keys.
 */
type Paths<T, P extends string = ''> = T extends readonly unknown[]
  ? never
  : T extends object
    ? { [K in keyof T & string]-?: `${P}${K}` | Paths<Exclude<T[K], undefined>, `${P}${K}.`> }[keyof T & string]
    : never;

type Shape<T> = { [K in keyof T]: T[K] };
type Conforms<Zod, Spec> = Shape<Zod> extends Spec ? true : false;

export type Drift = [
  Expect<Conforms<z.infer<typeof PolicyConfiguration>, Schemas['PolicyConfiguration']>>,
  Expect<Equal<Paths<z.infer<typeof PolicyConfiguration>>, Paths<Schemas['PolicyConfiguration']>>>,
  Expect<Conforms<z.infer<typeof PolicyConfigurationPatch>, Schemas['PolicyConfigurationPatch']>>,
  Expect<Equal<Paths<z.infer<typeof PolicyConfigurationPatch>>, Paths<Schemas['PolicyConfigurationPatch']>>>,
  Expect<Conforms<z.infer<typeof PolicyPatch>, Schemas['PolicyPatch']>>,
  Expect<Equal<Paths<z.infer<typeof PolicyPatch>>, Paths<Schemas['PolicyPatch']>>>,
];
