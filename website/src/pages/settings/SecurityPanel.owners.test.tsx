/**
 * SecurityPanel.tsx and the owners beside it.
 *
 * The facade stays the panel's one import path (Settings, the stories and
 * captures that mount it, and the specs that `vi.mock` it by that id). These
 * pins keep that shape honest:
 *
 *  - the facade still exports every name it exported as one file, each the
 *    very binding its owner defines;
 *  - owners never import the facade, and nothing outside the panel (bar its
 *    own specs) imports an owner, so a mock of the facade still replaces the
 *    whole panel.
 *
 * Every component moved whole, so no hook moved between functions; the split's
 * hook-order proof against BASE is in its PR.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'

import * as facade from './SecurityPanel'
import * as trust from './SecurityPanelTrust'
import * as redaction from './SecurityPanelRedaction'
import * as layers from './SecurityPanelLayers'
import * as governance from './SecurityPanelGovernance'
import { PAGES_DIR, pageFamilyFiles } from '../../test/pageFamily'

const DIR = join(PAGES_DIR, 'settings')
const SRC = resolve(PAGES_DIR, '..')
const FILES = pageFamilyFiles('SecurityPanel', DIR)
const OWNERS = FILES.slice(1)
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
const stem = (p: string) => p.replace(/\.(tsx?|jsx?)$/, '').replace(/\/index$/, '')
function importsOf(file: string): string[] {
  return [...read(file).matchAll(SPEC_RE)].map((m) => m[1]).flatMap((spec) => {
    if (spec.startsWith('@/')) return [stem(join(SRC, spec.slice(2)))]
    if (spec.startsWith('.')) return [stem(resolve(dirname(file), spec))]
    return []
  })
}

describe('SecurityPanel owners', () => {
  it('are the files the panel is made of', () => {
    expect(OWNERS).toEqual([
      'SecurityPanelApprovals.tsx',
      'SecurityPanelDeniedCommands.tsx',
      'SecurityPanelGovernance.tsx',
      'SecurityPanelLayers.tsx',
      'SecurityPanelPosture.tsx',
      'SecurityPanelRedaction.tsx',
      'SecurityPanelTailnet.tsx',
      'SecurityPanelTrust.tsx',
    ])
  })

  it('include every sibling the facade imports under its name', () => {
    // A part named off the family pattern would drop out of `pageFamilyFiles`,
    // and every pin that reads the panel through it would stop seeing that part.
    const lower = 'SecurityPanel'.charAt(0).toLowerCase() + 'SecurityPanel'.slice(1)
    const named = new RegExp(`^(SecurityPanel|${lower}|useSecurityPanel)[A-Z.]`)
    const family = new Set(FILES.map((f) => stem(join(DIR, f))))
    const siblings = importsOf(join(DIR, 'SecurityPanel.tsx'))
      .filter((t) => dirname(t) === DIR && named.test(t.slice(DIR.length + 1)))
    expect(siblings.length).toBeGreaterThan(0)
    expect(siblings.filter((t) => !family.has(t))).toEqual([])
  })

  it('leave the facade exporting every name it exported as one file', () => {
    expect(Object.keys(facade).sort()).toEqual([
      'FEATURE_DESCRIPTION_KEY',
      'FEATURE_LABEL_KEY',
      'GOV_PLANE_TITLE_KEY',
      'RedactionAllowedHostsCard',
      'RedactionSection',
      'SCOPE_LABEL_KEY',
      'SECTION_GROUP_KEY',
      'SECTION_LABEL_KEY',
      'SecurityPanel',
      'humaniseScopeLeaf',
      'registryTrustFailureMessage',
      'trustFailureMessage',
    ])
  })

  it('re-export each moved name as the binding its owner defines', () => {
    expect(facade.trustFailureMessage).toBe(trust.trustFailureMessage)
    expect(facade.registryTrustFailureMessage).toBe(trust.registryTrustFailureMessage)
    expect(facade.RedactionAllowedHostsCard).toBe(redaction.RedactionAllowedHostsCard)
    expect(facade.RedactionSection).toBe(redaction.RedactionSection)
    expect(facade.FEATURE_LABEL_KEY).toBe(layers.FEATURE_LABEL_KEY)
    expect(facade.FEATURE_DESCRIPTION_KEY).toBe(layers.FEATURE_DESCRIPTION_KEY)
    expect(facade.SCOPE_LABEL_KEY).toBe(governance.SCOPE_LABEL_KEY)
    expect(facade.humaniseScopeLeaf).toBe(governance.humaniseScopeLeaf)
    expect(facade.GOV_PLANE_TITLE_KEY).toBe(governance.GOV_PLANE_TITLE_KEY)
  })

  it('never import the facade', () => {
    const facadePath = stem(join(DIR, 'SecurityPanel.tsx'))
    expect(OWNERS.filter((f) => importsOf(join(DIR, f)).includes(facadePath))).toEqual([])
  })

  it('are imported only by the facade and by each other', () => {
    const family = new Set(FILES.map((f) => stem(join(DIR, f))))
    const owners = new Set(OWNERS.map((f) => stem(join(DIR, f))))
    const offenders = walk(SRC)
      .filter((file) => !family.has(stem(file)))
      .flatMap((file) => importsOf(file).filter((t) => owners.has(t)).map((t) => `${relative(SRC, file)} -> ${relative(SRC, t)}`))
    expect(offenders).toEqual([])
  })

  it('reads imports the way the pins above rely on', () => {
    const probe = join(DIR, 'probe.tsx')
    const text = "import './Side'\nimport type { T } from '@/pages/Typed'\nexport { X } from './Re'\nconst L = () => import('./Lazy')\n"
    const found = [...text.matchAll(SPEC_RE)].map((m) => m[1])
    expect(found).toEqual(['./Side', '@/pages/Typed', './Re', './Lazy'])
    expect(stem(resolve(dirname(probe), './Side.tsx'))).toBe(join(DIR, 'Side'))
  })
})
