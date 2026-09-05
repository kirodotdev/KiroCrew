import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { renderHook } from '@testing-library/react'
import * as React from 'react'
import { useIsCoarsePointer } from '../hooks/useIsCoarsePointer'
import { PhoneSubContentDiv, PhoneSubTriggerDiv, usePhoneSubState } from '../components/ui/phoneSubmenu'

/** Flippable matchMedia stub mirroring useIsTouchDevice.test.ts. */
function stubMatchMedia(initial: Record<string, boolean>) {
  const state = { ...initial }
  const listeners = new Map<string, Set<() => void>>()
  const original = window.matchMedia
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      get matches() {
        return state[query] ?? false
      },
      media: query,
      addEventListener: (_: string, cb: () => void) => {
        const s = listeners.get(query) ?? new Set()
        s.add(cb)
        listeners.set(query, s)
      },
      removeEventListener: (_: string, cb: () => void) => {
        listeners.get(query)?.delete(cb)
      },
      addListener: (cb: () => void) => {
        const s = listeners.get(query) ?? new Set()
        s.add(cb)
        listeners.set(query, s)
      },
      removeListener: (cb: () => void) => {
        listeners.get(query)?.delete(cb)
      },
      dispatchEvent: () => false,
    }),
  })
  return {
    set(query: string, value: boolean) {
      state[query] = value
      for (const cb of listeners.get(query) ?? []) (cb as (e: unknown) => void)({ matches: value })
    },
    restore: () => Object.defineProperty(window, 'matchMedia', { writable: true, value: original }),
  }
}

const COARSE = '(pointer: coarse)'

let mm: ReturnType<typeof stubMatchMedia> | null = null
afterEach(() => {
  mm?.restore()
  mm = null
})

describe('useIsCoarsePointer', () => {
  it('is false on a fine pointer', () => {
    mm = stubMatchMedia({ [COARSE]: false })
    const { result } = renderHook(() => useIsCoarsePointer())
    expect(result.current).toBe(false)
  })

  it('is true on a coarse pointer', () => {
    mm = stubMatchMedia({ [COARSE]: true })
    const { result } = renderHook(() => useIsCoarsePointer())
    expect(result.current).toBe(true)
  })

  it('re-renders when the pointer kind changes', () => {
    mm = stubMatchMedia({ [COARSE]: false })
    const { result } = renderHook(() => useIsCoarsePointer())
    expect(result.current).toBe(false)
    act(() => {
      mm!.set(COARSE, true)
    })
    expect(result.current).toBe(true)
  })

  it('reports false when matchMedia is unavailable', () => {
    const original = window.matchMedia
    Object.defineProperty(window, 'matchMedia', { writable: true, configurable: true, value: undefined })
    const { result } = renderHook(() => useIsCoarsePointer())
    expect(result.current).toBe(false)
    Object.defineProperty(window, 'matchMedia', { writable: true, value: original })
  })
})

describe('usePhoneSubState', () => {
  it('starts collapsed and toggles uncontrolled', () => {
    const onOpenChange = vi.fn()
    const { result } = renderHook(() => usePhoneSubState(undefined, undefined, onOpenChange))
    expect(result.current.expanded).toBe(false)
    act(() => {
      result.current.toggle()
    })
    expect(result.current.expanded).toBe(true)
    expect(onOpenChange).toHaveBeenCalledWith(true)
    act(() => {
      result.current.toggle()
    })
    expect(result.current.expanded).toBe(false)
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('honours defaultOpen for the initial state', () => {
    const { result } = renderHook(() => usePhoneSubState(undefined, true, undefined))
    expect(result.current.expanded).toBe(true)
  })

  it('follows the controlled open prop and still notifies', () => {
    const onOpenChange = vi.fn()
    const { result, rerender } = renderHook(({ open }: { open: boolean }) => usePhoneSubState(open, undefined, onOpenChange), {
      initialProps: { open: true },
    })
    expect(result.current.expanded).toBe(true)
    act(() => {
      result.current.toggle()
    })
    // Controlled: local state does not move, but the parent is notified.
    expect(result.current.expanded).toBe(true)
    expect(onOpenChange).toHaveBeenCalledWith(false)
    rerender({ open: false })
    expect(result.current.expanded).toBe(false)
  })
})

describe('PhoneSubTriggerDiv', () => {
  it('composes a caller onClick with the toggle', () => {
    const onToggle = vi.fn()
    const onClick = vi.fn()
    render(
      <PhoneSubTriggerDiv expanded={false} onToggle={onToggle} onClick={onClick}>
        <span>Recent</span>
      </PhoneSubTriggerDiv>,
    )
    const row = screen.getByRole('button', { name: /Recent/ })
    expect(row).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(row)
    expect(onClick).toHaveBeenCalledTimes(1)
    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  it('toggles on Enter and Space but not other keys', () => {
    const onToggle = vi.fn()
    render(
      <PhoneSubTriggerDiv expanded={false} onToggle={onToggle}>
        <span>Recent</span>
      </PhoneSubTriggerDiv>,
    )
    const row = screen.getByRole('button', { name: /Recent/ })
    fireEvent.keyDown(row, { key: 'Enter' })
    expect(onToggle).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(row, { key: ' ' })
    expect(onToggle).toHaveBeenCalledTimes(2)
    fireEvent.keyDown(row, { key: 'ArrowRight' })
    expect(onToggle).toHaveBeenCalledTimes(2)
  })

  it('reflects the expanded state', () => {
    const { rerender } = render(
      <PhoneSubTriggerDiv expanded={false} onToggle={() => {}}>
        <span>Recent</span>
      </PhoneSubTriggerDiv>,
    )
    expect(screen.getByRole('button', { name: /Recent/ })).toHaveAttribute('aria-expanded', 'false')
    rerender(
      <PhoneSubTriggerDiv expanded onToggle={() => {}}>
        <span>Recent</span>
      </PhoneSubTriggerDiv>,
    )
    expect(screen.getByRole('button', { name: /Recent/ })).toHaveAttribute('aria-expanded', 'true')
  })
})

describe('PhoneSubContentDiv', () => {
  it('renders children inline', () => {
    render(
      <PhoneSubContentDiv>
        <span>picker</span>
      </PhoneSubContentDiv>,
    )
    expect(screen.getByText('picker')).toBeTruthy()
  })
})
