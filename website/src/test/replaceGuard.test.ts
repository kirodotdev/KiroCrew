/**
 * Tests for `optsForReplace` — the guard that decides whether a file open may
 * re-target an existing tab in place, or must open its own tab to avoid
 * discarding a buffer the user has typed into.
 *
 * The consumer (`handleFileOpen`) applies it on BOTH the success and the failure
 * path, so these cases hold for a read that threw as much as one that resolved.
 */
import { describe, it, expect, vi } from 'vitest'
import { optsForReplace } from '../pages/chat/replaceGuard'

describe('optsForReplace', () => {
  it('keeps replaceId while the source buffer is untouched', () => {
    const opts = { replaceId: 'file:/a.md', canReplace: () => true }
    expect(optsForReplace(opts)).toBe(opts)
  })

  it('drops replaceId once the source buffer is dirty, so the tab is spared', () => {
    // The edits stay in their own tab and the requested file opens beside it.
    const opts = { replaceId: 'file:/a.md', canReplace: () => false, diffMode: true }
    expect(optsForReplace(opts)).toEqual({ replaceId: undefined, canReplace: opts.canReplace, diffMode: true })
  })

  it('asks at the moment of replacement, not when the navigation started', () => {
    // The file read sits between the click and the replacement, and that is
    // exactly when the user can start typing — so a cached answer is wrong.
    let dirty = false
    const canReplace = vi.fn(() => !dirty)
    const opts = { replaceId: 'file:/a.md', canReplace }

    expect(optsForReplace(opts)?.replaceId).toBe('file:/a.md')
    dirty = true
    expect(optsForReplace(opts)?.replaceId).toBeUndefined()
    expect(canReplace).toHaveBeenCalledTimes(2)
  })

  it('passes opts through untouched when the caller is not replacing anything', () => {
    const opts = { line: 447 }
    expect(optsForReplace(opts)).toBe(opts)
    expect(optsForReplace(undefined)).toBeUndefined()
  })

  it('leaves replaceId alone when no predicate was supplied', () => {
    // A caller that offers no way to re-check gets the behaviour it asked for;
    // only the rail supplies `canReplace`, and it always supplies it.
    const opts = { replaceId: 'file:/a.md' }
    expect(optsForReplace(opts)).toBe(opts)
  })
})
