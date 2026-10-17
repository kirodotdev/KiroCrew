import { act, renderHook } from '@testing-library/react'
import { useLayoutEffect, useRef, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import {
  __createComposerSyncForTests,
  __resetComposerSendHoldsForTests,
  cancelComposerUploads,
  finishComposerAttachment,
  holdComposerSend,
  isComposerSendHeld,
  registerComposerUpload,
  releaseComposerSend,
  unregisterComposerUpload,
  useComposerArrivals,
  useComposerSendHeld,
  useComposerUploadCount,
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

  it('dedupes landed paths and clears them when taken', () => {
    const sync = __createComposerSyncForTests()
    sync.landComposerAttachments('take', ['/up/a.txt', '/up/a.txt', '/up/b.txt'])
    sync.landComposerAttachments('take', ['/up/b.txt', '/up/c.txt'])
    expect(sync.takeComposerArrivals('take')).toEqual(['/up/a.txt', '/up/b.txt', '/up/c.txt'])
    expect(sync.takeComposerArrivals('take')).toEqual([])
    sync.close()
  })

  it('drains on mount and on each arrival', () => {
    const onArrive = vi.fn()
    holdComposerSend('drain')
    finishComposerAttachment('drain', ['/up/before.txt'])
    renderHook(() => useComposerArrivals('drain', onArrive))
    expect(onArrive).toHaveBeenCalledWith(['/up/before.txt'], 'drain')

    act(() => {
      holdComposerSend('drain')
      finishComposerAttachment('drain', ['/up/after.txt'])
    })
    expect(onArrive).toHaveBeenLastCalledWith(['/up/after.txt'], 'drain')
    expect(onArrive).toHaveBeenCalledTimes(2)
  })

  it('leaves a layout-phase arrival with its slot across a host switch', () => {
    const slotA = 'layout-arrival-a'
    const slotB = 'layout-arrival-b'
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
          if (settle) finishComposerAttachment(slotA, ['/up/layout.txt'])
        }, [settle])
        return files
      },
      { initialProps: { slot: slotA, settle: false } },
    )

    view.rerender({ slot: slotB, settle: true })
    expect(view.result.current[slotB]).toBeUndefined()

    view.rerender({ slot: slotA, settle: false })
    expect(view.result.current[slotA]).toEqual(['/up/layout.txt'])
  })

  it('leaves arrivals queued while disabled and drains when enabled', () => {
    const onArrive = vi.fn()
    const { rerender } = renderHook(
      ({ enabled }) => useComposerArrivals('disabled', onArrive, enabled),
      { initialProps: { enabled: false } },
    )
    act(() => {
      holdComposerSend('disabled')
      finishComposerAttachment('disabled', ['/up/waiting.txt'])
    })
    expect(onArrive).not.toHaveBeenCalled()

    rerender({ enabled: true })
    expect(onArrive).toHaveBeenCalledWith(['/up/waiting.txt'], 'disabled')
  })
})

describe('composerSendHolds: live upload controllers', () => {
  it('is cancellable while at least one controller is registered', () => {
    const a = new AbortController()
    const b = new AbortController()
    const { result } = renderHook(() => useComposerUploadCount('live') > 0)
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
    const { result } = renderHook(() => useComposerUploadCount('cancel-me') > 0)
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
    const { result } = renderHook(() => useComposerUploadCount(null) > 0)
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
      return useComposerUploadCount('hooked') > 0
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

  // #13071: the cancel label names how many uploads one press stops, so the
  // count is state that re-renders as a second upload starts and each settles.
  it('reports the live controller count for its slot only', () => {
    const a = new AbortController()
    const b = new AbortController()
    const other = new AbortController()
    const { result } = renderHook(() => useComposerUploadCount('counted-live'))
    expect(result.current).toBe(0)
    act(() => registerComposerUpload('counted-live', a))
    expect(result.current).toBe(1)
    act(() => {
      registerComposerUpload('counted-live', b)
      registerComposerUpload('counted-other', other)
    })
    expect(result.current).toBe(2)
    act(() => unregisterComposerUpload('counted-live', a))
    expect(result.current).toBe(1)
    act(() => {
      unregisterComposerUpload('counted-live', b)
      unregisterComposerUpload('counted-other', other)
    })
    expect(result.current).toBe(0)
  })

  it('counts zero for a null slot', () => {
    const { result } = renderHook(() => useComposerUploadCount(null))
    registerComposerUpload(null, new AbortController())
    expect(result.current).toBe(0)
  })

  it('keeps the hold count independent of the controllers', () => {
    const c = new AbortController()
    const { result } = renderHook(() => useComposerUploadCount('split') > 0)
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


describe('composerSendHolds: quota-safe arrivals', () => {
  it('keeps an arrival in memory when the sessionStorage mirror rejects it', () => {
    __resetComposerSendHoldsForTests()
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('quota', 'QuotaExceededError')
    })
    const onArrive = vi.fn()
    const view = renderHook(() => useComposerArrivals('quota-slot', onArrive))

    act(() => {
      holdComposerSend('quota-slot')
      finishComposerAttachment('quota-slot', ['/up/quota.txt'])
    })

    expect(onArrive).toHaveBeenCalledWith(['/up/quota.txt'], 'quota-slot')
    view.unmount()
    setItem.mockRestore()
    __resetComposerSendHoldsForTests()
  })
})

describe('composerSendHolds: independent windows', () => {
  const WINDOW_ID = 'mc-composer-window-id'
  const openerWithId = (id: string) => ({
    closed: false,
    sessionStorage: { getItem: (key: string) => (key === WINDOW_ID ? id : null) },
  })

  it('does not seed a popout from cloned session storage', () => {
    // A popout's first load: its storage is a copy of the opener's, id included.
    sessionStorage.setItem(WINDOW_ID, 'opener-window')
    sessionStorage.setItem('mc-composer-arrivals', JSON.stringify({ shared: ['/up/cloned.txt'] }))
    vi.stubGlobal('opener', openerWithId('opener-window'))
    const popout = __createComposerSyncForTests()
    expect(popout.takeComposerArrivals('shared')).toEqual([])
    // The popout dropped the clone from its own storage copy and took its own
    // id, so a later load of it cannot resurrect files the opener has sent.
    expect(sessionStorage.getItem('mc-composer-arrivals')).toBeNull()
    expect(sessionStorage.getItem(WINDOW_ID)).not.toBe('opener-window')

    vi.stubGlobal('opener', null)
    sessionStorage.setItem('mc-composer-arrivals', JSON.stringify({ shared: ['/up/primary.txt'] }))
    const primary = __createComposerSyncForTests()
    expect(primary.takeComposerArrivals('shared')).toEqual(['/up/primary.txt'])
    vi.unstubAllGlobals()
    popout.close()
    primary.close()
  })

  it('hydrates a popout\'s own arrivals on any later load, not only a reload', () => {
    sessionStorage.setItem(WINDOW_ID, 'opener-window')
    vi.stubGlobal('opener', openerWithId('opener-window'))
    const first = __createComposerSyncForTests()
    first.landComposerAttachments('mine', ['/up/own.txt'])
    first.close()

    // An ordinary full-page navigation inside the popout: the opener is still
    // there, and this window's stored arrivals are its own.
    const next = __createComposerSyncForTests()
    expect(next.takeComposerArrivals('mine')).toEqual(['/up/own.txt'])
    vi.unstubAllGlobals()
    next.close()
  })

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
