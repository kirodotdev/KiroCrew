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

function sourceFiles(): string[] {
  return readdirSync(SRC, { recursive: true, encoding: 'utf8' })
    .map(p => p.split('\\').join('/'))
    .filter(p => /\.tsx?$/.test(p))
    .filter(p => !p.startsWith('test/') && !p.includes('__tests__'))
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

  it('never lets a standalone onBlur sit on an element that spreads the binding', () => {
    // JSX resolves duplicate props by last-one-wins, so a standalone `onBlur` after the
    // spread drops the latch reset and a standalone one before it drops the caller's own
    // handler. Both are silent. This is not hypothetical: the pet composer shipped the
    // first form in the very commit that made the binding mandatory, and only a blind
    // reviewer caught it — the hook can make the correct spelling AVAILABLE but only a
    // reader of the whole element can see the shadowing, so the check belongs here.
    //
    // Elements are bracketed by this tree's formatting (one attribute per line, the tag
    // closing on its own line). A file that formats differently is not flagged rather
    // than mis-flagged: the check fails open, which is the right direction for a rule
    // whose job is to catch a copied shape.
    const offenders: string[] = []
    for (const rel of sourceFiles()) {
      const lines = readFileSync(join(SRC, rel), 'utf8').split('\n')
      lines.forEach((line, i) => {
        if (!/\.\.\.\w+\.bindComposition/.test(line)) return
        let start = i
        while (start > 0 && !/<[A-Za-z]/.test(lines[start])) start--
        let end = i
        while (end < lines.length - 1 && !lines[end].includes('/>')) end++
        const span = lines.slice(start, end + 1)
        if (span.some(l => /^\s*onBlur=/.test(l))) offenders.push(`${rel}:${i + 1}`)
      })
    }
    // Pass the handler INTO bindComposition({ onBlur }) so both run.
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
