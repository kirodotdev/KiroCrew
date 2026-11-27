import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

// The app registry is a 126 KB gz chunk. Hovering an app entry point starts it
// so the first open does not wait on it, and repeat hovers must not refetch.
const loads = vi.hoisted(() => ({ count: 0 }))
vi.mock('../app-sdk/shared-modules', () => {
  loads.count += 1
  return {}
})
vi.mock('../components/AppIcon', () => ({ default: () => null }))

import { SessionControlChips } from '../components/chat-input/ContextShelf'
import { isSpeculativeAppRegistryChunkError, loadAppRegistry } from '../components/appRegistryPrefetch'

const chunkError = new Error('Failed to fetch dynamically imported module: http://x/assets/shared-modules-AbC123.js')

// A dynamic import can take seconds on a loaded runner under coverage.
const LAZY_IMPORT_TIMEOUT = { timeout: 5000 }

describe('app registry prefetch', () => {
  it('starts on hover of a session-control chip, exactly once', async () => {
    render(
      <SessionControlChips
        sessionControls={[{ key: 'demo:ctl', label: 'Demo control' }]}
        shelfCompact={false}
        onSessionControlClick={() => {}}
      />,
    )
    expect(loads.count).toBe(0)
    const chip = screen.getByRole('button', { name: 'Demo control' })
    expect(isSpeculativeAppRegistryChunkError(chunkError)).toBe(false) // nothing in flight
    fireEvent.pointerEnter(chip)
    // While only the hover wants the chunk, its failure must not reload the
    // page (main.tsx skips the stale-chunk reload); another chunk still does.
    expect(isSpeculativeAppRegistryChunkError(chunkError)).toBe(true)
    expect(isSpeculativeAppRegistryChunkError(new Error('Failed to fetch dynamically imported module: http://x/assets/AppPage-Zz9.js'))).toBe(false)
    // The hover alone starts the dynamic import('../app-sdk/shared-modules');
    // nothing else has asked yet. The mocked factory runs once it resolves.
    await vi.waitFor(() => expect(loads.count).toBe(1), LAZY_IMPORT_TIMEOUT)
    const first = loadAppRegistry()
    // Once an open is waiting, or nothing is in flight, a failure reloads as usual.
    expect(isSpeculativeAppRegistryChunkError(chunkError)).toBe(false)
    fireEvent.pointerEnter(chip)
    fireEvent.focus(chip)
    fireEvent.pointerDown(chip)
    // Every later hover, focus, press and open joins the same load.
    expect(loadAppRegistry()).toBe(first)
    await first
    expect(loads.count).toBe(1)
  })

  it('treats a startup mount (demanded: false) as speculative, like a hover', async () => {
    vi.resetModules()
    const fresh = await import('../components/appRegistryPrefetch')
    const p = fresh.loadAppRegistry({ demanded: false })
    expect(fresh.isSpeculativeAppRegistryChunkError(chunkError)).toBe(true)
    fresh.loadAppRegistry() // an app the user opens joins it and may reload
    expect(fresh.isSpeculativeAppRegistryChunkError(chunkError)).toBe(false)
    await p
  })
})
