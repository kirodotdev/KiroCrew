/**
 * ChatPanel.tsx and the owners beside it.
 *
 * The facade stays the chat panel's one import path (ChatApp, the capture
 * harnesses and the specs import it by that id, and MochiChatAppCoverage mocks
 * it by that id) and re-exports each part from its owner. These pins keep that
 * shape honest:
 *
 *  - the facade still exports every name it exported as one file, and a
 *    re-exported name is the owner's own binding rather than a copy;
 *  - owners never import the facade, and nothing outside the panel (bar specs)
 *    imports an owner, whether it lives under `src/` or in the capture
 *    harnesses beside it.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'

import * as facade from './ChatPanel'
import * as bubble from './ChatPanelBubble'
import * as markdown from './ChatPanelMarkdown'
import * as pinned from './ChatPanelPinnedFiles'
import { pageFamilyFiles } from '../../../../test/pageFamily'

const DIR = __dirname
const SRC = resolve(DIR, '..', '..', '..', '..')
const CAPTURE = resolve(SRC, '..', 'capture')
const FILES = pageFamilyFiles('ChatPanel', DIR)
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

describe('ChatPanel owners', () => {
  it('are the files the panel is made of', () => {
    expect(OWNERS).toEqual([
      'ChatPanelBubble.tsx',
      'ChatPanelMarkdown.tsx',
      'ChatPanelPinnedFiles.tsx',
    ])
  })

  it('include every sibling the facade imports under its name', () => {
    // A part named off the family pattern would drop out of `pageFamilyFiles`,
    // and every pin that reads the panel through it would stop seeing that part.
    const named = /^(ChatPanel|chatPanel|useChatPanel)[A-Z.]/
    const family = new Set(FILES.map((f) => stem(join(DIR, f))))
    const siblings = importsOf(join(DIR, 'ChatPanel.tsx'))
      .filter((t) => dirname(t) === DIR && named.test(t.slice(DIR.length + 1)))
    expect(siblings.length).toBeGreaterThan(0)
    expect(siblings.filter((t) => !family.has(t))).toEqual([])
  })

  it('leave the facade exporting every name it exported as one file', () => {
    expect(Object.keys(facade).sort()).toEqual([
      'Bubble',
      'ChatPanel',
      'PinnedSidePanel',
      'externalApprovalApproved',
      'fixStreamingFences',
      'parseApproval',
    ])
  })

  it('re-export each part as its owner binding, not a copy', () => {
    expect(facade.PinnedSidePanel).toBe(pinned.PinnedSidePanel)
    expect(facade.fixStreamingFences).toBe(markdown.fixStreamingFences)
    expect(facade.Bubble).toBe(bubble.Bubble)
    expect(facade.parseApproval).toBe(bubble.parseApproval)
    expect(facade.externalApprovalApproved).toBe(bubble.externalApprovalApproved)
  })

  it('never import the facade', () => {
    const facadePath = stem(join(DIR, 'ChatPanel.tsx'))
    expect(OWNERS.filter((f) => importsOf(join(DIR, f)).includes(facadePath))).toEqual([])
  })

  it('are imported only by the facade and by each other', () => {
    const family = new Set(FILES.map((f) => stem(join(DIR, f))))
    const owners = new Set(OWNERS.map((f) => stem(join(DIR, f))))
    const offenders = [...walk(SRC), ...walk(CAPTURE)]
      .filter((file) => !family.has(stem(file)))
      .flatMap((file) => importsOf(file).filter((t) => owners.has(t)).map((t) => `${relative(SRC, file)} -> ${relative(SRC, t)}`))
    expect(offenders).toEqual([])
  })

  it('reads imports the way the pins above rely on', () => {
    const probe = join(DIR, 'probe.tsx')
    const text = "import './Side'\nimport type { T } from '@/apps/Typed'\nexport { X } from './Re'\nconst L = () => import('./Lazy')\n"
    const found = [...text.matchAll(SPEC_RE)].map((m) => m[1])
    expect(found).toEqual(['./Side', '@/apps/Typed', './Re', './Lazy'])
    expect(stem(resolve(dirname(probe), './Side.tsx'))).toBe(join(DIR, 'Side'))
  })
})
