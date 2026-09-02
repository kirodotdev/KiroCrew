/**
 * `isWaitToolTitle` — the transport-shaped name match behind the wait countdown.
 *
 * The three title forms asserted first are not hypothetical: the direct MCP path
 * sends `wait`, the pooled gateway namespaces it `kirocrew-core___wait`, and one
 * path appends a suffix (`wait (mcp)`). The `toolName === 'wait'` comparison this
 * replaced matched only the bare form, so on the other two transports the
 * countdown silently never appeared — which reads as "the feature is broken on my
 * machine" rather than as a name mismatch.
 *
 * Frontend mirror of `src/kiro_crew/acp/liveness.py::is_wait_tool`. The two have
 * to agree on every form here: the backend uses its copy to decide the session is
 * alive (and to keep minting `wait_state`), and this one decides whether anything
 * renders. A form only one side matches is a countdown that never shows for a
 * sleep the backend is happily tracking.
 */
import { describe, it, expect } from 'vitest'
import { isWaitToolTitle } from '../utils/waitToolTitle'

describe('isWaitToolTitle', () => {
  it('matches every transport shape of the wait tool title', () => {
    expect(isWaitToolTitle('wait')).toBe(true) // direct MCP
    expect(isWaitToolTitle('kirocrew-core___wait')).toBe(true) // pooled gateway
    expect(isWaitToolTitle('wait (mcp)')).toBe(true) // suffixed
  })

  it('is case-insensitive and ignores surrounding whitespace', () => {
    expect(isWaitToolTitle('WAIT')).toBe(true)
    expect(isWaitToolTitle('Wait')).toBe(true)
    expect(isWaitToolTitle(' wait ')).toBe(true)
    expect(isWaitToolTitle('\tKiroCrew-Core___Wait\n')).toBe(true)
  })

  it('rejects titles that merely contain the letters', () => {
    // Token equality, not substring — `wait` has to BE a token.
    expect(isWaitToolTitle('waiting')).toBe(false)
    expect(isWaitToolTitle('await')).toBe(false)
    expect(isWaitToolTitle('waitress')).toBe(false)
    expect(isWaitToolTitle('nowait')).toBe(false)
    // The shape a shell pill actually carries, for contrast.
    expect(isWaitToolTitle('Running: gh pr checks --watch')).toBe(false)
  })

  it('treats an empty or missing title as no match', () => {
    expect(isWaitToolTitle('')).toBe(false)
    expect(isWaitToolTitle('   ')).toBe(false)
    // Unreachable through the declared type, but the implementation guards it
    // because both call sites read a possibly-absent transport field
    // (`e.text || ''`, and a message content that may be bare).
    expect(isWaitToolTitle(undefined as unknown as string)).toBe(false)
  })

  it('REJECTS an unrelated wait_* tool from another server', () => {
    // The case that made the earlier token-membership rule unsafe, not merely
    // untidy. `slot.wait_state` is minted by whichever session
    // `_resolve_session_key()` resolved to, and that answers per RUNTIME rather
    // than per ACP session (#2347). So a subagent's `wait` can publish a deadline
    // onto its parent's slot while the parent's newest tool row is an unrelated
    // `wait_for_ci` from a different server. A token test accepts that title, the
    // countdown renders on it, and End-wait ends the subagent's sleep. The
    // contested-slot latch cannot cover it either: only one wait_id ever pings, so
    // there is no collision to detect.
    expect(isWaitToolTitle('wait_for_thing')).toBe(false)
    expect(isWaitToolTitle('wait-for-ci')).toBe(false)
    expect(isWaitToolTitle('wait_for_ci (mcp)')).toBe(false)
    expect(isWaitToolTitle('other___wait_for_ci')).toBe(false)
  })

  it('accepts a namespace prefix and an mcp suffix together', () => {
    // The two decorations are independent, so the combination has to hold too.
    expect(isWaitToolTitle('kirocrew-core___wait (mcp)')).toBe(true)
    expect(isWaitToolTitle('some.server_1___wait')).toBe(true)
  })

  it('rejects a bare namespace separator with nothing before it', () => {
    // Guards the prefix group against matching an empty namespace, which would
    // let a title that merely ENDS in `___wait` through.
    expect(isWaitToolTitle('___wait')).toBe(false)
  })
})
