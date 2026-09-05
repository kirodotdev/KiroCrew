// UX's two Watch items at 94d0d6b0a6: a storage-blocked browser got an unappealable refusal,
// and the chip's paint asked a narrower question than its click.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { __resetSlotDirtyForTests, claimsAreReadable } from '../utils/slotDirtyBeacon'

const DISPATCH = readFileSync(join(__dirname, '..', 'hooks', 'useOptionActionDispatch.ts'), 'utf-8')
const BAR = readFileSync(join(__dirname, '..', 'components', 'FollowUpBar.tsx'), 'utf-8')
const PAGE = readFileSync(join(__dirname, '..', 'pages', 'ChatPage.tsx'), 'utf-8')
const PANE = readFileSync(join(__dirname, '..', 'components', 'ChatPane.tsx'), 'utf-8')
const REGISTRY = readFileSync(join(__dirname, '..', 'utils', 'slotComposerRegistry.ts'), 'utf-8')
const count = (src: string, needle: string) => src.split(needle).length - 1

describe('a storage-blocked browser gets a confirm, not a dead end', () => {
  // The probe caches for CLAIM_PROBE_TTL_MS, so a prior call would answer for this one.
  beforeEach(() => __resetSlotDirtyForTests())
  afterEach(() => vi.restoreAllMocks())

  it('reports the claim store readable when it works', () => {
    expect(claimsAreReadable()).toBe(true)
  })

  it('reports it UNREADABLE when the store refuses writes', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError')
    })
    expect(claimsAreReadable()).toBe(false)
  })

  it('refuses only on a knowable source, so the fail-closed read falls through', () => {
    expect(DISPATCH).toContain("if (unsentAt !== null && (unsentAt === 'here' || claimsAreReadable()))")
  })

  it("exempts 'here', which the in-process registry answers without storage", () => {
    // Own-composer work is genuinely knowable, so refusing on it is still correct.
    expect(DISPATCH).toContain("unsentAt === 'here'")
  })
})

describe('the chip paints on the same question the click asks', () => {
  it('folds the slot-wide answer into the existing block reason', () => {
    expect(BAR).toContain("(composerHasUnsentWork || actionBlockedBySlot) ? 'unsentWork' : null")
  })

  it('adds no new catalogue key for it', () => {
    // The consequence is identical, so the shipped copy already covers it.
    expect(BAR).not.toContain("'slotWork'")
    expect(BAR).not.toContain('action_unavailable_short_slot')
  })

  it('is resolved by BOTH hosts each render, since the registry is a plain read', () => {
    expect(count(PAGE, 'actionBlockedBySlot={slotBlocksAction()}')).toBe(1)
    expect(count(PANE, 'actionBlockedBySlot={slotBlocksAction()}')).toBe(1)
  })

  it('paints from the registry alone, so a render touches no storage', () => {
    const helper = DISPATCH.slice(DISPATCH.indexOf('const slotBlocksAction'))
    expect(helper).toContain('return slotHasUnsentWorkHere(slot)')
    expect(helper).not.toContain('claimsAreReadable()')
    expect(REGISTRY).toContain('export function slotHasUnsentWorkHere')
  })

  it('still applies the FULL slot-wide test where the click is refused', () => {
    expect(DISPATCH).toContain("unsentAt === 'here' || claimsAreReadable()")
  })

  it('is published from the hook so a host cannot invent its own answer', () => {
    expect(DISPATCH).toContain('return { dispatchFollowUpAction, slotBlocksAction }')
  })
})

describe('the prompt-teaching segment is switchable in the field', () => {
  const CONTEXT = readFileSync(
    join(__dirname, '..', '..', '..', 'src', 'kiro_crew', 'context.py'), 'utf-8')

  it('ships a THIRD fixed constant rather than templating the trusted block', () => {
    expect(CONTEXT).toContain('_CRITICAL_RULES_NO_OPTION_ACTIONS = (')
    expect(CONTEXT).toContain('_CRITICAL_RULES_NO_OPTION_ACTIONS,')
  })

  it('keeps the neutralization prefix check matching every variant', () => {
    const at = CONTEXT.indexOf('_rules_prefix = next(')
    const block = CONTEXT.slice(at, at + 400)
    expect(block).toContain('_CRITICAL_RULES,')
    expect(block).toContain('_CRITICAL_RULES_NO_OPTION_ACTIONS,')
    expect(block).toContain('_CRITICAL_RULES_CHANNEL,')
  })

  it('defaults to teaching, and only the TEACHING is gated', () => {
    expect(CONTEXT).toContain('KIROCREW_TEACH_OPTION_ACTIONS')
    expect(CONTEXT).toContain('"1").lower() not in ("0", "false", "no")')
    // The parse/strip machinery must stay always-on: a model can emit the marker regardless.
    expect(CONTEXT).not.toContain('if not _option_actions_taught(): return ""')
  })

  it('reads a surface that exists, not an unmodelled config key', () => {
    expect(CONTEXT).not.toContain('teach_option_actions"')
    expect(CONTEXT).not.toContain('from kiro_crew.config import load_config')
  })
})
