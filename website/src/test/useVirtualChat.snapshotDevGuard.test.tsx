// Feature: chat-virtualizer — the `window.__vcSnapshot()` debug probe is a DEV
// affordance and must not be installed by a production build.
//
// It was guarded only by `typeof window === 'undefined'`, an SSR check, so it
// shipped in every release. Called, it console.logs the session id and the
// transcript's shape, which a production build has no reason to carry.
//
// `import.meta.env.DEV` is TRUE under vitest, so the production branch is
// reachable here only by stubbing it — which is exactly what these tests do.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render } from '@testing-library/react'
import { useRef, type RefObject } from 'react'

import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'

interface Item { id: string }
const getKey = (it: Item) => it.id
const items: Item[] = Array.from({ length: 4 }, (_, i) => ({ id: `m${i}` }))

function Harness() {
  const scrollerRef = useRef<HTMLDivElement>(null)
  const virt = useVirtualChat<Item>({
    items,
    sessionId: 'snapshot-dev-guard',
    getKey,
    externalScrollerRef: scrollerRef as RefObject<HTMLDivElement | null>,
  })
  return (
    <div ref={scrollerRef}>
      {virt.virtualItems.map((vi) =>
        vi.mounted ? <div key={vi.key} ref={virt.measureRef(vi.index)} /> : null,
      )}
    </div>
  )
}

/** The probe's global, read without asserting its type. */
const probe = () => (window as unknown as { __vcSnapshot?: () => unknown }).__vcSnapshot

describe('useVirtualChat: __vcSnapshot dev guard', () => {
  beforeEach(() => {
    localStorage.clear()
    delete (window as unknown as { __vcSnapshot?: () => unknown }).__vcSnapshot
  })

  afterEach(() => {
    vi.unstubAllEnvs()
    delete (window as unknown as { __vcSnapshot?: () => unknown }).__vcSnapshot
  })

  it('does not install the probe in a production build', () => {
    vi.stubEnv('DEV', false)

    render(<Harness />)

    expect(probe()).toBeUndefined()
  })

  it('installs the probe in a dev build', () => {
    // The other half of the guard: the affordance still exists where it is
    // meant to, so the fix removed a production leak rather than the feature.
    vi.stubEnv('DEV', true)

    render(<Harness />)

    expect(typeof probe()).toBe('function')
  })

  it('removes the probe on unmount', () => {
    vi.stubEnv('DEV', true)

    const { unmount } = render(<Harness />)
    expect(typeof probe()).toBe('function')

    unmount()
    expect(probe()).toBeUndefined()
  })
})
