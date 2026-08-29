import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { closeFailureKind, CLOSE_FAILURE_COPY_KEY, CLOSE_FAILURE_TITLE_KEY } from '../utils/sessionCloseFailure'
import { i18nT } from '../i18n/t'

/** Resolve the copy exactly as the App shell does — through the SAME exported key map,
 *  so this pins the string a user actually sees rather than a second mapping. */
const noticeFor = (e?: unknown) => i18nT(CLOSE_FAILURE_COPY_KEY[closeFailureKind(e)])
const titleFor = (e?: unknown) => i18nT(CLOSE_FAILURE_TITLE_KEY[closeFailureKind(e)], { title: 'S' })
const wholeFor = (e?: unknown) => `${titleFor(e)}. ${noticeFor(e)}`
const err = (status: number) => Object.assign(new Error(`HTTP ${status}`), { status })

/** Both user close gestures must report a terminal failure, or the restored row
 *  reads as the flicker `closingSlots` removes — and the message has to be true of
 *  the branch it fires in, which is why classification takes the rejection. */
describe('session close failure notice', () => {
  it('resolves copy rather than leaking a catalog key', () => {
    const msg = noticeFor(err(503))
    expect(msg).not.toContain('useSessionActions')
    expect(msg.length).toBeGreaterThan(10)
  })

  /** A definitive refusal is the server's considered answer, so the session is
   *  provably still there, naming it is safe — and the close aborts roll every
   *  partial step back, so the copy must name the action that is now safe.
   *
   *  Asserted on the OUTCOME the title claims, not on the word "refused": the title
   *  named an internal component the product never surfaces, so it now leads with what
   *  the reader can act on. "Still open" is what distinguishes this from the unknown
   *  variant, and it is the claim that licenses the retry. */
  it('names a refused close, with the safe next step', () => {
    const msg = noticeFor(err(403))
    expect(titleFor(err(403))).toMatch(/still open/i)
    expect(titleFor(err(403))).not.toMatch(/gateway/i)
    expect(msg).toMatch(/clos(e|ing) it again/i)
    expect(msg).not.toMatch(/avoid closing/i)
  })

  /** THE UX FINDING — `SlotCloseError` hardcodes status 500 for EVERY close
   *  failure, so keying on the status made the refused branch unreachable and
   *  reported the hedged copy for failures the gateway had definitively refused
   *  and rolled back. The gateway's own `definitive` flag now discriminates — a
   *  wire contract, not a code list mirrored in the client. */
  it.each([
    ['forwarded on the rejection payload', { status: 500, message: 'failed', definitive: true }],
    ['unparsed on a raw ApiError body', { status: 500, body: '{"error":"failed to save history","code":"history_save_failed","definitive":true}' }],
  ])('reports a REFUSED close when the server says definitive, %s', (_label, e) => {
    expect(titleFor(e)).toMatch(/still open/i)
    expect(noticeFor(e)).toMatch(/clos(e|ing) it again/i)
    expect(wholeFor(e)).not.toMatch(/couldn.t confirm/i)
  })

  /** A 5xx carrying NO determinism flag never reached the close path, so its outcome
   *  is genuinely unknown. An abort CODE alone no longer implies a refusal. */
  it.each([
    ['a code but no flag', { status: 500, code: 'history_save_failed' }],
    ['an unrecognised code', { status: 500, code: 'something_else' }],
    ['a bare 500', { status: 500, message: 'boom' }],
    ['an unparseable body', { status: 500, body: '<html>502</html>' }],
    ['definitive false', { status: 500, definitive: false }],
  ])('still reports an unknown outcome for %s', (_label, e) => {
    expect(titleFor(e)).toMatch(/couldn.t confirm/i)
  })

  /** Every other failure leaves the outcome UNKNOWN — the DELETE may have completed
   *  and slot keys are reusable. The copy walks a narrow line between two findings
   *  that pull opposite ways, so both are pinned here at once. */
  it.each([
    ['no status', new Error('network down')],
    ['a timeout', err(408)],
    ['a rate limit', err(429)],
    ['a 5xx', err(503)],
  ])('reports an unknown outcome for %s', (_label, e) => {
    const msg = noticeFor(e)
    expect(titleFor(e)).toMatch(/couldn.t confirm/i)
    // THE UX FINDING — the prohibition must be the FIRST thing the guidance says, since a
    // skimmer at an anxious moment reads the opening clause and acts on it.
    expect(msg.split(/(?<=[.!?])\s+/)[0]).toMatch(/don[’']t close it again yet|before closing it again/i)
    // The lead must carry NO directive, or the split moved the wall of text without shortening it.
    expect(titleFor(e)).not.toMatch(/check its messages|close it again|list refreshes/i)
    expect(msg).not.toMatch(/still open/i)
    expect(msg).not.toMatch(/try again in a moment/i)
    expect(msg).not.toMatch(/unreachable|couldn't reach/i)
    // The instruction must key on something the user can SEE. "Settles" described an
    // internal quiescence they cannot observe, so it is forbidden.
    expect(msg).not.toMatch(/settles?\b/i)
    // It must not invite a BLIND second close: keys are reusable, so a returning row
    // can be a replacement carrying a live turn, and closing it would archive that.
    expect(msg).not.toMatch(/you can close it|close it then|close it from/i)
    // The reclose stays offered, but gated on identification — and the identification must
    // name a METHOD, not demand a certainty the user has no way to reach.
    expect(msg).toMatch(/before closing it again|don[’']t close it again yet/i)
    // Every sibling string calls it a session; "row" is the grid's word, not the user's.
    expect(msg).not.toMatch(/\brow\b/i)
    const sentences = msg.split(/(?<=[.!?])\s+/).filter(s => s.trim())
    expect(sentences.length, 'hands the user more than one instruction and one condition').toBeLessThanOrEqual(2)
    expect(msg.match(/\bif\b/gi) ?? [], 'carries more than one conditional').toHaveLength(1)
    expect(msg, 'does not say the session returns without the user acting').toMatch(/on its own|by itself/i)
    // Self-referential wording is what this replaced, so it must not come back.
    expect(msg).not.toMatch(/this (message|notice|toast)/i)
    expect(msg).not.toMatch(/clears once|once the list updates|will clear\b/i)
    // "the name" is the server's slot KEY, which the user never sees. Say what they can see.
    expect(msg).not.toMatch(/\bname\b/i)
  })

  /** The two outcomes must be distinguishable, or the parameter buys nothing. */
  it('says something different for refused than for unknown', () => {
    expect(noticeFor(err(403))).not.toBe(noticeFor(err(503)))
  })
})

/** OPUS FINDING — the failure used to reach the user through a native `alert()` in a
 *  `.ts` helper. The repo's `errors-use-error-notice` rule bans that surface but globs
 *  `src/**` + `*.tsx`, so a `.ts` util satisfied it by scope rather than by being
 *  right. These guard the shape the fix relies on, since the linter still cannot. */
describe('the close failure is rendered in-page, not alerted', () => {
  const read = (p: string) => readFileSync(new URL(p, import.meta.url), 'utf8')
  /** Strip comments before scanning: these files DISCUSS the banned `alert()` in their
   *  docstrings, and matching prose would fail the guard on the explanation of the fix. */
  const code = (p: string) => read(p).replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')

  it('ships no alert() anywhere on the close-failure path', () => {
    for (const p of ['../utils/sessionCloseFailure.ts', '../utils/closeOutcome.ts']) {
      expect(code(p)).not.toMatch(/\balert\s*\(/)
    }
    // Positive control TWICE: the symbol is in the scanned file, and the stripped text
    // is still substantial — so neither a moved module nor an over-eager strip passes.
    expect(code('../utils/sessionCloseFailure.ts')).toContain('closeFailureKind')
    expect(code('../utils/closeOutcome.ts').length).toBeGreaterThan(200)
  })

  it('routes the failure through the store so BOTH gestures share one surface', () => {
    const slice = read('../store/chatSlice.ts')
    // Tolerates line breaks inside the call, but still pins all THREE fields: the kind
    // comes from the shared classifier, and key and title are what the copy names.
    expect(slice).toMatch(
      /setSessionCloseFailure\(\{\s*kind: closeFailureKind\(e\),\s*key,\s*title\s*\}\)/,
    )
    expect(slice).not.toMatch(/alertSessionCloseFailed/)
  })

  it('captures the title at CLOSE time, not at render time', () => {
    // The tombstone hides the row, so a render-time lookup misses in exactly the
    // unknown-outcome case the copy has to name. Both gestures must read it up front.
    for (const f of ['../hooks/useSessionActions.ts', '../hooks/useKeyboardShortcuts.ts']) {
      expect(read(f)).toMatch(/closeSlotWithNotice\(\s*dispatch,[^)]*title/s)
    }
    // The shell interpolates it, so the string is not left with a bare placeholder.
    expect(read('../App.tsx')).toMatch(/CLOSE_FAILURE_TITLE_KEY\[[^\]]+\],\s*\{/)
  })

  /** AUTOSDE errors-use-error-notice blocks a touched ErrorNotice carrying NEITHER
   *  `askAgent` NOR a `No hand-off` comment naming a concrete draft. */
  it('carries the askAgent hand-off, which is the decision the rule requires', () => {
    const shell = read('../App.tsx')
    const i = shell.indexOf('testId="session-close-failed"')
    expect(i).toBeGreaterThan(-1)
    // Scan the notice's own JSX only, so a stray `askAgent` elsewhere in this large
    // file cannot satisfy this: the props sit above the testId that anchors it.
    const block = shell.slice(Math.max(0, i - 400), i)
    expect(block).toMatch(/\baskAgent\b/)
    expect(block).toContain('CLOSE_FAILURE_COPY_KEY')
  })
})

/** DESIGN FINDING — `fetchSlots.fulfilled` no longer means "applied": a reply issued
 *  before a membership move is discarded whole, so a caller that AWAITS the raw thunk
 *  and reads `payload` acts on a list the store rejected. `fetchSlotsIfApplied` is the
 *  safe entry point, and this makes that structural instead of conventional.
 *
 *  Scanning rather than un-exporting: the raw thunk has six legitimate fire-and-forget
 *  dispatch sites plus two reducer `addCase`s, so it cannot be un-exported, and renaming
 *  it would edit six files unrelated to this fix. Plain `dispatch(fetchSlots())` is safe
 *  and stays legal; only reading the reply's payload is not. */
describe('the unchecked slots read cannot be awaited by accident', () => {
  it('has no caller awaiting the raw thunk outside the checked helper', () => {
    const files = import.meta.glob('../**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true }) as Record<string, string>
    const offenders: string[] = []
    for (const [path, src] of Object.entries(files)) {
      if (path.includes('.test.') || path.endsWith('/dashboardSlice.ts')) continue
      // Awaiting it, or unwrapping it, both yield a payload the caller can act on.
      // chatSlice is exempt: it unwraps only to SEQUENCE, and never reads the payload.
      for (const m of src.matchAll(/await\s+dispatch\(\s*fetchSlots\(|dispatch\(\s*fetchSlots\(\)[^)]*\)\s*(?:as never\s*)?\)?\s*\.unwrap\(/g)) {
        if (path.endsWith('/chatSlice.ts')) continue
        offenders.push(`${path}: ${m[0]}`)
      }
    }
    expect(offenders).toEqual([])
    // Positive control: the scan really reached app source with the symbol in it.
    const seen = Object.entries(files).filter(([p, s]) => !p.includes('.test.') && s.includes('fetchSlots'))
    expect(seen.length).toBeGreaterThan(3)
  })
})

/** the close-failure toast and the agent-switch notice carried
 *  BYTE-IDENTICAL `fixed` positioning, so a close failing while an agent-switch notice was
 *  up (6s lifetime) put them exactly on top of each other and made one dismiss control
 *  unreachable. Asserted structurally rather than by pixels: there must be ONE positioned
 *  slot, both notices must live inside it, and it must stack rather than overlay. */
describe('the transient notices share one stacking slot', () => {
  // Read through the raw glob, the mechanism this file already uses for source scans:
  // a URL read of a sibling module is not file-scheme under the test transform.
  const readShell = () => {
    const files = import.meta.glob('../App.tsx', { query: '?raw', import: 'default', eager: true }) as Record<string, string>
    const entry = Object.values(files)[0]
    expect(entry, 'App.tsx was not reachable through the raw glob').toBeTruthy()
    return entry
  }

  it('positions exactly one container, so neither notice owns the slot alone', () => {
    const shell = readShell()
    const positioned = shell.match(/top-safe-offset-14/g) ?? []
    expect(positioned).toHaveLength(1)
    // Positive control: the class really is the one the notices are positioned by.
    expect(shell).toMatch(/data-testid="notice-stack"/)
  })

  it('stacks them in a column with a gap instead of overlaying', () => {
    const shell = readShell()
    const i = shell.indexOf('data-testid="notice-stack"')
    expect(i).toBeGreaterThan(-1)
    const container = shell.slice(Math.max(0, i - 300), i)
    expect(container).toMatch(/flex flex-col gap-/)
  })

  it('renders BOTH notices inside that one container', () => {
    const shell = readShell()
    const open = shell.indexOf('data-testid="notice-stack"')
    const close = shell.indexOf('testId="session-close-failed"')
    expect(open).toBeGreaterThan(-1)
    // The close notice sits after the container opens, and the agent-switch notice with it.
    expect(close).toBeGreaterThan(open)
    const block = shell.slice(open, close)
    expect(block).toMatch(/agentSwitchNotice/)
    // Neither child may re-declare its own fixed positioning.
    expect(block).not.toMatch(/\bfixed z-\[/)
  })
})
