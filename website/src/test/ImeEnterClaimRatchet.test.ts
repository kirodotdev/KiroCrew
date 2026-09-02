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

/**
 * A `compositionstart` subscription is the seed of a composition LATCH, and a
 * latch hand-rolled beside a native handler is the second spelling this
 * ratchet exists to prevent: its author re-derives the flag-and-timer
 * semantics (the post-`compositionend` window, the stale-timer clear, the
 * stranded-latch recovery) and reliably gets one of them wrong. So each
 * subscription must feed the shared latch — `useImeGuard` for synthetic
 * handlers, its `createImeLatch` factory for native ones (document-capture
 * keydown listeners never see a synthetic event, so `claimEnter` is
 * structurally unavailable to them; `useListKeyboardNav` is the reference
 * consumer). The exemption is judged in a bounded window around EACH
 * subscription, on comment-stripped lines: a whole-file test would let one
 * sanctioned latch (or a mere comment mention) exempt every other
 * subscription in the file. The window accepts either the shared-guard call
 * itself or a handler delegating into a latch's own `onCompositionStart()`
 * (both in-tree consumers wire it through a one-line arrow). A
 * `compositionend`-only grace window (TerminalCompletion's xterm handler
 * tracks a timestamp, not a latch) does not subscribe to `compositionstart`
 * and stays out of scope — a structural distinction, not a pardoned file.
 */
const COMPOSITION_SUBSCRIBE = /addEventListener\(\s*['"]compositionstart['"]/
const SHARED_LATCH = /\b(?:useImeGuard|createImeLatch)\(|\.onCompositionStart\(\)/
const LATCH_WINDOW = 12

function scanUnlatchedCompositionSubscribers(lines: string[]): number[] {
  const code = lines.map(l => {
    const t = l.trim()
    return t.startsWith('//') || t.startsWith('*') || t.startsWith('/*') ? '' : l
  })
  const hits: number[] = []
  code.forEach((line, i) => {
    if (!COMPOSITION_SUBSCRIBE.test(line)) return
    const windowLines = code.slice(Math.max(0, i - LATCH_WINDOW), i + LATCH_WINDOW + 1)
    if (!windowLines.some(l => SHARED_LATCH.test(l))) hits.push(i + 1)
  })
  return hits
}

/**
 * An `Enter → blur` commit that consults NO composition signal at all.
 *
 * Every rule above needs the site to have referenced a signal — they police a
 * guard that is written WRONGLY. None of them can see a guard that is simply
 * ABSENT, and the absent one is the shape that spreads by copy: the panel this
 * rule was added for carried `if (e.key === 'Enter') e.currentTarget.blur()`
 * twice, on a folder-name field and a group cooldown, both copied from a
 * sibling channel panel BEFORE that sibling grew its guard. The whole tree was
 * otherwise clean, so nothing failed and no reviewer of either file could have
 * seen what the other one did.
 *
 * `blur()` on an Enter branch is the scoping signal, and it is a narrow one: in
 * this tree that spelling exists only to commit a text field on Enter, which is
 * exactly the action an IME's committing keydown must not trigger. The element
 * is cleared when the guard appears anywhere in its span — the signal call
 * itself, or a `bindEnter`/`bindComposition` spread that carries it — so a site
 * using the sanctioned shape in any of its spellings passes.
 */
const ENTER_BLUR = /key === 'Enter'|key !== 'Enter'/
const BLUR_COMMIT = /\.blur\(\)/
const GUARD_PRESENT = /ime\.(?:isComposing|claimEnter)\(|\.\.\.\w+\.bind(?:Enter|Composition)\b/
/** Lines of the handler/element around `i`, bounded by this tree's formatting. */
const COMMIT_WINDOW = 10

function scanUnguardedEnterBlurCommits(lines: string[]): number[] {
  const code = lines.map(l => {
    const t = l.trim()
    return t.startsWith('//') || t.startsWith('*') || t.startsWith('/*') ? '' : l
  })
  const hits: number[] = []
  code.forEach((line, i) => {
    if (!ENTER_BLUR.test(line)) return
    const window = code.slice(i, i + COMMIT_WINDOW + 1)
    // The commit has to be on this branch, not merely later in the file.
    if (!window.some(l => BLUR_COMMIT.test(l))) return
    // The guard may sit above (an early return) or on the element (a spread).
    const span = code.slice(Math.max(0, i - COMMIT_WINDOW), i + COMMIT_WINDOW + 1)
    if (!span.some(l => GUARD_PRESENT.test(l))) hits.push(i + 1)
  })
  return hits
}

/**
 * A Tab branch that moves focus but consults NO composition signal at all.
 *
 * The absent-guard twin of the Enter→blur rule above, for the OTHER
 * choose-class key: IMEs use Tab to cycle the candidate list, and on WebKit
 * the keydown that commits a candidate arrives after `compositionend` with
 * `isComposing` already false — so a hand-rolled dialog focus trap that wraps
 * the boundary Tab yanks focus and aborts the composition. Every rule above
 * is keyed on a composition signal the offender must already have NAMED, so
 * a trap that consults none of them matches none of them, and the shape
 * spreads by copy exactly like the Enter ones did: six dialogs carried it
 * before the shared latch existed, and a seventh grew in a page nobody wrote
 * an IME test for.
 *
 * `.focus(` within the branch's forward window is the scoping anchor, and it
 * is a deliberate one: in this tree a Tab branch that calls `.focus()` (with
 * or without options — `preventScroll` is this tree's own convention) exists
 * only to re-aim the key somewhere the user did not send it (a trap's wrap,
 * a picker's close-and-return). The guard is `claimKey(` BETWEEN the branch
 * and the first focus move after it — the claim has to run before the move
 * it declines, so a claim past the move (or a sibling branch's claim beyond
 * it) does not clear this one. A Tab branch that acts through STATE
 * (accepting a suggestion, indenting a list item) has no focus call to
 * anchor on and is out of THIS rule's scope by design: fail open rather
 * than mis-flag, consistent with the rest of this file. The rule below
 * covers that shape, anchored on the act instead of the move. A `.focus(`
 * further than the window is likewise not this structure.
 */
const TAB_BRANCH = /key === 'Tab'|key !== 'Tab'/
const FOCUS_MOVE = /\.focus\(/
// Either flavour of the claim clears the rule: `claimKey` for a native
// document/window listener, `claimSyntheticKey` for a React handler that also
// needs React's own propagation flag stopped. What the rule is about is that a
// claim RUNS before the move, not which event world the branch lives in.
const TAB_CLAIM = /\bclaim(?:Synthetic)?Key\(/
const TRAP_WINDOW = 25

/**
 * Blank out comment lines so a rule cannot be laundered by mentioning its own
 * remedy in prose — and so the prose that DOCUMENTS a defect shape (this file,
 * the hook, the tests that pin the seam) is not itself an offender.
 */
function codeLines(lines: string[]): string[] {
  return lines.map(l => {
    const t = l.trim()
    return t.startsWith('//') || t.startsWith('*') || t.startsWith('/*') ? '' : l
  })
}

function scanUnguardedTabFocusTraps(lines: string[]): number[] {
  const code = codeLines(lines)
  const hits: number[] = []
  code.forEach((line, i) => {
    if (!TAB_BRANCH.test(line)) return
    // The focus move has to be on this branch's window, not merely later in
    // the file — a Tab branch with no focus call is a different structure.
    const focusAt = code
      .slice(i, i + TRAP_WINDOW + 1)
      .findIndex(l => FOCUS_MOVE.test(l))
    if (focusAt === -1) return
    // The claim guards THIS branch only when it runs between the key check
    // and the move: one guarded branch must not clear an unguarded sibling.
    const span = code.slice(i, i + focusAt + 1)
    if (!span.some(l => TAB_CLAIM.test(l))) hits.push(i + 1)
  })
  return hits
}

/**
 * The synthetic half of a claim, hand-spelled.
 *
 * `claimKey` takes a NATIVE event, so a React `onKeyDown` that claims through
 * it has to reach into `.nativeEvent` — and then remember the second half
 * itself, because the native `stopPropagation()` does not set React's own
 * propagation flag and React walks that flag when dispatching to component
 * ancestors. So the spelling is a PAIR, and a pair is exactly what the rest of
 * this file exists to stop: four sites carried it (two fullscreen panels, a
 * sidebar column popover, and Modal's key isolation), and any one of them
 * could lose a half in a later edit with nothing to catch it.
 *
 * `claimSyntheticKey(e)` owns both halves, so the pair has one place to live.
 * The rule is the reach itself — a `claimKey(` whose argument is a
 * `.nativeEvent` — which is structural rather than a list of pardoned files:
 * a NATIVE listener passes its event straight through and is out of scope by
 * construction. The one sanctioned `.nativeEvent` claim is the implementation
 * of `claimSyntheticKey` inside the hook, which the tree scan skips the same
 * way the raw-flag rule above skips it.
 */
const HAND_SPELLED_SYNTHETIC_CLAIM = /\bclaimKey\([^)]*\.nativeEvent/

function scanHandSpelledSyntheticClaims(lines: string[]): number[] {
  const hits: number[] = []
  codeLines(lines).forEach((line, i) => {
    if (HAND_SPELLED_SYNTHETIC_CLAIM.test(line)) hits.push(i + 1)
  })
  return hits
}

/**
 * A Tab branch that takes the key from the browser and then ACTS ON THE FIELD,
 * with no composition signal anywhere.
 *
 * The state-setter twin of the rule above, for the half of the boundary-Tab
 * shape a focus anchor structurally cannot see. Four sites carried it: a path
 * bar accepting the highlighted suggestion into the draft, two markdown
 * editors re-indenting a list item by rewriting the whole textarea value, and
 * a composer accepting slash-command autocomplete. The hazard is the one the
 * focus rule pins — IMEs use Tab to cycle the candidate list, and on WebKit
 * the keydown that commits a candidate arrives after `compositionend` with
 * `isComposing` already false — but the damage lands in the field's own text
 * instead of in the focus ring, which is the worse half: the composing draft
 * is replaced, so the user loses what they were typing rather than where they
 * were.
 *
 * The anchor is `preventDefault()` FOLLOWED BY AN ACT, inside the branch and
 * with no focus move in it — the two anchors partition the shape between them
 * rather than double-reporting it. Consuming the key on its own is
 * deliberately NOT enough: a surface that swallows Tab to hold still
 * (`useListKeyboardNav`'s empty-list branch, whose host composer carries its
 * own guard) writes nothing an IME could corrupt, and flagging it would buy a
 * noisy rule an exemption list to quiet it. What makes an unclaimed Tab a
 * corruption is the act that follows the consumption, so that is what puts a
 * branch in scope.
 *
 * The claim clears the branch from either position that actually guards the
 * code path: on the branch before the consumption it owns, or as a
 * decline-and-return ABOVE the branch at no deeper nesting — an early
 * `if (!latch.claimKey(e)) return` covering every branch below it, which is
 * how `useListKeyboardNav` guards its own choose dispatch. A claim nested
 * inside a SIBLING branch is not on this branch's path and does not count,
 * the same asymmetry the rule above states. Indentation is the nesting proxy,
 * as it is for the element-bracketing rules below; unrecognized formatting
 * fails open.
 */
const TAB_CONSUME = /\.preventDefault\(/
/** A decline-and-return, in both spellings the tree uses. */
const TAB_DECLINE = /!\s*[\w.!?]*\bclaimKey\(/
/** Where a keydown handler opens — the ceiling for the decline search. */
const KEY_HANDLER = /\bonKeyDown\b|\bonKey\b|['"]keydown['"]/
/** Consuming the key and leaving the branch is plumbing, not an act. */
const BRANCH_PLUMBING =
  /^[{}();,\s]*$|^[\w.?!]*\.(?:preventDefault|stopPropagation|stopImmediatePropagation)\(\)[\s;)}]*$|^return(?:\s+(?:true|false))?[\s;)}]*$/
const HANDLER_WINDOW = 60

/** The branch's own lines, to the first dedent that closes it (this tree's formatting). */
function tabBranchSpan(code: string[], i: number): string[] {
  const indent = code[i].search(/\S/)
  const out = [code[i]]
  for (let j = i + 1; j < code.length && j <= i + TRAP_WINDOW; j++) {
    if (code[j].trim() === '') continue
    if (code[j].search(/\S/) <= indent && /^\s*[})]/.test(code[j])) break
    out.push(code[j])
  }
  return out
}

/** True when the branch does something after consuming the key. */
function actsAfterConsuming(branch: string[], at: number): boolean {
  // The one-line spelling puts the act on the consume line itself:
  // `{ e.preventDefault(); setDraft(suggestion) }`.
  const consume = branch[at]
  const tail = consume
    .slice(consume.search(TAB_CONSUME))
    .replace(/^\.preventDefault\(\)\s*;?/, '')
  return [tail, ...branch.slice(at + 1)].some(
    l => l.trim() !== '' && !BRANCH_PLUMBING.test(l.trim()),
  )
}

/** True when an early decline-and-return above the branch covers its path. */
function declinedAbove(code: string[], i: number): boolean {
  const indent = code[i].search(/\S/)
  for (let j = i - 1; j >= 0 && i - j <= HANDLER_WINDOW; j--) {
    const line = code[j]
    if (line.trim() === '') continue
    // Above the handler's own opening there is no shared path left to guard.
    if (KEY_HANDLER.test(line)) return false
    // Deeper than this branch means it sits inside a sibling one.
    if (line.search(/\S/) > indent) continue
    if (!TAB_DECLINE.test(line)) continue
    // The decline has to RETURN — on its own line, or the two-line spelling.
    if (/\breturn\b/.test(line)) return true
    for (let k = j + 1; k <= j + 2 && k < code.length; k++) {
      if (/\breturn\b/.test(code[k])) return true
    }
  }
  return false
}

function scanUnguardedTabStateWrites(lines: string[]): number[] {
  const code = lines.map(l => {
    const t = l.trim()
    return t.startsWith('//') || t.startsWith('*') || t.startsWith('/*') ? '' : l
  })
  const hits: number[] = []
  code.forEach((line, i) => {
    if (!TAB_BRANCH.test(line)) return
    const branch = tabBranchSpan(code, i)
    // A branch that re-aims focus is the rule above's shape, not this one.
    if (branch.some(l => FOCUS_MOVE.test(l))) return
    const at = branch.findIndex(l => TAB_CONSUME.test(l))
    if (at === -1) return
    if (!actsAfterConsuming(branch, at)) return
    if (branch.slice(0, at + 1).some(l => TAB_CLAIM.test(l))) return
    if (declinedAbove(code, i)) return
    hits.push(i + 1)
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

  it('never commits an Enter to blur with no composition guard at all', () => {
    // The rules above all require the site to have NAMED a composition signal,
    // so each one polices a guard written wrongly and none can see one that is
    // absent. `WhatsAppPanel` shipped the absent form twice, copied from
    // `WeixinPanel` before that panel grew its own guard: a CJK operator
    // pressing Enter to accept a candidate persisted the intermediate
    // composition text as the folder name. A behavioural test per panel could
    // not have caught it — the second copy was in a component nobody had
    // written an IME test for.
    const offenders: string[] = []
    for (const rel of sourceFiles()) {
      if (rel === 'hooks/useImeGuard.ts') continue
      const lines = readFileSync(join(SRC, rel), 'utf8').split('\n')
      for (const n of scanUnguardedEnterBlurCommits(lines)) offenders.push(`${rel}:${n}`)
    }
    // An entry here commits a text field on an Enter it never checked. Add the
    // early return (`if (ime.isComposing(e)) return`) before the blur, and the
    // `bindComposition` spread that tracks the latch it reads.
    expect(offenders).toEqual([])
  })

  it('never moves focus on a Tab the branch did not claim', () => {
    // The boundary-Tab twin of the rule above: `DevFleetPage` carried exactly
    // this — a hand-rolled focus trap on a document-capture listener, copied
    // from the same pre-latch shape six dialogs shared — and no rule in this
    // file could see it, because every one of them requires the site to have
    // named a composition signal it consulted no part of. Both of that trap's
    // ring boundaries are buttons today, so the defect was unreachable; this
    // rule is what pins that a layout change (or the next copy of the shape)
    // cannot make it reachable silently.
    const offenders: string[] = []
    for (const rel of sourceFiles()) {
      if (rel === 'hooks/useImeGuard.ts') continue
      const lines = readFileSync(join(SRC, rel), 'utf8').split('\n')
      for (const n of scanUnguardedTabFocusTraps(lines)) offenders.push(`${rel}:${n}`)
    }
    // An entry here re-aims a Tab it never checked. Route the branch through
    // the shared latch: `useDocumentImeLatch(...).claimKey(e)` for native
    // document/window listeners, `.claimSyntheticKey(e)` (or
    // `useImeGuard().claimKey(e)`) for React handlers — the claim runs BEFORE
    // the preventDefault() and focus move.
    expect(offenders).toEqual([])
  })

  it('never rewrites the field on a Tab the branch did not claim', () => {
    // The other half of the boundary-Tab shape: the rule above anchors on a
    // focus move, so the four branches that acted through STATE instead were
    // invisible to every rule in this file — a path bar accepting a suggestion
    // into the draft, two markdown editors re-indenting a list item, a
    // composer accepting slash-command autocomplete. Each replaced the text an
    // IME was still composing, and each sat one line away from the guard its
    // own Enter branch already used.
    const offenders: string[] = []
    for (const rel of sourceFiles()) {
      if (rel === 'hooks/useImeGuard.ts') continue
      const lines = readFileSync(join(SRC, rel), 'utf8').split('\n')
      for (const n of scanUnguardedTabStateWrites(lines)) offenders.push(`${rel}:${n}`)
    }
    // An entry here acts on a Tab it never checked. Claim the key first
    // (`if (!ime.claimKey(e)) return`) and keep the composition tracked with
    // the `bindComposition` spread the claim reads.
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

  it('never hand-rolls a composition latch beside a native handler', () => {
    // A `compositionstart` subscription outside the guard module must feed the
    // shared latch (`useImeGuard` / `createImeLatch`), never a private flag —
    // the native-event twin of the one-line rule above. `useListKeyboardNav`
    // shipped exactly the gap this pins: a document-capture Enter dispatch
    // with no composition reference at all, which on WebKit activated the
    // highlighted picker row with the keydown that committed an IME candidate.
    const offenders: string[] = []
    for (const rel of sourceFiles()) {
      if (rel === 'hooks/useImeGuard.ts') continue
      const lines = readFileSync(join(SRC, rel), 'utf8').split('\n')
      for (const n of scanUnlatchedCompositionSubscribers(lines)) {
        offenders.push(`${rel}:${n}`)
      }
    }
    // An entry here tracks composition with its own state. Consume
    // `createImeLatch()` (native handlers) or `useImeGuard()` (synthetic).
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

  it('never hand-spells the synthetic half of a claim', () => {
    // A React handler that claims through the native `claimKey` has to reach
    // into `.nativeEvent` and then stop React's own propagation flag itself —
    // two halves, one call site, the drift this file exists to prevent. Four
    // sites carried the pair before `claimSyntheticKey` owned it (#5542).
    const offenders: string[] = []
    for (const rel of sourceFiles()) {
      // The hook is where the pair legitimately lives: `claimSyntheticKey` IS
      // the one `.nativeEvent` claim, the same exemption the raw-flag rule
      // above takes for the same reason.
      if (rel === 'hooks/useImeGuard.ts') continue
      const hits = scanHandSpelledSyntheticClaims(readFileSync(join(SRC, rel), 'utf8').split('\n'))
      offenders.push(...hits.map(n => `${rel}:${n}`))
    }
    // Claim with `latch.claimSyntheticKey(e)` instead — it consumes the native
    // event AND stops the synthetic flag, so neither half can be forgotten.
    expect(offenders).toEqual([])
  })

  it('exposes no composition binding without the latch recovery', () => {
    // A binding that tracks composition but does not reset on blur lets a surface
    // strand itself: an abandoned composition latches the guard, and since claimEnter
    // consumes what it declines, the surface then silently stops sending. The hook is
    // the only place that can make that unreachable, so it must not hand out a
    // recovery-less binding for a caller to pick by mistake.
    const hook = readFileSync(join(SRC, 'hooks/useImeGuard.ts'), 'utf8')
    // Anchor on the hook itself: the file also exports the `createImeLatch`
    // factory (the tracked latch shared with native-event consumers), whose
    // own `return {` would otherwise be the first match.
    const hookBody = hook.slice(hook.indexOf('export function useImeGuard'))
    const returned = /return \{([^}]*)\}/.exec(hookBody)?.[1] ?? ''
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

  it('flags a compositionstart subscription that feeds a private flag, not the shared latch', () => {
    const handRolled = jsx(`
      let composing = false
      document.addEventListener('compositionstart', () => { composing = true }, true)
      document.addEventListener('compositionend', () => { composing = false }, true)`)
    expect(scanUnlatchedCompositionSubscribers(handRolled)).toHaveLength(1)
    // The sanctioned shapes: the same subscription feeding the shared latch,
    // via the factory call or a handler delegating into it.
    expect(scanUnlatchedCompositionSubscribers(jsx(`
      const latch = createImeLatch()
      document.addEventListener('compositionstart', () => latch.onCompositionStart(), true)`))).toHaveLength(0)
    expect(scanUnlatchedCompositionSubscribers(jsx(`
      const onStart = () => imeRef.current.onCompositionStart()
      el.addEventListener('compositionstart', onStart)`))).toHaveLength(0)
    // A compositionend-only grace window (TerminalCompletion's xterm handler)
    // is a timestamp, not a latch, and stays out of scope.
    expect(scanUnlatchedCompositionSubscribers(jsx(`
      ta.addEventListener('compositionend', done)`))).toHaveLength(0)
    // The exemption is per subscription and ignores comments: a hand-rolled
    // flag is still flagged when the sanctioned latch is merely mentioned in
    // a nearby comment, or lives elsewhere in the same file beyond the window.
    expect(scanUnlatchedCompositionSubscribers(jsx(`
      // the shared guard is createImeLatch(), which this deliberately skips
      let composing = false
      document.addEventListener('compositionstart', () => { composing = true }, true)`))).toHaveLength(1)
  })

  it('flags an Enter-to-blur commit that names no composition signal', () => {
    // The exact shape WhatsAppPanel shipped twice.
    expect(scanUnguardedEnterBlurCommits(jsx(`
      <input
        onChange={setFolderName}
        onBlur={commitFolderName}
        onKeyDown={e => {
          if (e.key === 'Enter') e.currentTarget.blur()
        }}
      />`))).toHaveLength(1)
    // The sanctioned shape: the early return plus the tracking spread.
    expect(scanUnguardedEnterBlurCommits(jsx(`
      <input
        onChange={setFolderName}
        {...ime.bindComposition({ onBlur: commitFolderName })}
        onKeyDown={e => {
          if (e.key !== 'Enter') return
          if (ime.isComposing(e)) return
          e.currentTarget.blur()
        }}
      />`))).toHaveLength(0)
    // `claimEnter` is the textarea remedy and clears the rule too.
    expect(scanUnguardedEnterBlurCommits(jsx(`
      onKeyDown={e => {
        if (e.key !== 'Enter') return
        if (!ime.claimEnter(e)) return
        e.currentTarget.blur()
      }}`))).toHaveLength(0)
    // Scoped to a COMMIT: an Enter branch that does something other than blur a
    // field is a different structure and out of scope, as is a blur that no
    // Enter branch reaches.
    expect(scanUnguardedEnterBlurCommits(jsx(`
      onKeyDown={e => {
        if (e.key === 'Enter') onSelect(row)
      }}`))).toHaveLength(0)
    expect(scanUnguardedEnterBlurCommits(jsx(`
      const dismiss = () => inputRef.current?.blur()`))).toHaveLength(0)
    // A guard mentioned only in a comment does not launder the site.
    expect(scanUnguardedEnterBlurCommits(jsx(`
      onKeyDown={e => {
        // ime.isComposing is handled upstream, honestly
        if (e.key === 'Enter') e.currentTarget.blur()
      }}`))).toHaveLength(1)
  })

  it('flags a boundary-Tab trap that does not claim the key', () => {
    // The pre-latch shape all six converted dialogs shared, and the one
    // DevFleetPage carried after them: two boundary branches, each
    // preventDefault() + a focus wrap, no claim anywhere. Both branches flag —
    // each is independently a place the defect re-enters.
    expect(scanUnguardedTabFocusTraps(jsx(`
      const onKey = (e: KeyboardEvent) => {
        if (e.key === 'Tab' && e.shiftKey && document.activeElement === cancelRef.current) {
          e.preventDefault()
          confirmRef.current?.focus()
        } else if (e.key === 'Tab' && !e.shiftKey && document.activeElement === confirmRef.current) {
          e.preventDefault()
          cancelRef.current?.focus()
        }
      }`))).toHaveLength(2)
    // The sanctioned shape: claimKey before the preventDefault and focus move.
    expect(scanUnguardedTabFocusTraps(jsx(`
      const onKey = (e: KeyboardEvent) => {
        if (e.key === 'Tab' && e.shiftKey && document.activeElement === cancelRef.current) {
          if (!imeLatch.claimKey(e)) return
          e.preventDefault()
          confirmRef.current?.focus()
        } else if (e.key === 'Tab' && !e.shiftKey && document.activeElement === confirmRef.current) {
          if (!imeLatch.claimKey(e)) return
          e.preventDefault()
          cancelRef.current?.focus()
        }
      }`))).toHaveLength(0)
    // The enumerating trap (the converted dialogs' shape): the wrap decision
    // sits several code lines below the key check, still inside the window.
    expect(scanUnguardedTabFocusTraps(jsx(`
      const onKeyDown = (event: KeyboardEvent) => {
        if (event.key !== 'Tab') return
        const focusable = getFocusable(dialogRef.current)
        if (focusable.length === 0) return
        const first = focusable[0]
        const last = focusable[focusable.length - 1]
        const wrapsBackward = event.shiftKey && document.activeElement === first
        const wrapsForward = !event.shiftKey && document.activeElement === last
        if (!wrapsBackward && !wrapsForward) return
        event.preventDefault()
        ;(wrapsBackward ? last : first).focus()
      }`))).toHaveLength(1)
    expect(scanUnguardedTabFocusTraps(jsx(`
      const onKeyDown = (event: KeyboardEvent) => {
        if (event.key !== 'Tab') return
        const focusable = getFocusable(dialogRef.current)
        if (focusable.length === 0) return
        const first = focusable[0]
        const last = focusable[focusable.length - 1]
        const wrapsBackward = event.shiftKey && document.activeElement === first
        const wrapsForward = !event.shiftKey && document.activeElement === last
        if (!wrapsBackward && !wrapsForward) return
        if (!imeLatch.claimKey(event)) return
        event.preventDefault()
        ;(wrapsBackward ? last : first).focus()
      }`))).toHaveLength(0)
    // ONE guarded branch must not clear an unguarded sibling: the claim has
    // to sit between each branch and the move it guards, so the sibling's
    // claim (which sits past this branch's own focus call) does not count.
    expect(scanUnguardedTabFocusTraps(jsx(`
      const onKey = (e: KeyboardEvent) => {
        if (e.key === 'Tab' && e.shiftKey && document.activeElement === cancelRef.current) {
          if (!imeLatch.claimKey(e)) return
          e.preventDefault()
          confirmRef.current?.focus()
        } else if (e.key === 'Tab' && !e.shiftKey && document.activeElement === confirmRef.current) {
          e.preventDefault()
          cancelRef.current?.focus()
        }
      }`))).toHaveLength(1)
    // A focus call carrying options is this tree's own convention
    // (preventScroll keeps the page behind a dialog from twitching) and
    // anchors the rule the same as a bare one.
    expect(scanUnguardedTabFocusTraps(jsx(`
      const onKey = (e: KeyboardEvent) => {
        if (e.key !== 'Tab') return
        e.preventDefault()
        items[0].focus({ preventScroll: true })
      }`))).toHaveLength(1)
    // The one-line close-and-return spelling (a picker's Tab-to-dismiss): the
    // focus call sits ON the branch line, and the same-line window catches it.
    expect(scanUnguardedTabFocusTraps(jsx(`
      onKeyDown={e => {
        if (e.key === 'Escape' || e.key === 'Tab') { e.preventDefault(); onOpenChange(false); btnRef?.current?.focus() }
      }}`))).toHaveLength(1)
    // Its guarded form claims through the synthetic delegate.
    expect(scanUnguardedTabFocusTraps(jsx(`
      onKeyDown={e => {
        if (e.key === 'Escape' || e.key === 'Tab') {
          if (!ime.claimKey(e)) return
          e.preventDefault(); onOpenChange(false); btnRef?.current?.focus()
        }
      }}`))).toHaveLength(0)
    // A React handler on the dialog panel claims the same key through the
    // latch's synthetic twin, which also stops React's propagation flag. Both
    // spellings clear the rule — the point is a claim before the move.
    expect(scanUnguardedTabFocusTraps(jsx(`
      onKeyDown={e => {
        if (e.key !== 'Tab') return
        if (!wrapsBackward && !wrapsForward) return
        if (!fsImeLatch.claimSyntheticKey(e)) return
        e.preventDefault()
        ;(wrapsBackward ? last : first).focus()
      }}`))).toHaveLength(0)
    // Scoped to a FOCUS MOVE: a Tab branch that acts through state (accepting
    // a suggestion, indenting a list item) has no focus call to anchor on and
    // is out of THIS rule's scope — the state-write rule below owns it.
    expect(scanUnguardedTabFocusTraps(jsx(`
      onKeyDown={e => {
        if (e.key === 'Tab' && open && suggestions.length > 0) { e.preventDefault(); setDraft(suggestions[0].path) }
      }}`))).toHaveLength(0)
    // A focus call beyond the window is a different structure, not this trap.
    const farFocus = [
      "if (e.key !== 'Tab') return",
      ...Array.from({ length: 30 }, () => 'noop()'),
      'inputRef.current?.focus()',
    ]
    expect(scanUnguardedTabFocusTraps(farFocus)).toHaveLength(0)
    // A claim mentioned only in a comment does not launder the site.
    expect(scanUnguardedTabFocusTraps(jsx(`
      const onKey = (e: KeyboardEvent) => {
        // claimKey( is deliberately skipped here, honestly
        if (e.key === 'Tab' && e.shiftKey && document.activeElement === cancelRef.current) {
          e.preventDefault()
          confirmRef.current?.focus()
        }
      }`))).toHaveLength(1)
  })

  it('flags a hand-spelled synthetic claim but not a native one or the unified call', () => {
    // The exact pair the four sites shipped: a native claim reached through
    // `.nativeEvent`, with the synthetic half remembered by the caller.
    expect(scanHandSpelledSyntheticClaims(jsx(
      '          if (!fsImeLatch.claimKey(e.nativeEvent)) { e.stopPropagation(); return }',
    ))).toHaveLength(1)
    // Dropping the caller-side half is the drift the rule is really about, and
    // it must flag too — otherwise the rule would only catch the CORRECT
    // spelling of a shape it wants gone.
    expect(scanHandSpelledSyntheticClaims(jsx(
      '      if (!imeLatch.claimKey(e.nativeEvent)) return',
    ))).toHaveLength(1)
    // Modal's shape: the pair split across a condition with no early return.
    expect(scanHandSpelledSyntheticClaims(jsx(
      '      if (!imeLatch.claimKey(e.nativeEvent)) e.stopPropagation()',
    ))).toHaveLength(1)
    // The remedy.
    expect(scanHandSpelledSyntheticClaims(jsx(
      '          if (!fsImeLatch.claimSyntheticKey(e)) return',
    ))).toHaveLength(0)
    // A NATIVE listener passes its own event straight through — the other
    // flavour of the same latch, and out of scope by construction.
    expect(scanHandSpelledSyntheticClaims(jsx(
      '        if (!imeLatch.claimKey(e)) return',
    ))).toHaveLength(0)
    // Prose describing the defect is not the defect.
    expect(scanHandSpelledSyntheticClaims(jsx(
      '  // sites used to spell claimKey(e.nativeEvent) plus a caller-side stop',
    ))).toHaveLength(0)
  })

  it('flags a Tab branch that acts on the field without claiming the key', () => {
    // The one-line accept-the-suggestion shape the path bar carried: consume
    // the key, replace the draft, no composition signal anywhere.
    expect(scanUnguardedTabStateWrites(jsx(`
      onKeyDown={e => {
        if (e.key === 'Tab' && open && suggestions.length > 0) { e.preventDefault(); setDraft(suggestions[0].path) }
      }}`))).toHaveLength(1)
    // Its sanctioned form: the claim runs before the consumption it owns.
    expect(scanUnguardedTabStateWrites(jsx(`
      onKeyDown={e => {
        if (e.key === 'Tab' && open && suggestions.length > 0) { if (!ime.claimKey(e)) return; e.preventDefault(); setDraft(suggestions[0].path) }
      }}`))).toHaveLength(0)
    // The guard-clause spelling both markdown editors used: the early
    // `!== 'Tab'` return scopes the rest of the handler, and the rewrite sits
    // several lines below its own bail-outs.
    expect(scanUnguardedTabStateWrites(jsx(`
      onKeyDown={e => {
        if (e.key !== 'Tab') return
        const ta = e.currentTarget
        const next = shiftListItem(content, ta.selectionStart, e.shiftKey)
        if (!next) return
        e.preventDefault()
        ta.value = next.text
        edit(next.text)
      }}`))).toHaveLength(1)
    expect(scanUnguardedTabStateWrites(jsx(`
      onKeyDown={e => {
        if (e.key !== 'Tab') return
        const ta = e.currentTarget
        const next = shiftListItem(content, ta.selectionStart, e.shiftKey)
        if (!next) return
        if (!ime.claimKey(e)) return
        e.preventDefault()
        ta.value = next.text
        edit(next.text)
      }}`))).toHaveLength(0)
    // A claim that runs AFTER the consumption does not guard it: by then the
    // browser has already been told the site owns the key.
    expect(scanUnguardedTabStateWrites(jsx(`
      onKeyDown={e => {
        if (e.key === 'Tab') {
          e.preventDefault()
          if (!ime.claimKey(e)) return
          setInput(filtered[cmdIdx].cmd)
        }
      }}`))).toHaveLength(1)
    // Consuming the key to hold a surface STILL is not an act: the empty-list
    // branch swallows Tab so the picker stays put and writes nothing an IME
    // could corrupt. Flagging it would need an exemption list to quiet it.
    expect(scanUnguardedTabStateWrites(jsx(`
      const onKey = (e: KeyboardEvent) => {
        if (n === 0) {
          if (e.key === 'Enter' || e.key === 'Tab') {
            if (releaseKeysWhenEmpty) {
              onClose()
              return
            }
            e.preventDefault()
            e.stopPropagation()
          }
          return
        }
      }`))).toHaveLength(0)
    // An early decline-and-return at no deeper nesting covers every branch
    // below it — the shape `useListKeyboardNav` guards its choose dispatch
    // with, in both the one-line and the wrapped spelling.
    expect(scanUnguardedTabStateWrites(jsx(`
      const onKey = (e: KeyboardEvent) => {
        if ((e.key === 'Enter' || e.key === 'Tab') && !latch.claimKey(e)) {
          return
        }
        if (e.key === 'Enter') {
          e.preventDefault()
          onChoose(selected)
        } else if (e.key === 'Tab') {
          e.preventDefault()
          onChoose(selected)
        }
      }`))).toHaveLength(0)
    // …but a claim nested inside a SIBLING branch is not on this branch's
    // path, so it does not clear it (the asymmetry the focus rule states).
    expect(scanUnguardedTabStateWrites(jsx(`
      const onKey = (e: KeyboardEvent) => {
        if (e.key === 'Escape') {
          if (!latch.claimKey(e)) return
          e.preventDefault()
          onClose()
          return
        }
        if (e.key === 'Tab') {
          e.preventDefault()
          onChoose(selected)
        }
      }`))).toHaveLength(1)
    // A decline that never returns is not a guard: the branch runs on anyway.
    expect(scanUnguardedTabStateWrites(jsx(`
      const onKey = (e: KeyboardEvent) => {
        const declined = !latch.claimKey(e)
        if (e.key === 'Tab') {
          e.preventDefault()
          onChoose(selected)
        }
      }`))).toHaveLength(1)
    // A branch that never takes the key from the browser is a different
    // structure: nothing was re-aimed, so there is nothing to have claimed.
    expect(scanUnguardedTabStateWrites(jsx(`
      onKeyDown={e => {
        if (e.key === 'Tab') setHint(null)
      }}`))).toHaveLength(0)
    // A claim mentioned only in a comment does not launder the site.
    expect(scanUnguardedTabStateWrites(jsx(`
      onKeyDown={e => {
        if (e.key === 'Tab') {
          // ime.claimKey( is handled upstream, honestly
          e.preventDefault()
          setInput(filtered[cmdIdx].cmd)
        }
      }}`))).toHaveLength(1)
  })
})
