import { act, renderHook } from '@testing-library/react'
import { useLayoutEffect, useRef, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import {
  __createComposerSyncForTests,
  cancelComposerUploads,
  finishComposerAttachment,
  holdComposerSend,
  isComposerSendHeld,
  registerComposerUpload,
  releaseComposerSend,
  unregisterComposerUpload,
  useComposerArrivals,
  useComposerSendHeld,
  useComposerUploadCancellable,
} from '../utils/composerSendHolds'

describe('composerSendHolds', () => {
  it('counts holds and never drops below zero', () => {
    holdComposerSend('counted')
    holdComposerSend('counted')
    expect(isComposerSendHeld('counted')).toBe(true)

    releaseComposerSend('counted')
    expect(isComposerSendHeld('counted')).toBe(true)
    releaseComposerSend('counted')
    releaseComposerSend('counted')
    expect(isComposerSendHeld('counted')).toBe(false)
  })

  it('treats null and empty slots as no-ops', () => {
    holdComposerSend(null)
    holdComposerSend('')
    releaseComposerSend(undefined)
    expect(isComposerSendHeld(null)).toBe(false)
    expect(isComposerSendHeld('')).toBe(false)
  })

  it('notifies hook subscribers when a hold changes', () => {
    const render = vi.fn()
    const { result } = renderHook(() => {
      render()
      return useComposerSendHeld('subscribed')
    })
    expect(result.current).toBe(false)

    act(() => holdComposerSend('subscribed'))
    expect(result.current).toBe(true)
    act(() => releaseComposerSend('subscribed'))
    expect(result.current).toBe(false)
    expect(render).toHaveBeenCalledTimes(3)
  })
})


describe('composerSendHolds: attachment arrivals', () => {
  it('lands paths before releasing the slot hold', () => {
    const seen: Array<{ paths: string[]; held: boolean }> = []
    renderHook(() => useComposerArrivals('ordered', paths => {
      seen.push({ paths, held: isComposerSendHeld('ordered') })
    }))
    holdComposerSend('ordered')

    act(() => finishComposerAttachment('ordered', ['/up/a.txt']))

    expect(seen).toEqual([{ paths: ['/up/a.txt'], held: true }])
    expect(isComposerSendHeld('ordered')).toBe(false)
  })

  it('dedupes the paths of one arrival', () => {
    const onArrive = vi.fn()
    renderHook(() => useComposerArrivals('dedupe', onArrive))
    act(() => finishComposerAttachment('dedupe', ['/up/a.txt', '/up/a.txt', '/up/b.txt']))
    expect(onArrive).toHaveBeenCalledWith(['/up/a.txt', '/up/b.txt'], 'dedupe')
  })

  it('parks an arrival that no composer is showing in the slot draft', () => {
    const park = vi.fn()
    holdComposerSend('nobody-home')

    finishComposerAttachment('nobody-home', ['/up/parked.txt'], park)

    expect(park).toHaveBeenCalledWith(['/up/parked.txt'], 'nobody-home')
    expect(isComposerSendHeld('nobody-home')).toBe(false)
  })

  it('keeps nothing in memory, so a composer mounted later is not handed a parked file twice', () => {
    const park = vi.fn()
    finishComposerAttachment('later', ['/up/once.txt'], park)
    const onArrive = vi.fn()
    renderHook(() => useComposerArrivals('later', onArrive))
    expect(park).toHaveBeenCalledTimes(1)
    expect(onArrive).not.toHaveBeenCalled()
  })

  it('parks a layout-phase arrival in its own slot across a host switch', () => {
    const slotA = 'layout-arrival-a'
    const slotB = 'layout-arrival-b'
    const park = vi.fn()
    holdComposerSend(slotA)

    const view = renderHook(
      ({ slot, settle }) => {
        const shownSlotRef = useRef(slot)
        shownSlotRef.current = slot
        const [files, setFiles] = useState<Record<string, string[]>>({})
        useComposerArrivals(slot, (paths, arrivalSlot) => {
          if (arrivalSlot !== shownSlotRef.current) return false
          setFiles(previous => ({
            ...previous,
            [arrivalSlot]: [...(previous[arrivalSlot] ?? []), ...paths],
          }))
          return true
        })
        useLayoutEffect(() => {
          if (settle) finishComposerAttachment(slotA, ['/up/layout.txt'], park)
        }, [settle])
        return files
      },
      { initialProps: { slot: slotA, settle: false } },
    )

    view.rerender({ slot: slotB, settle: true })

    expect(view.result.current[slotB]).toBeUndefined()
    expect(park).toHaveBeenCalledWith(['/up/layout.txt'], slotA)
  })

  it('parks while the composer is disabled', () => {
    const onArrive = vi.fn()
    const park = vi.fn()
    renderHook(() => useComposerArrivals('disabled', onArrive, false))
    act(() => {
      holdComposerSend('disabled')
      finishComposerAttachment('disabled', ['/up/waiting.txt'], park)
    })
    expect(onArrive).not.toHaveBeenCalled()
    expect(park).toHaveBeenCalledWith(['/up/waiting.txt'], 'disabled')
  })
})

describe('composerSendHolds: live upload controllers', () => {
  it('is cancellable while at least one controller is registered', () => {
    const a = new AbortController()
    const b = new AbortController()
    const { result } = renderHook(() => useComposerUploadCancellable('live'))
    expect(result.current).toBe(false)
    act(() => {
      registerComposerUpload('live', a)
      registerComposerUpload('live', b)
    })
    expect(result.current).toBe(true)

    act(() => unregisterComposerUpload('live', a))
    expect(result.current).toBe(true)
    act(() => unregisterComposerUpload('live', b))
    expect(result.current).toBe(false)
    // Unregistering something never registered is a no-op.
    unregisterComposerUpload('live', a)
    expect(result.current).toBe(false)
  })

  it('aborts every controller of the slot and only that slot', () => {
    const a = new AbortController()
    const b = new AbortController()
    const other = new AbortController()
    const { result } = renderHook(() => useComposerUploadCancellable('cancel-me'))
    act(() => {
      registerComposerUpload('cancel-me', a)
      registerComposerUpload('cancel-me', b)
      registerComposerUpload('leave-me', other)
    })

    cancelComposerUploads('cancel-me')
    expect([a.signal.aborted, b.signal.aborted, other.signal.aborted]).toEqual([true, true, false])
    // Aborting is a request to end: the host unregisters once the fetch
    // rejects, so the slot stays cancellable until then.
    expect(result.current).toBe(true)

    act(() => {
      unregisterComposerUpload('cancel-me', a)
      unregisterComposerUpload('cancel-me', b)
      unregisterComposerUpload('leave-me', other)
    })
  })

  it('treats null and empty slots as no-ops', () => {
    const c = new AbortController()
    const { result } = renderHook(() => useComposerUploadCancellable(null))
    registerComposerUpload(null, c)
    registerComposerUpload('', c)
    cancelComposerUploads(undefined)
    unregisterComposerUpload(null, c)
    expect(c.signal.aborted).toBe(false)
    expect(result.current).toBe(false)
  })

  it('notifies hook subscribers on register and unregister, not on cancel', () => {
    const c = new AbortController()
    const render = vi.fn()
    const { result } = renderHook(() => {
      render()
      return useComposerUploadCancellable('hooked')
    })
    expect(result.current).toBe(false)

    act(() => registerComposerUpload('hooked', c))
    expect(result.current).toBe(true)
    act(() => cancelComposerUploads('hooked'))
    expect(c.signal.aborted).toBe(true)
    expect(result.current).toBe(true)
    act(() => unregisterComposerUpload('hooked', c))
    expect(result.current).toBe(false)
    expect(render).toHaveBeenCalledTimes(3)
  })

  it('keeps the hold count independent of the controllers', () => {
    const c = new AbortController()
    const { result } = renderHook(() => useComposerUploadCancellable('split'))
    holdComposerSend('split')
    expect(isComposerSendHeld('split')).toBe(true)
    expect(result.current).toBe(false)

    act(() => registerComposerUpload('split', c))
    expect(result.current).toBe(true)
    act(() => unregisterComposerUpload('split', c))
    expect(result.current).toBe(false)
    expect(isComposerSendHeld('split')).toBe(true)
    releaseComposerSend('split')
    expect(isComposerSendHeld('split')).toBe(false)
  })
})

describe('composerSendHolds: independent windows', () => {

  it('keeps upload cancellation local to the producing window', () => {
    const owner = __createComposerSyncForTests()
    const popout = __createComposerSyncForTests()
    const controller = new AbortController()
    owner.registerComposerUpload('shared', controller)

    popout.cancelComposerUploads('shared')
    expect(controller.signal.aborted).toBe(false)
    owner.cancelComposerUploads('shared')
    expect(controller.signal.aborted).toBe(true)

    owner.close()
    popout.close()
  })
})

it('keeps holds and arrivals in the window that started the upload', () => {
  const owner = __createComposerSyncForTests()
  const popout = __createComposerSyncForTests()
  const ownerArrivals = vi.fn()
  const popoutArrivals = vi.fn()
  const stopOwner = owner.subscribeComposerArrivals('shared', ownerArrivals)
  const stopPopout = popout.subscribeComposerArrivals('shared', popoutArrivals)

  owner.holdComposerSend('shared')

  expect(owner.isComposerSendHeld('shared')).toBe(true)
  expect(popout.isComposerSendHeld('shared')).toBe(false)

  owner.finishComposerAttachment('shared', ['/up/owner-only.txt'])

  expect(ownerArrivals).toHaveBeenCalledWith(['/up/owner-only.txt'], 'shared')
  expect(popoutArrivals).not.toHaveBeenCalled()
  expect(owner.isComposerSendHeld('shared')).toBe(false)
  expect(popout.isComposerSendHeld('shared')).toBe(false)

  stopOwner()
  stopPopout()
  owner.close()
  popout.close()
})
