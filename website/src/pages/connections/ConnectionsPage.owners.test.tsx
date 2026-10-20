/**
 * The Connections page's facade and its owners beside it.
 *
 * ConnectionsPage.tsx stays the page's one import path: CapabilitiesPage
 * renders its default export, three CapabilitiesPage specs
 * `vi.mock('../pages/connections/ConnectionsPage')`, and the page's specs and
 * the capture entry import its named exports. A provider card renders from
 * `ConnectionsPageCard.tsx`, and what the card shows is decided in
 * `connectionsPageCardState.ts`. These pins keep that shape honest:
 *
 *  - the facade still exports what it exported, each moved value as the
 *    owner's own binding;
 *  - imports run one way: the facade imports the owners, the card imports the
 *    state module, and neither owner imports the facade (a mock of the page
 *    then still replaces all of it);
 *  - nothing outside the page imports an owner.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'

import * as facade from './ConnectionsPage'
import * as cardState from './connectionsPageCardState'
import { pageFamilyFiles } from '../../test/pageFamily'

const DIR = __dirname
const SRC = resolve(DIR, '..', '..')
const CAPTURE = resolve(SRC, '..', 'capture')
const FACADE = join(DIR, 'ConnectionsPage.tsx')
const CARD = join(DIR, 'ConnectionsPageCard.tsx')
const STATE = join(DIR, 'connectionsPageCardState.ts')
const owners = pageFamilyFiles('ConnectionsPage', DIR).slice(1)
const read = (p: string) => readFileSync(p, 'utf8')

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(ts|tsx)$/.test(name) && !/\.test\.|\.spec\./.test(name)) out.push(p)
  }
  return out
}

/** Every module a file imports (static, side-effect, `export … from` and
 *  dynamic), resolved to an absolute path without its extension. `@/` is the
 *  `src/` alias. */
const SPEC_RE = /(?:from\s+|import\s*\(?)\s*['"]([^'"]+)['"]/g
const stem = (p: string) => p.replace(/\.(tsx?|jsx?)$/, '').replace(/[\\/]index$/, '')
function importsIn(file: string, text: string): string[] {
  return [...text.matchAll(SPEC_RE)].map((m) => m[1]).flatMap((spec) => {
    if (spec.startsWith('@/')) return [stem(join(SRC, spec.slice(2)))]
    if (spec.startsWith('.')) return [stem(resolve(dirname(file), spec))]
    return []
  })
}
const importsOf = (file: string) => importsIn(file, read(file))

/** The values the page exported as one file, every one now the state module's. */
const MOVED_VALUE_EXPORTS = [
  'confirmedGrantPresent', 'connectionStateFor', 'disconnectFeedback', 'effectiveOAuth',
  'errorIndicatesProviderRejection', 'latestOAuthByServer', 'mintOutcome', 'probeIndicatesConnected',
  'uninstallOnCancel', 'withMintedUrl',
] as const

describe('ConnectionsPage facade', () => {
  it('keeps every export it had, the moved ones as the owner’s own bindings', () => {
    expect(Object.keys(facade)).toEqual(expect.arrayContaining(['default', ...MOVED_VALUE_EXPORTS]))
    expect(typeof facade.default).toBe('function')
    for (const name of MOVED_VALUE_EXPORTS) {
      expect(typeof facade[name], name).toBe('function')
      expect(facade[name], name).toBe(cardState[name])
    }
  })
})

describe('ConnectionsPage owners', () => {
  it('are the two files beside the facade', () => {
    expect(owners).toEqual(['ConnectionsPageCard.tsx', 'connectionsPageCardState.ts'])
  })

  it('include every sibling the facade imports under the page’s name', () => {
    // A part named off the family pattern would drop out of `readPageFamily`,
    // and every pin that reads the page through it would stop seeing that part.
    const named = /^(ConnectionsPage|connectionsPage|useConnectionsPage)[A-Z.]/
    const family = new Set(owners.map((f) => stem(join(DIR, f))))
    const siblings = importsOf(FACADE).filter((t) => dirname(t) === DIR && named.test(t.slice(DIR.length + 1)))
    expect(siblings.length).toBeGreaterThan(0)
    expect(siblings.filter((t) => !family.has(t))).toEqual([])
  })

  it('never import the facade, and the state module imports neither file', () => {
    for (const f of owners) {
      expect(importsOf(join(DIR, f)), f).not.toContain(stem(FACADE))
    }
    expect(importsOf(STATE)).not.toContain(stem(CARD))
    expect(importsOf(CARD)).toContain(stem(STATE))
    expect(importsOf(FACADE)).toEqual(expect.arrayContaining([stem(CARD), stem(STATE)]))
  })

  it('are imported only by the facade and by each other', () => {
    const family = new Set([FACADE, ...owners.map((f) => join(DIR, f))])
    const ownerStems = new Set(owners.map((f) => stem(join(DIR, f))))
    const outside = [...walk(SRC), ...walk(CAPTURE)]
      .filter((p) => !family.has(p))
      .filter((p) => importsOf(p).some((s) => ownerStems.has(s)))
      .map((p) => relative(SRC, p))
    expect(outside).toEqual([])
  })

  it('reads imports the way the pins above rely on', () => {
    const probe = join(DIR, 'probe.tsx')
    const text = [
      "import './Side'",
      "import type { T } from '@/pages/connections/Typed'",
      "export { X } from './Re'",
      "const L = () => import('./Lazy')",
      "import { A,\n  B } from '../Multi'",
    ].join('\n')
    expect(importsIn(probe, text)).toEqual([
      join(DIR, 'Side'), join(DIR, 'Typed'), join(DIR, 'Re'), join(DIR, 'Lazy'), join(dirname(DIR), 'Multi'),
    ])
    expect(stem(join(DIR, 'Card.tsx'))).toBe(join(DIR, 'Card'))
    expect(stem(join(DIR, 'state', 'index.ts'))).toBe(join(DIR, 'state'))
  })
})
