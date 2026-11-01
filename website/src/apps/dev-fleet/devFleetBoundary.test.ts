/**
 * Dev Fleet's UI is one directory the app can leave the repository with.
 *
 * The core SPA reaches it through exactly one import — the `/dev-fleet` row of
 * `apps/builtinRegistry.ts` — and nothing else outside `src/apps/dev-fleet/`
 * imports a module from it. The capture entries under `website/capture/` mount the
 * page directly on purpose and are outside `src/`, so they are not read here.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

const APP_DIR = __dirname
const SRC = resolve(APP_DIR, '..', '..')
const SPEC_RE = /(?:from\s+|import\s*\(?\s*|vi\.mock\(\s*)['"]([^'"]+)['"]/g

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(ts|tsx)$/.test(name)) out.push(p)
  }
  return out
}

function importsIntoTheApp(file: string): boolean {
  return [...readFileSync(file, 'utf8').matchAll(SPEC_RE)].some(([, spec]) => {
    const abs = spec.startsWith('@/') ? join(SRC, spec.slice(2)) : spec.startsWith('.') ? resolve(dirname(file), spec) : null
    return abs !== null && (abs === APP_DIR || abs.startsWith(APP_DIR + '/'))
  })
}

describe('Dev Fleet UI boundary', () => {
  it('is imported from outside its directory only by the builtin registry', () => {
    const outside = walk(SRC)
      .filter((p) => !p.startsWith(APP_DIR + '/'))
      .filter(importsIntoTheApp)
      .map((p) => relative(SRC, p).split('\\').join('/'))
    expect(outside).toEqual(['apps/builtinRegistry.ts'])
  })
})
