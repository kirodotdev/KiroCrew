import { describe, it, expect } from 'vitest'
import { enclosingRepoRootOf } from '../pages/chat/page/composerChips'

describe('enclosingRepoRootOf', () => {
  it('names the enclosing root for a plain folder inside a repository', () => {
    expect(enclosingRepoRootOf({ repo: true, repoRoot: '/home/me', isRepoRoot: false }, false)).toBe('/home/me')
  })

  it('is undefined for a project that is its own repository root', () => {
    expect(enclosingRepoRootOf({ repo: true, repoRoot: '/repo', isRepoRoot: true }, false)).toBeUndefined()
  })

  it('is undefined for a plain non-git folder', () => {
    expect(enclosingRepoRootOf({ repo: false }, false)).toBeUndefined()
  })

  it('is undefined while the probe is pending or after it failed', () => {
    expect(enclosingRepoRootOf(undefined, false)).toBeUndefined()
    // React Query keeps the last good data after a failed refetch; an errored
    // probe must not keep withholding the worktree action on stale data.
    expect(enclosingRepoRootOf({ repo: true, repoRoot: '/home/me', isRepoRoot: false }, true)).toBeUndefined()
  })

  it('is undefined for a gateway that does not send isRepoRoot', () => {
    // Older gateways: keep the action offered and the server as the backstop.
    expect(enclosingRepoRootOf({ repo: true, repoRoot: '/repo' }, false)).toBeUndefined()
  })
})
