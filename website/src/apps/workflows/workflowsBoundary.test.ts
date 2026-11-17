/**
 * The Workflows app's UI is one directory the app can leave the repository with.
 *
 * The workflow run views the chat, the sidebar and Customize > Workflows render
 * (`components/workflows/`) are core: they read the core run registry at
 * `/api/workflows/*`, so they never live under the app. The core SPA reaches the
 * app through exactly one import, the `/workflows` row of `apps/builtinRegistry.ts`
 * that lazy-loads its page, and nothing else outside `src/apps/workflows/` imports a
 * module from it. The app's tests live beside it.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, isAbsolute, join, relative, resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import { BUILTIN_COMPONENT_REGISTRY } from '../builtinRegistry'

const APP_DIR = __dirname
const SRC = resolve(APP_DIR, '..', '..')
const SPEC_RE = /(?:from\s+|import\s*\(?\s*|vi\.mock\(\s*|import\.meta\.glob\(\s*)['"`]([^'"`]+)['"`]/g

function insideTheApp(p: string): boolean {
  const rel = relative(APP_DIR, p)
  return rel === '' || (!rel.startsWith('..') && !isAbsolute(rel))
}

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(ts|tsx)$/.test(name)) out.push(p)
  }
  return out
}

function resolveSpec(file: string, spec: string): string | null {
  if (spec.startsWith('@/')) return join(SRC, spec.slice(2))
  if (spec.startsWith('.')) return resolve(dirname(file), spec)
  return null
}

function importsIntoTheApp(file: string): boolean {
  return [...readFileSync(file, 'utf8').matchAll(SPEC_RE)].some(([, spec]) => {
    const abs = resolveSpec(file, spec)
    return abs !== null && insideTheApp(abs)
  })
}

describe('Workflows app UI boundary', () => {
  it('is imported from outside its directory only by the builtin registry', () => {
    const outside = walk(SRC)
      .filter((p) => !insideTheApp(p))
      .filter(importsIntoTheApp)
      .map((p) => relative(SRC, p).split('\\').join('/'))
    expect(outside).toEqual(['apps/builtinRegistry.ts'])
  })

  it('is the page its registry row lazy-loads, under the app id', () => {
    expect(BUILTIN_COMPONENT_REGISTRY['/workflows']?.appId).toBe('workflows')
  })

  it('catches every import spelling it guards', () => {
    const probe = join(SRC, 'test', 'probe.ts')
    for (const spec of [
      "from '../apps/workflows/WorkflowsPage'",
      "import('@/apps/workflows/WorkflowsPage')",
      "vi.mock('../apps/workflows/WorkflowsPage'",
      'import(`../apps/workflows/${path}?raw`)',
      "import.meta.glob('../apps/workflows/*.tsx'",
    ]) {
      const [, path] = [...spec.matchAll(SPEC_RE)][0] ?? []
      expect(path, spec).toBeDefined()
      const abs = resolveSpec(probe, path)
      expect(abs !== null && insideTheApp(abs), spec).toBe(true)
    }
  })
})
