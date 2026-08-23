/**
 * Source-level ratchet: no Enter-submit handler may hand-roll the IME guard.
 *
 * The defect this pins is not a bug in one component, it is a SHAPE that spread by
 * copy: `if (e.key === 'Enter' && !ime.isComposing(e)) { e.preventDefault(); … }`
 * puts the consumption inside the guarded condition, so a declined Enter reaches
 * the element and the browser inserts a line break into the text the user is about
 * to send. `ime.claimEnter(e)` owns both halves, so a call site cannot get one
 * right and the other wrong.
 *
 * Two spellings count, because both were found in the tree and they fail
 * differently. Consulting the HOOK (`ime.isComposing(e)`) carries the tracked
 * latch, so it produces the newline. Consulting only the NATIVE flag
 * (`e.nativeEvent.isComposing`) has no latch at all, so instead it re-opens the
 * half-sent-message defect the latch exists to prevent: on WebKit the keydown that
 * commits a candidate reports that flag as false.
 *
 * A behavioural test cannot catch a NEW copy of the shape in a component nobody
 * wrote a test for, which is why this reads the tree instead.
 *
 * There is deliberately no allowlist. An earlier revision exempted whole FILES,
 * which was unsound: one file can hold both an exempt single-line input and a
 * multiline textarea, so the exemption covered a surface it was never meant for.
 * Every Enter-submit branch in the tree now goes through the hook, so the rule can
 * be absolute — which is a cheaper thing to keep true than a list of reasons.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

const SRC = join(__dirname, '..')

/** An Enter branch that reads a composition signal directly instead of claiming the key. */
const HAND_ROLLED = /key === 'Enter'[^\n]*(?:ime\.isComposing\(|nativeEvent\.isComposing)/

/**
 * The same defect split across two lines: a guard that DECLINES the key without
 * consuming it, then consumes manually on the accepted path. `claimEnter` owns
 * both halves precisely so the two cannot disagree — a copy of this spelling
 * pasted onto a textarea re-opens the newline defect the one-line rule pins.
 * A decline WITHOUT a following manual `preventDefault` stays legal ONLY on
 * single-line inputs: there nothing is inserted and consuming would suppress a
 * wanted implicit form submit. On a textarea the browser answers the unconsumed
 * key with a literal newline, so a separate rule below rejects the decline
 * by element tag.
 */
const GUARD_RETURN = /\bisComposing\([^)]*\)\)?\s*return\b/
const MANUAL_CONSUME = /^\s*(?:if \([^)]*\) *\{? *)?\w+\.preventDefault\(\)/

function sourceFiles(): string[] {
  return readdirSync(SRC, { recursive: true, encoding: 'utf8' })
    .map(p => p.split('\\').join('/'))
    .filter(p => /\.tsx?$/.test(p))
    .filter(p => !p.startsWith('test/') && !p.includes('__tests__'))
}

/*
 * The scan bodies live in named functions rather than inline in each `it` so
 * the fixture suite at the bottom can exercise the exact predicate the tree
 * scan runs — a regex tightened against the live tree alone can silently stop
 * matching the defect shape it was written for, and no offender would tell us.
 */

/** True when one of the next two CODE lines after `i` manually consumes the key. */
function consumesWithinWindow(lines: string[], i: number): boolean {
  let seen = 0
  for (let j = i + 1; j < lines.length && seen < 2; j++) {
    const l = lines[j].trim()
    if (l === '' || l.startsWith('//') || l.startsWith('/*') || l.startsWith('*')) continue
    seen++
    if (MANUAL_CONSUME.test(lines[j])) return true
  }
  return false
}

/** Decline-then-consume pairs: the two-line re-implementation of claimEnter. */
function scanDeclineConsumePairs(lines: string[]): number[] {
  const hits: number[] = []
  lines.forEach((line, i) => {
    if (GUARD_RETURN.test(line) && consumesWithinWindow(lines, i)) hits.push(i + 1)
  })
  return hits
}

/**
 * Unconsumed declines on a textarea. The single-line-input exemption above
 * does not transfer: a textarea answers the unclaimed Enter with a literal
 * newline into the draft — the exact corruption the guard exists to prevent —
 * and an `Enter→blur` commit does not change that. The element is found by the
 * same backtrack the shadowing rule uses; a spread's own generic annotation
 * (`bindComposition<HTMLTextAreaElement>`) also names the tag, so the shape
 * that motivated this rule is caught either way. Unrecognized formatting fails
 * open, consistent with the rest of this file.
 */
function scanUnconsumedTextareaDeclines(lines: string[]): number[] {
  const hits: number[] = []
  lines.forEach((line, i) => {
    if (!GUARD_RETURN.test(line) || consumesWithinWindow(lines, i)) return
    let start = i
    while (start > 0 && !/<[A-Za-z]/.test(lines[start])) start--
    if (/<\w*[Tt]ext[Aa]rea/.test(lines[start])) hits.push(i + 1)
  })
  return hits
}

/**
 * Props each binding carries; a standalone copy of one beside the spread
 * shadows that half of the guard by JSX last-one-wins. `bindEnter` also owns
 * the whole keydown, so a standalone `onKeyDown` beside it replaces the Enter
 * guard itself. `bindComposition` deliberately does NOT list `onKeyDown`: the
 * claim-in-own-handler pattern (rule 2) spreads the composition binding next
 * to a site-owned `onKeyDown` by design.
 */
const SHADOWABLE: Record<string, RegExp> = {
  bindComposition: /^\s*(?:onBlur|onFocus|onCompositionStart|onCompositionEnd)=/,
  bindEnter: /^\s*(?:onBlur|onFocus|onCompositionStart|onCompositionEnd|onKeyDown)=/,
}

/** The lines of the JSX element enclosing line `i` (this tree's formatting). */
function elementSpan(lines: string[], i: number): string[] {
  let start = i
  while (start > 0 && !/<[A-Za-z]/.test(lines[start])) start--
  let end = i
  while (end < lines.length - 1 && !lines[end].includes('/>')) end++
  return lines.slice(start, end + 1)
}

/** Binding spreads with a standalone copy of a prop the binding already carries. */
function scanShadowedBindings(lines: string[]): number[] {
  const hits: number[] = []
  lines.forEach((line, i) => {
    const m = /\.\.\.\w+\.(bindComposition|bindEnter)/.exec(line)
    if (!m) return
    if (elementSpan(lines, i).some(l => SHADOWABLE[m[1]].test(l))) hits.push(i + 1)
  })
  return hits
}

describe('IME Enter claim ratchet', () => {
  it('routes every Enter-submit branch through the guard', () => {
    const offenders: string[] = []
    for (const rel of sourceFiles()) {
      readFileSync(join(SRC, rel), 'utf8').split('\n').forEach((line, i) => {
        if (HAND_ROLLED.test(line)) offenders.push(`${rel}:${i + 1}`)
      })
    }
    // An entry here is a call site that decides not to submit and then hands the key
    // to the browser anyway. Route it through `ime.claimEnter(e)`.
    expect(offenders).toEqual([])
  })

  it('never re-implements claimEnter as a decline-then-consume pair', () => {
    // The two-line spelling of the same defect the one-line rule pins: decline
    // via `isComposing(...) return`, then `preventDefault()` on the accepted
    // path. Comment-only lines between the two do not launder the pair. The
    // scan window is deliberately short (the next two code lines): a guard
    // whose consumption sits further away is a different structure, and this
    // check fails open rather than mis-flagging it.
    const offenders: string[] = []
    for (const rel of sourceFiles()) {
      if (rel === 'hooks/useImeGuard.ts') continue
      const lines = readFileSync(join(SRC, rel), 'utf8').split('\n')
      for (const n of scanDeclineConsumePairs(lines)) offenders.push(`${rel}:${n}`)
    }
    // An entry here declines the Enter without consuming it and then consumes
    // manually when accepted. Replace the pair with `ime.claimEnter(e)`.
    expect(offenders).toEqual([])
  })

  it('never leaves a declined Enter unconsumed on a textarea', () => {
    // BlockEditor shipped exactly this in the sweep that added the rule above:
    // `if (ime.isComposing(e)) return` before an Enter→blur commit on a
    // textarea. Inside the post-composition latch window the decline hands the
    // key back to the browser, which inserts a literal newline into the note
    // draft — neither committed nor intact. Textarea declines must claim.
    const offenders: string[] = []
    for (const rel of sourceFiles()) {
      if (rel === 'hooks/useImeGuard.ts') continue
      const lines = readFileSync(join(SRC, rel), 'utf8').split('\n')
      for (const n of scanUnconsumedTextareaDeclines(lines)) offenders.push(`${rel}:${n}`)
    }
    // An entry here declines an Enter on a textarea and lets the browser act
    // on it. Replace the decline with `if (!ime.claimEnter(e)) return`.
    expect(offenders).toEqual([])
  })

  it('never lets a standalone copy of a bound prop sit on an element that spreads a binding', () => {
    // JSX resolves duplicate props by last-one-wins, so a standalone `onBlur` after the
    // spread drops the latch reset and a standalone one before it drops the caller's own
    // handler. Both are silent. This is not hypothetical: the pet composer shipped the
    // first form in the very commit that made the binding mandatory, and only a blind
    // reviewer caught it — the hook can make the correct spelling AVAILABLE but only a
    // reader of the whole element can see the shadowing, so the check belongs here.
    // The guarded set is per binding (see SHADOWABLE): `onFocus` joined when the
    // stale-latch reset moved into the binding, the composition handlers because a
    // standalone copy severs the tracking itself, and `onKeyDown` beside `bindEnter`
    // because there the binding carries the Enter guard in that very prop.
    //
    // Elements are bracketed by this tree's formatting (one attribute per line, the tag
    // closing on its own line). A file that formats differently is not flagged rather
    // than mis-flagged: the check fails open, which is the right direction for a rule
    // whose job is to catch a copied shape.
    const offenders: string[] = []
    for (const rel of sourceFiles()) {
      const lines = readFileSync(join(SRC, rel), 'utf8').split('\n')
      for (const n of scanShadowedBindings(lines)) offenders.push(`${rel}:${n}`)
    }
    // Pass the handler INTO bindComposition/bindEnter({ onFocus, onBlur, … }) so both
    // run — or, for an `onKeyDown` beside bindEnter, keep the site's own handler and
    // claim the Enter branch instead of spreading bindEnter.
    expect(offenders).toEqual([])
  })

  it('keeps every React handler off the raw composition flag', () => {
    // The Enter check above only sees the two on one line. `SearchableSelect` guarded
    // Enter with a bare `e.nativeEvent.isComposing` early-return one line ABOVE the
    // dispatch, and survived — the latch-less spelling, on a picker where the committing
    // keydown WebKit reports as non-composing would accept the first option and discard
    // the text the user just composed.
    //
    // `nativeEvent.isComposing` only exists on a React synthetic event, so the prefix is
    // what separates in-scope from out: a handler receiving a NATIVE DOM event reads
    // `e.isComposing` and cannot use this hook, which takes a synthetic one
    // (`TerminalCompletion`'s xterm key handler carries its own grace window for that
    // reason; two Escape handlers on `document` are likewise native). That is a
    // structural distinction, not a list of pardoned files — nothing here needs an
    // exemption to maintain.
    const offenders: string[] = []
    for (const rel of sourceFiles()) {
      if (rel === 'hooks/useImeGuard.ts') continue
      readFileSync(join(SRC, rel), 'utf8').split('\n').forEach((line, i) => {
        if (/nativeEvent\.isComposing/.test(line)) offenders.push(`${rel}:${i + 1}`)
      })
    }
    // Read the flag through `ime.isComposing(e)` instead: the hook layers the tracked
    // latch over it, which is the half of the guard a raw read cannot have.
    expect(offenders).toEqual([])
  })

  it('exposes no composition binding without the latch recovery', () => {
    // A binding that tracks composition but does not reset on blur lets a surface
    // strand itself: an abandoned composition latches the guard, and since claimEnter
    // consumes what it declines, the surface then silently stops sending. The hook is
    // the only place that can make that unreachable, so it must not hand out a
    // recovery-less binding for a caller to pick by mistake.
    const hook = readFileSync(join(SRC, 'hooks/useImeGuard.ts'), 'utf8')
    const returned = /return \{([^}]*)\}/.exec(hook)?.[1] ?? ''
    expect(returned).toContain('bindComposition')
    expect(returned.split(',').map(s => s.trim())).not.toContain('composition')
    // Every binding the hook returns carries onBlur.
    expect(hook).toMatch(/bindComposition[\s\S]{0,600}?onBlur/)
  })
})

describe('ratchet rule fixtures', () => {
  // The tree scans above can only prove "no offender today". These fixtures
  // prove the predicates still MATCH the defect shapes they were written for —
  // without them, a regex edit could silently stop matching and every scan
  // would keep passing vacuously.
  const jsx = (s: string) => s.split('\n')

  it('flags a standalone onKeyDown beside bindEnter but not beside bindComposition', () => {
    expect(scanShadowedBindings(jsx(`
      <input
        {...ime.bindEnter({ onEnter: send })}
        onKeyDown={e => step(e)}
      />`))).toHaveLength(1)
    // Rule 2's sanctioned shape: composition binding NEXT TO a site-owned
    // keydown that claims its own Enter branch.
    expect(scanShadowedBindings(jsx(`
      <input
        onKeyDown={e => { if (e.key === 'Enter' && !ime.claimEnter(e)) return }}
        {...ime.bindComposition()}
      />`))).toHaveLength(0)
  })

  it('flags standalone composition handlers beside either binding', () => {
    expect(scanShadowedBindings(jsx(`
      <input
        {...ime.bindComposition()}
        onCompositionStart={track}
      />`))).toHaveLength(1)
    expect(scanShadowedBindings(jsx(`
      <input
        {...ime.bindEnter({ onEnter: send })}
        onCompositionEnd={track}
      />`))).toHaveLength(1)
  })

  it('still flags the original onBlur/onFocus shadowing', () => {
    expect(scanShadowedBindings(jsx(`
      <input
        {...ime.bindComposition()}
        onBlur={commit}
      />`))).toHaveLength(1)
    expect(scanShadowedBindings(jsx(`
      <input
        onFocus={e => e.target.select()}
        {...ime.bindEnter({ onEnter: send })}
      />`))).toHaveLength(1)
  })

  it('flags an unconsumed decline on a textarea but not on a single-line input', () => {
    const textareaDecline = jsx(`
      <textarea
        value={text}
        onKeyDown={e => {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            if (ime.isComposing(e)) return
            e.currentTarget.blur()
          }
        }}
      />`)
    expect(scanUnconsumedTextareaDeclines(textareaDecline)).toHaveLength(1)
    // The same decline on a single-line input is the sanctioned non-consuming
    // remedy — nothing is inserted there.
    expect(scanUnconsumedTextareaDeclines(
      textareaDecline.map(l => l.replace('<textarea', '<input')),
    )).toHaveLength(0)
  })

  it('routes a consumed decline to the pair rule, not the textarea rule', () => {
    const consumedPair = jsx(`
      <textarea
        onKeyDown={e => {
          if (ime.isComposing(e)) return
          e.preventDefault()
        }}
      />`)
    expect(scanUnconsumedTextareaDeclines(consumedPair)).toHaveLength(0)
    expect(scanDeclineConsumePairs(consumedPair)).toHaveLength(1)
  })

  it('catches the generic-annotated spread shape BlockEditor actually shipped', () => {
    // The backtrack from the decline stops at the spread's own generic
    // annotation, which still names the tag (<HTMLTextAreaElement>): the exact
    // pre-fix BlockEditor shape is caught even though the `<textarea` opener
    // sits further up.
    expect(scanUnconsumedTextareaDeclines(jsx(`
      {...ime.bindComposition<HTMLTextAreaElement>({
        onBlur: () => onCommit(text),
      })}
      onKeyDown={e => {
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
          if (ime.isComposing(e)) return
          e.currentTarget.blur()
        }
      }}`))).toHaveLength(1)
  })
})
