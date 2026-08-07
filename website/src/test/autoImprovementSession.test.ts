import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, it, expect } from 'vitest'

import { sessionKey, truncate } from '../apps/auto-improvement/lib/agentSession'
import { findingPrompt, prPrompt, rulerPrompt, runPrompt } from '../apps/auto-improvement/lib/prompts'

/** A fixture repo for the cases that are about kind + id, not the repo segment. */
const R = 'octo/repo'

describe('sessionKey (resumable chat-session identity)', () => {
  // The key now carries a fingerprint suffix on each free-form segment (repo, id) so a
  // lossy sanitization cannot alias two distinct inputs (see the collision test below).
  // The readable prefix is unchanged, so assert on that prefix + the behavioral guards
  // rather than the exact literal, which would otherwise pin the hash.
  it('namespaces by kind so the same number is not one conversation', () => {
    // Regression guard: a PR and a finding can share an id. Without the kind
    // prefix, "discuss finding 7" would resume "discuss PR 7" and overwrite it.
    expect(sessionKey('pr', 7, R)).not.toEqual(sessionKey('finding', 7, R))
    expect(sessionKey('pr', 7, R)).toMatch(/^pr-octo-repo\.[0-9a-f]{8}-7\./)
    expect(sessionKey('finding', 7, R)).toMatch(/^finding-octo-repo\.[0-9a-f]{8}-7\./)
  })

  it('is stable for the same subject, so a repeat click resumes', () => {
    expect(sessionKey('pr', 42, R)).toBe(sessionKey('pr', 42, R))
  })

  it('sanitizes an id into a filename-safe key', () => {
    // The key becomes a filename on the backend, which rejects traversal.
    const trav = sessionKey('finding', '../../etc/passwd', R)
    expect(trav).toContain('finding-octo-repo')
    expect(trav).toContain('etc-passwd')
    expect(trav).not.toContain('/')
    expect(trav).not.toContain('..')
    expect(sessionKey('run', 'a b/c', R)).toContain('run-octo-repo')
    expect(sessionKey('run', 'a b/c', R)).toContain('a-b-c')
  })

  it('never produces an empty or separator-shaped key', () => {
    expect(sessionKey('run', '///', R)).toContain('run-octo-repo')
    expect(sessionKey('run', '///', R)).toContain('unknown')
    expect(sessionKey('run', '', R)).toContain('unknown')
    expect(sessionKey('run', '///', R)).not.toContain('/')
  })

  it('keeps a readable fingerprint token in the key', () => {
    expect(sessionKey('finding', 'deadbeef1234', R)).toContain('deadbeef1234')
  })
})

describe('sessionKey — the repository is part of the identity', () => {
  /**
   * Session records live at the DATA ROOT, not under the per-repository workspace
   * (`store.sessions_dir` says so explicitly: "a chat session ... may reference any
   * repo, so it is not scoped to the active one"). With a key of only `kind-id`, that
   * root is shared: discuss repo A's PR #1, retarget the app at repo B, discuss ITS
   * PR #1, and `loadRecord('pr-1')` resumes repo A's conversation about a different
   * pull request. Raised by the GPT review.
   */
  it('does not collide across repositories for the same subject id', () => {
    expect(sessionKey('pr', 1, 'octo/alpha')).not.toEqual(sessionKey('pr', 1, 'octo/beta'))
  })

  it('does not collide when two distinct repos sanitize to the same safe segment', () => {
    // `safeSegment` is lossy: `team/service-api` and `team-service/api` both collapse to
    // `team-service-api`, so a key built from the safe form alone would resume the WRONG
    // repository's conversation. The raw-value fingerprint makes the mapping injective.
    // Raised by the GPT review.
    expect(sessionKey('pr', 1, 'team/service-api')).not.toEqual(sessionKey('pr', 1, 'team-service/api'))
    // A third pair that also collapses identically under naive sanitization.
    expect(sessionKey('finding', 'a', 'o/r-x')).not.toEqual(sessionKey('finding', 'a', 'o-r/x'))
  })

  it('is stable for the same repo + subject, so a repeat click still resumes', () => {
    expect(sessionKey('pr', 1, 'octo/alpha')).toBe(sessionKey('pr', 1, 'octo/alpha'))
  })

  it('separates the singleton subjects that every repository has', () => {
    // The worst case: the ruler row passes `id: 'current'`, so EVERY repository the
    // app is ever pointed at shared one `ruler-current` record.
    expect(sessionKey('ruler', 'current', 'octo/alpha'))
      .not.toEqual(sessionKey('ruler', 'current', 'octo/beta'))
  })

  it('keeps the repo segment filename-safe', () => {
    // The key becomes a filename; a slug must not reintroduce a path separator.
    const key = sessionKey('pr', 1, '../../etc/passwd')
    expect(key).not.toContain('/')
    expect(key).not.toContain('..')
  })

  it('still works when no repository is known yet', () => {
    // Config may not have loaded; a missing repo must not produce an empty or
    // traversal-shaped segment.
    expect(sessionKey('pr', 1, '')).toBeTruthy()
    expect(sessionKey('pr', 1, '')).not.toContain('/')
  })
})

describe('truncate (slot titles stay readable in the folder list)', () => {
  it('leaves a short title alone', () => {
    expect(truncate('short')).toBe('short')
  })

  it('ellipsizes and trims trailing space', () => {
    expect(truncate('x'.repeat(60)).endsWith('…')).toBe(true)
    expect(truncate('abc def', 4)).toBe('abc…')
  })
})

describe('seed prompts', () => {
  const CONSTRAINT_MARKERS = ['never publish', 'never edit the ruler']

  function assertConstraints(prompt: string) {
    const low = prompt.toLowerCase()
    for (const marker of CONSTRAINT_MARKERS) {
      expect(low, `prompt is missing the "${marker}" constraint`).toContain(marker)
    }
  }

  it('every surface carries the draft-only and no-harness-editing constraints', () => {
    // These are the app's whole value proposition: an agent that publishes a PR
    // or edits the ruler has defeated the measurement. A new surface must not be
    // able to forget them.
    assertConstraints(prPrompt({ number: 1, title: 't', url: 'https://github.com/o/r/pull/1' }))
    assertConstraints(findingPrompt({ fingerprint: 'ab', kind: 'perf', target: 'x', status: 'filed' }))
    assertConstraints(rulerPrompt({ status: 'calibrated' }))
    assertConstraints(runPrompt({ runId: 'r1' }))
  })

  it('the PR prompt leads with the url and asks what is blocking before changing anything', () => {
    const p = prPrompt({
      number: 42,
      title: 'Speed up the parser',
      url: 'https://github.com/o/r/pull/42',
      verdict: 'PROGRESS',
      verdictReason: 'failing checks: ci',
      checks: '1 failing',
      mergeable: 'mergeable',
    })
    expect(p).toContain('https://github.com/o/r/pull/42')
    expect(p).toContain('PROGRESS')
    expect(p).toContain('failing checks: ci')
    expect(p.toLowerCase()).toContain('before changing anything')
  })

  it('omits absent optional PR fields rather than printing empties', () => {
    const p = prPrompt({ number: 1, title: 't', url: 'https://github.com/o/r/pull/1' })
    expect(p).not.toContain('Current state:')
    expect(p).not.toContain('CI checks:')
    expect(p).not.toContain('Mergeability:')
  })

  it('the finding prompt invites discarding a weak result', () => {
    const p = findingPrompt({ fingerprint: 'ff', kind: 'bug', target: 'parser.py', status: 'filed' })
    expect(p).toContain('parser.py')
    expect(p.toLowerCase()).toContain('noise band')
    expect(p.toLowerCase()).toContain('discarded')
  })

  it('the ruler prompt refuses to proceed on an untrustworthy metric', () => {
    const p = rulerPrompt({ status: 'uncalibrated', noiseBand: '4ms', canary: 'failed' })
    expect(p).toContain('uncalibrated')
    expect(p).toContain('4ms')
    expect(p.toLowerCase()).toContain('not trustworthy')
  })

  it('the run prompt treats a zero-keep run as a legitimate outcome', () => {
    const p = runPrompt({ runId: 'run-9', cycles: 12, kept: 0, drafted: 0 })
    expect(p).toContain('run-9')
    expect(p).toContain('12')
    expect(p.toLowerCase()).toContain('not automatically a failed run')
  })
})

describe('openSession re-entry latch (double-click cannot orphan a conversation)', () => {
  /**
   * `setBusy(true)` is React state and therefore applied ASYNCHRONOUSLY, so it cannot
   * serialize `openSession`: a rapid double-click on "discuss" runs the callback twice
   * before either render lands. Both invocations see "no record", both create a seeded
   * slot, and the second `saveRecord` overwrites the first slot mapping — orphaning a
   * live conversation the user can no longer reach.
   *
   * The fix is a synchronous `useRef` latch, checked and set BEFORE the first `await`.
   * Asserted structurally on the source: the hook needs Redux + router providers to
   * render, and the property that matters here is the ORDERING of the guard against the
   * first suspension point, which reads exactly. Raised by the GPT review.
   */
  const SRC = readFileSync(
    join(__dirname, '..', 'apps', 'auto-improvement', 'lib', 'agentSession.ts'),
    'utf-8',
  )

  it('uses a synchronous ref latch, not React state, to serialize opens', () => {
    expect(SRC).toContain('useRef')
    expect(SRC).toMatch(/inFlight\.current\.has\(/)
    expect(SRC).toMatch(/inFlight\.current\.add\(/)
  })

  it('takes the latch before the first await, or the race is still open', () => {
    const body = SRC.slice(SRC.indexOf('const openSession'))
    const guard = body.indexOf('inFlight.current.has(')
    const firstAwait = body.indexOf('await ')
    expect(guard).toBeGreaterThan(-1)
    expect(firstAwait).toBeGreaterThan(-1)
    expect(guard).toBeLessThan(firstAwait)
  })

  it('releases the latch in finally, so a subject can be reopened', () => {
    const body = SRC.slice(SRC.indexOf('const openSession'))
    const fin = body.indexOf('} finally {')
    expect(fin).toBeGreaterThan(-1)
    expect(body.slice(fin)).toMatch(/inFlight\.current\.delete\(/)
  })
})
