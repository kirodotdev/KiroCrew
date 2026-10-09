/**
 * ChatPage's facade and its composed owners under pages/chat/page/.
 *
 * ChatPage.tsx stays the page's one public surface (App, PopoutFrame, the app
 * SDK's ChatPanel, Papyrus' co-author panel and the artifact companion chat
 * import it) and calls each owner hook where its inline code used to run.
 * These pins keep that shape honest:
 *
 *  - the facade still exports what its importers use, and PREFILL_STORAGE_KEY
 *    is the SAME binding navIntent owns, not a copy;
 *  - owners depend on the page's collaborators, never back on the facade, and
 *    only the facade (and tests) import them, so nothing else grows a second
 *    route into the page's internals;
 *  - no owner switches slots itself: the `switchSlot` call sites stay in the
 *    files the switch-slot classification ratchet counts;
 *  - the owner hooks are called in the order the inline code ran. React runs
 *    a component's effects in declaration order, and several of these are
 *    ordered against each other (the draft commit / slot restore / staged
 *    persistence / composer-key advance chain is the sharpest case), so a
 *    reshuffle is a behaviour change even when every test of each owner still
 *    passes. Inline page effects that an owner is ordered against are pinned
 *    in the same sequence by an anchor text unique to them.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'

import * as facade from '../pages/ChatPage'
import * as navIntent from '../utils/navIntent'

const SRC = resolve(__dirname, '..')
const OWNER_DIR = join(SRC, 'pages/chat/page')
const PAGE = join(SRC, 'pages/ChatPage.tsx')

const owners = readdirSync(OWNER_DIR).filter(f => /\.(ts|tsx)$/.test(f) && !/\.test\./.test(f))
const read = (p: string) => readFileSync(p, 'utf8')

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(ts|tsx)$/.test(name) && !/\.test\.|\.spec\./.test(name) && !p.includes(`${join(SRC, 'test')}`)) out.push(p)
  }
  return out
}

describe('ChatPage facade', () => {
  it('keeps the exports its importers use', () => {
    expect(typeof facade.default).toBe('function')
    expect(typeof facade.shouldAutoFillOlder).toBe('function')
    expect(facade.PREFILL_STORAGE_KEY).toBe(navIntent.PREFILL_STORAGE_KEY)
  })
})

describe('pages/chat/page owners', () => {
  it('exist, and none imports the ChatPage facade back', () => {
    expect(owners.length).toBeGreaterThan(0)
    for (const f of owners) {
      const text = read(join(OWNER_DIR, f))
      expect(text, f).not.toMatch(/from ['"](\.\.\/)+ChatPage['"]/)
      expect(text, f).not.toMatch(/from ['"][^'"]*pages\/ChatPage['"]/)
    }
  })

  it('are imported only by the facade and by each other', () => {
    // Resolve every relative specifier (static and dynamic imports alike), so
    // a sibling's `./page/x` is caught as surely as the facade's `./chat/page/x`.
    const importsOwner = (file: string) => [...read(file).matchAll(/(?:from|import\()\s*['"](\.[^'"]+)['"]/g)]
      .some(m => resolve(dirname(file), m[1]).startsWith(OWNER_DIR + '/'))
    const importers = walk(SRC).filter(p => !p.startsWith(OWNER_DIR + '/') && importsOwner(p))
    expect(importers.map(p => relative(SRC, p))).toEqual(['pages/ChatPage.tsx'])
  })

  it('never switch slots themselves', () => {
    for (const f of owners) expect(read(join(OWNER_DIR, f)), f).not.toMatch(/switchSlot\(/)
  })

  it('are called by the page in the order their inline code ran', () => {
    const page = read(PAGE)
    // Inline page effects that are ordered against an owner, located by text
    // unique to them. The signed-token intake strips the WHOLE query string, so
    // the `?prefill=` read in usePendingInputIntake has to run before it.
    // (No `<ComposerDraftSync` anchor: a child's effects run before the page's.)
    const INLINE = [
      { name: '(inline) signed-token intake effect', re: /tokenConsumingRef is initialized true when a token/g },
    ]
    for (const a of INLINE) expect([...page.matchAll(a.re)], a.name).toHaveLength(1)
    const ownerHooks = new Set(
      owners.flatMap(f => [...read(join(OWNER_DIR, f)).matchAll(/^export function (use[A-Z][A-Za-z]+)/gm)].map(m => m[1])),
    )
    // useFileMentionTokens is composed inside useComposerStaging, not by the page.
    ownerHooks.delete('useFileMentionTokens')
    const sequence = [
      ...[...page.matchAll(/\b(use[A-Z][A-Za-z]+)\(/g)].filter(m => ownerHooks.has(m[1])).map(m => ({ at: m.index, name: m[1] })),
      ...INLINE.map(a => ({ at: [...page.matchAll(a.re)][0].index, name: a.name })),
    ].sort((x, y) => x.at - y.at).map(e => e.name)
    expect(sequence).toEqual([
      'useComposerDraftStores',
      'useWelcomeState',
      'useSessionRosters',
      'useSessionAutomation',
      'useErrorHandoffIntake',
      'usePendingInputIntake',
      '(inline) signed-token intake effect',
      'useComposerStaging',
      'useComposerDraftLifecycle',
      'useStagedFolderRefs',
      'useStagedDraftPersistence',
      'useAppLaunchIntake',
      'useFollowUpChips',
      'useAutoSendIntake',
      'useComposerSessionControls',
      'useFileMentionActions',
      'useChatEventBridges',
      'useColdFileTabHydration',
      'useComposerChips',
      'useTurnRecovery',
      'useTranscriptRows',
      'useStableRowKeys',
      'useBusyTurnControls',
      'useTranscriptJumps',
      'useComposerAboveBand',
      'useSessionControlChips',
    ])
  })
})
