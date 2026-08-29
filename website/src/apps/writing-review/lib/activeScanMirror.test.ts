/**
 * Tests for the active-scan sessionStorage mirror helpers.
 *
 * Provider state (activeJobId + activeJobDocName + activeJobPhase) survives
 * an unmount-remount cycle by writing to sessionStorage on every change and
 * reading it back synchronously on mount. The alternative -- fetching from
 * the backend as the only rehydration path -- has a gap: if the fetch
 * transiently fails during the remount window, the in-flight scan handle is
 * silently dropped and the user sees the ScanProgress card disappear until
 * they hard-refresh. sessionStorage is a per-tab, per-origin store: two
 * tabs each running a scan keep independent handles (unlike localStorage,
 * which would clobber across tabs on the same origin).
 *
 * The mirror is intentionally READ-ONLY as a rehydration source. Callers
 * always verify with the backend afterwards so a stale storage entry (job
 * was pruned, or another tab wrote before this tab reconnected) cannot
 * strand the user on a phantom job.
 *
 * All three functions defend against a hostile / sandboxed sessionStorage
 * (private-mode browsers, iframed contexts, quota exhaustion). A throw here
 * would crash the WritingReviewProvider mount, so silent fallback to
 * "no persisted state" is the correct posture.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  clearActiveScanMirror,
  readActiveScanMirror,
  writeActiveScanMirror,
  writingReviewActiveScanKey,
} from './activeScanMirror'

describe('activeScanMirror', () => {
  beforeEach(() => {
    // Reset per-test so a leaked value from one case cannot contaminate
    // another. The mirror is a per-tab store and vitest gives each test
    // a shared window.sessionStorage, so cleaning is explicit.
    window.sessionStorage.removeItem(writingReviewActiveScanKey)
  })

  afterEach(() => {
    window.sessionStorage.removeItem(writingReviewActiveScanKey)
  })

  it('reads an empty state when nothing has been written', () => {
    // Empty storage is the "fresh tab" case. Must return the empty
    // sentinel shape so ``useState`` initializers can use its fields
    // directly without a null-branch fork.
    const persistedState = readActiveScanMirror()
    expect(persistedState).toEqual({
      jobId: null,
      docName: null,
      phase: null,
    })
  })

  it('round-trips a written active-scan handle', () => {
    // The primary use case: provider writes on state change, reads on
    // mount. Values flow through unchanged.
    writeActiveScanMirror({
      jobId: 'job-abc-123',
      docName: 'design_doc.md',
      phase: 'scan',
    })
    const roundTrippedState = readActiveScanMirror()
    expect(roundTrippedState).toEqual({
      jobId: 'job-abc-123',
      docName: 'design_doc.md',
      phase: 'scan',
    })
  })

  it('clears the mirror when write is called with all-null fields', () => {
    // Terminal state (done / failed / interrupted) clears local
    // provider state to null. The mirror must clear too so the next
    // mount does not read stale data.
    writeActiveScanMirror({
      jobId: 'job-abc-123',
      docName: 'design_doc.md',
      phase: 'scan',
    })
    writeActiveScanMirror({ jobId: null, docName: null, phase: null })
    expect(readActiveScanMirror()).toEqual({
      jobId: null,
      docName: null,
      phase: null,
    })
    // Storage entry itself is removed, not just JSON-null'd, so a
    // future ``clear`` on this key from another surface stays a no-op.
    expect(window.sessionStorage.getItem(writingReviewActiveScanKey)).toBeNull()
  })

  it('clearActiveScanMirror also removes the storage entry', () => {
    // The explicit clear path (called on backend 404 for the
    // rehydrated job, or by tests). Must be idempotent -- clearing an
    // already-empty mirror is a no-op, not an error.
    writeActiveScanMirror({
      jobId: 'job-abc-123',
      docName: 'design_doc.md',
      phase: 'scan',
    })
    clearActiveScanMirror()
    expect(window.sessionStorage.getItem(writingReviewActiveScanKey)).toBeNull()
    // Idempotent second call.
    clearActiveScanMirror()
    expect(window.sessionStorage.getItem(writingReviewActiveScanKey)).toBeNull()
  })

  it('returns empty state when the stored JSON is corrupt', () => {
    // A hand-edited or partially-written entry must not crash the
    // provider mount. Falls back to the empty sentinel; the backend
    // fetch then re-establishes truth.
    window.sessionStorage.setItem(writingReviewActiveScanKey, '{invalid json}')
    expect(readActiveScanMirror()).toEqual({
      jobId: null,
      docName: null,
      phase: null,
    })
  })

  it('returns empty state when the stored JSON is a wrong shape', () => {
    // Defensive parse: an array, or a number, or a plain string, or
    // an object missing the expected fields, all fall back to empty
    // rather than surfacing garbage as an active scan.
    window.sessionStorage.setItem(writingReviewActiveScanKey, JSON.stringify(['nope']))
    expect(readActiveScanMirror()).toEqual({
      jobId: null,
      docName: null,
      phase: null,
    })
    window.sessionStorage.setItem(
      writingReviewActiveScanKey,
      JSON.stringify({ unrelated: 'field' }),
    )
    expect(readActiveScanMirror()).toEqual({
      jobId: null,
      docName: null,
      phase: null,
    })
  })
})
