// UX's two Watch items at 94d0d6b0a6: a storage-blocked browser got an unappealable refusal,
// and the chip's paint asked a narrower question than its click.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { __resetSlotDirtyForTests, claimsAreReadable } from '../utils/slotDirtyBeacon'
import { slotUnsentWorkSource } from '../utils/slotComposerRegistry'

const DISPATCH = readFileSync(join(__dirname, '..', 'hooks', 'useOptionActionDispatch.ts'), 'utf-8')
const BAR = readFileSync(join(__dirname, '..', 'components', 'FollowUpBar.tsx'), 'utf-8')
const PAGE = readFileSync(join(__dirname, '..', 'pages', 'ChatPage.tsx'), 'utf-8')
const PANE = readFileSync(join(__dirname, '..', 'components', 'ChatPane.tsx'), 'utf-8')
const REGISTRY = readFileSync(join(__dirname, '..', 'utils', 'slotComposerRegistry.ts'), 'utf-8')
const SESSION = readFileSync(join(__dirname, '..', 'hooks', 'useSessionActions.ts'), 'utf-8')
const BEACON = readFileSync(join(__dirname, '..', 'utils', 'slotDirtyBeacon.ts'), 'utf-8')
const SUCCESSION = readFileSync(join(__dirname, '..', 'utils', 'slotSuccession.ts'), 'utf-8')
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

  it('does not NAME another window it could not read', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError')
    })
    // Still non-null, so the fail-closed confirm is untouched — only the wording hedges.
    expect(slotUnsentWorkSource('slot-unreadable')).toBe('unverifiable')
    expect(REGISTRY).toContain("if (!claimsAreReadable()) return 'unverifiable'")
    // Its own confirm copy: the generic line still says the work WILL be lost.
    expect(SESSION).toContain('close_unsent_confirm_unverifiable')
  })
})

describe('the chip paints on the same question the click asks', () => {
  it('gives the slot-sourced block its OWN line, naming the side panel', () => {
    // "send or clear your draft" beside a visibly empty composer sent the reader
    // hunting in the wrong place, so the two cases no longer share one reason.
    expect(BAR).toContain("actionBlockedBySlot ? 'unsentWorkElsewhere' : null")
    expect(BAR).toContain('action_unavailable_short_unsent_draft_elsewhere')
    expect(BAR).toContain('action_unavailable_while_unsent_draft_elsewhere')
  })

  it('records no row for a refusal the PAINT invited', () => {
    // `crossWindow` is event-sampled, so a window that mounted after another published its
    // claim paints a live chip — a row there records this component's own paint being wrong.
    expect(DISPATCH).toContain('const disclosed = slotHasUnsentWorkHere(slot) || crossWindowRef.current')
    expect(DISPATCH).toContain('if (disclosed) notify(i18nT(unsentNoticeKey(unsentAt)))')
    // And it must LEARN the answer, so the chip greys with its reason from that click on.
    expect(DISPATCH).toContain("if (unsentAt !== 'here') setCrossWindow(unsentAt)")
  })

  it('hedges a claim that stopped refreshing instead of naming a live window', () => {
    // A crashed window leaves a u:1 claim alive under the 12h bound but re-stamping
    // nothing, so "unsent work in another window" sends the user hunting for a ghost.
    expect(BEACON).toContain('export function anyWindowClaimRefreshStale')
    expect(REGISTRY).toContain("anyWindowClaimRefreshStale(slot) ? 'unverifiable' : 'other-window'")
  })

  it('does not read a LAPSED unrecoverable claim as a clean slot', () => {
    // An OS suspend freezes a live owner's timers, so a window still holding an in-flight
    // upload can miss every re-stamp; expiring to null deleted that work with no prompt.
    expect(BEACON).toContain('export function anyWindowClaimLapsed')
    expect(REGISTRY).toContain("if (anyWindowClaimLapsed(slot)) return 'unverifiable'")
  })

  it('refuses a succession walk that outruns its cap', () => {
    // Returning the intermediate lands the work in a slot already deleted.
    expect(SUCCESSION).toContain('return successors.get(current) ? null : current')
    expect(SUCCESSION).not.toContain('    break\n  }\n  return current')
  })

  it('greys the chip for another window WITHOUT reading storage on render or on a timer', () => {
    expect(DISPATCH).toContain('slotHasUnsentWorkHere(slot) || crossWindow')
    expect(DISPATCH).toContain("window.addEventListener('focus', sample)")
    // A mount read or an interval would breach the no-storage-when-idle contract.
    expect(DISPATCH).not.toContain('setInterval(sample')
  })

  it('keeps the two reasons distinct rather than re-folding them', () => {
    expect(BAR).not.toContain("(composerHasUnsentWork || actionBlockedBySlot) ? 'unsentWork' : null")
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

  it('applies the same readability condition to the LATE re-check', () => {
    expect(DISPATCH).toContain("lateUnsentAt === 'here' || claimsAreReadable()")
    // Bare, an accepted confirm aborted on unreadable storage AFTER writing the note.
    expect(DISPATCH).not.toContain('if (lateUnsentAt !== null) {')
  })
})

describe('the option-actions segment is taught unconditionally', () => {
  const CONTEXT = readFileSync(
    join(__dirname, '..', '..', '..', 'src', 'kiro_crew', 'context.py'), 'utf-8')

  it('keeps the neutralization prefix check matching every surviving variant', () => {
    const at = CONTEXT.indexOf('_rules_prefix = next(')
    const block = CONTEXT.slice(at, at + 400)
    expect(block).toContain('_CRITICAL_RULES,')
    expect(block).toContain('_CRITICAL_RULES_CHANNEL,')
  })

  it('carries no kill-switch: no gated variant, no env read, no selector', () => {
    expect(CONTEXT).not.toContain('_CRITICAL_RULES_NO_OPTION_ACTIONS')
    expect(CONTEXT).not.toContain('KIROCREW_TEACH_OPTION_ACTIONS')
    expect(CONTEXT).not.toContain('_option_actions_taught')
    // Positive control: the reader really does see this file's rule blocks.
    expect(CONTEXT).toContain('_CRITICAL_RULES_CHANNEL = (')
  })

  it('reads a surface that exists, not an unmodelled config key', () => {
    expect(CONTEXT).not.toContain('teach_option_actions"')
    expect(CONTEXT).not.toContain('from kiro_crew.config import load_config')
  })
})
