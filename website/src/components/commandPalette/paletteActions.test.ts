import { describe, it, expect, vi } from 'vitest'
import { resolveInvokableEnter } from './paletteActions'

/**
 * Unit tests for the pure §2 Enter-matrix primitive {@link resolveInvokableEnter}
 * (Search Everywhere, step 17). The `usePaletteActions` hook is a thin
 * store/router wrapper exercised via the provider tests + the palette component
 * tests; the security-relevant decision (insert vs new-session) lives here and
 * is tested in isolation.
 */

function sinks() {
  const insertToken = vi.fn()
  const newSessionWithToken = vi.fn()
  return { insertToken, newSessionWithToken }
}

describe('resolveInvokableEnter — active chat present', () => {
  it('inserts the token into the active composer (does not open a new session)', () => {
    const s = sinks()
    resolveInvokableEnter(true, '$brazil', s)()
    expect(s.insertToken).toHaveBeenCalledTimes(1)
    expect(s.insertToken).toHaveBeenCalledWith('$brazil')
    expect(s.newSessionWithToken).not.toHaveBeenCalled()
  })
})

describe('resolveInvokableEnter — no active chat', () => {
  it('opens a new session seeded with the token (does not insert)', () => {
    const s = sinks()
    resolveInvokableEnter(false, '@team/sop', s)()
    expect(s.newSessionWithToken).toHaveBeenCalledTimes(1)
    expect(s.newSessionWithToken).toHaveBeenCalledWith('@team/sop')
    expect(s.insertToken).not.toHaveBeenCalled()
  })
})

describe('resolveInvokableEnter — returns a callback (lazy)', () => {
  it('does not invoke any sink until the returned callback is called', () => {
    const s = sinks()
    resolveInvokableEnter(true, '$x', s)
    resolveInvokableEnter(false, '$x', s)
    expect(s.insertToken).not.toHaveBeenCalled()
    expect(s.newSessionWithToken).not.toHaveBeenCalled()
  })

  it('forwards the exact token unchanged (no FE-side rewriting)', () => {
    const s = sinks()
    // Token text is passed through verbatim; backend resolvers do the
    // allowlisting (input validation). The FE never mutates or path-resolves it.
    const raw = '$nested/skill-name_v2'
    resolveInvokableEnter(true, raw, s)()
    expect(s.insertToken).toHaveBeenCalledWith(raw)
  })
})
