import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import BuiltinAppRoute from '../apps/BuiltinAppRoute'
import { useAppIdentity, useTrustedAppId, type AppIdentity } from '../app-sdk/identity'


/**
 * What the mounted page saw on each of its renders. The FIRST entry is the
 * assertion that matters: identity must be in place before the page's first
 * render, because that is when its queries go out.
 */
const seen: (AppIdentity | null)[] = []
const seenTrusted: (string | null)[] = []

function Probe() {
  seen.push(useAppIdentity())
  seenTrusted.push(useTrustedAppId())
  return <div data-testid="test-app-page">Test App Content</div>
}

// Mock the registry to avoid loading real page components
vi.mock('../apps/builtinRegistry', async () => {
  // Dynamic import (not require, not a top-level import): vi.mock is hoisted
  // above static imports, so a module-scope `import { lazy }` would risk a TDZ
  // error inside this factory. await import() resolves lazily when the mock runs.
  const { lazy } = await import('react')
  // ONE lazy component for the module's lifetime, as the real registry has: a
  // fresh `lazy()` per call would be permanently cold, and it is the WARM case
  // (a second visit, module already loaded) that tells a render-body publication
  // apart from an effect.
  const component = lazy(() => Promise.resolve({ default: Probe }))
  return {
    getBuiltinApp: (path: string) =>
      path === '/test-app' ? { component, appId: 'test-app-id' } : undefined,
    hasBuiltinComponent: (path: string) => path === '/test-app',
    BUILTIN_COMPONENT_REGISTRY: {},
  }
})


function renderAtPath(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/:builtinApp" element={<BuiltinAppRoute />} />
        <Route path="/chat" element={<div data-testid="chat-page">Chat</div>} />
      </Routes>
    </MemoryRouter>,
  )
}


describe('BuiltinAppRoute', () => {
  it('renders the registered component for a known route', async () => {
    renderAtPath('/test-app')
    const page = await screen.findByTestId('test-app-page')
    expect(page).toBeInTheDocument()
    expect(page.textContent).toBe('Test App Content')
  })

  it('redirects to /chat for unknown routes', () => {
    renderAtPath('/unknown-route')
    expect(screen.getByTestId('chat-page')).toBeInTheDocument()
  })

  it('publishes the app identity before the page renders at all', async () => {
    // Recorded per render rather than read from the final DOM. Publishing from a
    // `useEffect` would still leave the right value on screen at the end — the
    // page would just have rendered and queried once with nothing — so asserting
    // the settled state would pass either way. The FIRST render is the contract.
    seen.length = 0
    seenTrusted.length = 0
    renderAtPath('/test-app')
    await screen.findByTestId('test-app-page')
    expect(seen.length).toBeGreaterThan(0)
    expect(seen[0]).toEqual({ appId: 'test-app-id', origin: 'builtin' })
    expect(seenTrusted[0]).toBe('test-app-id')
    // And it never regresses to absent on a later render.
    expect(seen.every((s) => s?.appId === 'test-app-id')).toBe(true)
  })

  it('publishes it on a repeat visit too, when the module is already loaded', async () => {
    // A second visit within one session is the case the identity ordering has to
    // survive, and the case a cold-only test cannot see. The page module is
    // resolved by now, so React renders it in the SAME pass as the route — no
    // suspension, and therefore no committed parent whose effects could have run
    // first. Identity has to come from the render body to be there.
    const { unmount } = renderAtPath('/test-app')
    await screen.findByTestId('test-app-page')
    unmount()

    seen.length = 0
    seenTrusted.length = 0
    renderAtPath('/test-app')
    await screen.findByTestId('test-app-page')
    expect(seen.length).toBeGreaterThan(0)
    expect(seen[0]).toEqual({ appId: 'test-app-id', origin: 'builtin' })
    expect(seenTrusted[0]).toBe('test-app-id')
  })
})
