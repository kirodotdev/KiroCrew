/**
 * ZoomProvider / useZoomCtx — the context wrapper around useZoom.
 *
 * The contract worth testing is the guard: consuming the context outside a
 * provider must throw rather than silently hand back null and crash later at a
 * property access. `useZoom` itself is mocked — this file is about the plumbing.
 */
import { describe, expect, it, vi } from 'vitest'
import { render, renderHook, screen } from '@testing-library/react'

const zoomValue = vi.hoisted(() => ({ zoom: 42, zoomSupported: true, family: 'mono' }))
vi.mock('./useZoom', () => ({ useZoom: () => zoomValue }))

import { ZoomProvider, useZoomCtx } from './ZoomProvider'

function Consumer() {
  const ctx = useZoomCtx() as unknown as typeof zoomValue
  return <span data-testid="zzq-zoom">{`${ctx.zoom}:${ctx.family}`}</span>
}

describe('ZoomProvider', () => {
  it('publishes the useZoom value to descendants', () => {
    render(
      <ZoomProvider>
        <Consumer />
      </ZoomProvider>,
    )
    expect(screen.getByTestId('zzq-zoom').textContent).toBe('42:mono')
  })

  it('hands descendants the identical object (no copy)', () => {
    let seen: unknown
    function Capture() {
      seen = useZoomCtx()
      return null
    }
    render(
      <ZoomProvider>
        <Capture />
      </ZoomProvider>,
    )
    expect(seen).toBe(zoomValue)
  })
})

describe('useZoomCtx', () => {
  it('throws when used outside a ZoomProvider', () => {
    expect(() => renderHook(() => useZoomCtx())).toThrow(/within ZoomProvider/)
  })
})
