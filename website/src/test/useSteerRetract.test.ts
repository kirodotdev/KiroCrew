import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import type { ChatMessage } from '../types'

const retractSteer = vi.fn()
vi.mock('../api/client', () => ({ api: { retractSteer: (...args: unknown[]) => retractSteer(...args) } }))
const revealComposer = vi.fn()
vi.mock('../pages/chat/composerFocus', () => ({ revealComposer: () => revealComposer() }))

const TAKE_BACKS_KEY = 'mc-steer-take-backs'
const storedTakeBacks = () => JSON.parse(localStorage.getItem(TAKE_BACKS_KEY) ?? '{}') as Record<string, unknown>

import {
  __resetSteerTakeBacksForTests,
  useSteerRetract,
  type SteerRetractOptions,
} from '../hooks/useSteerRetract'
import { ApiError } from '../api/apiError'
import { prependQuote } from '../chat-core/composer/messageQuote'
import { DRAFT_MAX_STORE_BYTES, DRAFT_TTL_MS } from '../utils/draftConstants'

const row = (meta: Record<string, unknown>, content = 'read [attached_file 1] /tmp/notes.md'): ChatMessage => ({
  role: 'user',
  content,
  cls: 'msg msg-u',
  ts: '2026-10-23T05:00:00Z',
  meta: { steer: true, mid: 'm-1', steerRetract: true, files: ['/tmp/notes.md'], ...meta },
})

function setup(initial: ChatMessage[], slotRunning = true, slot = 'test', restoreDraft = vi.fn()) {
  const hook = renderHook(
    (props: Partial<SteerRetractOptions>) => useSteerRetract({
      slot, messages: initial, slotRunning, restoreDraft, ...props,
    }),
    { initialProps: {} },
  )
  return { hook, restoreDraft }
}

function installFakeLocks(): () => void {
  const original = Object.getOwnPropertyDescriptor(navigator, 'locks')
  const held = new Set<string>()
  const queued = new Map<string, Array<() => void>>()
  const locks = {
    request: <T>(name: string, callback: () => T | PromiseLike<T>): Promise<T> => new Promise((resolve, reject) => {
      const release = () => {
        const next = queued.get(name)?.shift()
        if (next) next()
        else {
          held.delete(name)
          queued.delete(name)
        }
      }
      const run = () => {
        held.add(name)
        try {
          const result = callback()
          if (result && typeof (result as PromiseLike<T>).then === 'function') {
            void Promise.resolve(result).then(resolve, reject).finally(release)
          } else {
            resolve(result as T)
            release()
          }
        } catch (error) {
          release()
          reject(error)
        }
      }
      if (held.has(name)) {
        const waiters = queued.get(name) ?? []
        waiters.push(run)
        queued.set(name, waiters)
      } else {
        run()
      }
    }),
  }
  Object.defineProperty(navigator, 'locks', { configurable: true, value: locks })
  return () => {
    if (original) Object.defineProperty(navigator, 'locks', original)
    else delete (navigator as { locks?: unknown }).locks
  }
}

function removeWebLocks(): () => void {
  const original = Object.getOwnPropertyDescriptor(navigator, 'locks')
  delete (navigator as { locks?: unknown }).locks
  return () => {
    if (original) Object.defineProperty(navigator, 'locks', original)
    else delete (navigator as { locks?: unknown }).locks
  }
}

let restoreDefaultLocks: (() => void) | undefined

beforeEach(() => {
  restoreDefaultLocks = installFakeLocks()
  localStorage.clear()
  __resetSteerTakeBacksForTests()
  revealComposer.mockReset()
  retractSteer.mockReset()
  retractSteer.mockResolvedValue({ ok: true, pending: true })
})

afterEach(() => {
  restoreDefaultLocks?.()
  restoreDefaultLocks = undefined
  vi.useRealTimers()
})

describe('useSteerRetract', () => {
  it('refuses Edit without Web Locks before storing or sending', () => {
    const restoreLocks = removeWebLocks()
    try {
      const { hook, restoreDraft } = setup([row({ steerState: 'written' })])

      act(() => { hook.result.current.onSteerAction('m-1', 'edit') })

      expect(retractSteer).not.toHaveBeenCalled()
      expect(localStorage.getItem(TAKE_BACKS_KEY)).toBeNull()
      expect(restoreDraft).not.toHaveBeenCalled()
      expect(hook.result.current.pending).toEqual({})
      expect(hook.result.current.refusals).toEqual({ 'm-1': { refusal: 'failed', action: 'edit' } })
      expect(hook.result.current.requestFailures).toEqual({})
    } finally {
      restoreLocks()
    }
  })

  it.each(['unsend', 'queue'] as const)('allows %s without Web Locks', (action) => {
    const restoreLocks = removeWebLocks()
    try {
      const { hook } = setup([row({ steerState: 'written' })])

      act(() => { hook.result.current.onSteerAction('m-1', action) })

      expect(retractSteer).toHaveBeenCalledTimes(1)
      expect(retractSteer).toHaveBeenCalledWith('test', 'm-1', action)
      expect(storedTakeBacks()).toHaveProperty('test')
      expect(hook.result.current.pending).toEqual({ 'm-1': action })
    } finally {
      restoreLocks()
    }
  })

  it('refuses concurrent Edits in different chats without Web Locks', () => {
    const restoreLocks = removeWebLocks()
    try {
      const first = setup([row({ steerState: 'written' }, 'first chat text')], true, 'slot-a')
      const second = setup([row({ mid: 'm-2', steerState: 'written' }, 'second chat text')], true, 'slot-b')

      act(() => {
        first.hook.result.current.onSteerAction('m-1', 'edit')
        second.hook.result.current.onSteerAction('m-2', 'edit')
      })

      expect(first.hook.result.current.refusals).toEqual({ 'm-1': { refusal: 'failed', action: 'edit' } })
      expect(second.hook.result.current.refusals).toEqual({ 'm-2': { refusal: 'failed', action: 'edit' } })
      expect(first.hook.result.current.pending).toEqual({})
      expect(second.hook.result.current.pending).toEqual({})
      expect(retractSteer).not.toHaveBeenCalled()
      expect(localStorage.getItem(TAKE_BACKS_KEY)).toBeNull()
    } finally {
      restoreLocks()
    }
  })
  it('asks the gateway once and marks the row pending', () => {
    retractSteer.mockImplementationOnce(() => {
      expect(storedTakeBacks()).toHaveProperty('test')
      return Promise.resolve({ ok: true, pending: true })
    })
    const { hook } = setup([row({ steerState: 'written' })])

    act(() => { hook.result.current.onSteerAction('m-1', 'edit') })
    act(() => { hook.result.current.onSteerAction('m-1', 'edit') })

    expect(retractSteer).toHaveBeenCalledTimes(1)
    expect(retractSteer).toHaveBeenCalledWith('test', 'm-1', 'edit')
    expect(hook.result.current.pending).toEqual({ 'm-1': 'edit' })
  })

  it('keeps another tab from replacing an outstanding edit recovery', async () => {
    retractSteer
      .mockImplementationOnce(() => new Promise(() => {}))
      .mockRejectedValueOnce(new ApiError(409, 'busy', JSON.stringify({ code: 'steer_retract_busy' })))
    const first = setup([row({ steerState: 'written' }, 'first tab text')])
    const second = setup([row({ mid: 'm-2', steerState: 'written' }, 'second tab text')])

    act(() => { first.hook.result.current.onSteerAction('m-1', 'edit') })
    expect(storedTakeBacks()).toMatchObject({ test: { mid: 'm-1', content: 'first tab text' } })

    // Each browser tab has its own module state but shares localStorage.
    __resetSteerTakeBacksForTests()
    act(() => { second.hook.result.current.onSteerAction('m-2', 'queue') })

    await waitFor(
      () => expect(second.hook.result.current.refusals).toEqual({ 'm-2': { refusal: 'busy', action: 'queue' } }),
      { timeout: 500 },
    )
    expect(retractSteer).toHaveBeenCalledTimes(1)
    expect(storedTakeBacks()).toMatchObject({ test: { mid: 'm-1', content: 'first tab text' } })

    first.hook.unmount()
    second.hook.unmount()
    __resetSteerTakeBacksForTests()
    const reloaded = setup([row({ steerState: 'retracted', steerRetractAction: 'edit' }, '')])

    await waitFor(() => expect(reloaded.restoreDraft).toHaveBeenCalledTimes(1), { timeout: 500 })
    expect(reloaded.restoreDraft.mock.calls[0][0]).toBe('first tab text')
    expect(storedTakeBacks()).not.toHaveProperty('test')
  })

  it.each([
    ['the same steer', 'm-1', 'first tab text'],
    ['a different steer', 'm-2', 'second tab text'],
  ])('serializes concurrent Edit claims for %s across tabs', async (_case, secondMid, secondText) => {
    const restoreLocks = installFakeLocks()
    const first = setup([row({ steerState: 'written' }, 'first tab text')])
    const second = setup([row({ mid: secondMid, steerState: 'written' }, secondText)])
    const originalSetItem = Storage.prototype.setItem
    let triggeredSecondTab = false
    Storage.prototype.setItem = function (key: string, value: string): void {
      if (key === TAKE_BACKS_KEY && !triggeredSecondTab) {
        triggeredSecondTab = true
        // Each browser tab has its own module state but shares localStorage.
        __resetSteerTakeBacksForTests()
        act(() => { second.hook.result.current.onSteerAction(secondMid, 'edit') })
      }
      originalSetItem.call(this, key, value)
    }
    retractSteer.mockImplementation(() => {
      if (retractSteer.mock.calls.length === 1) return new Promise(() => {})
      return Promise.reject(new ApiError(409, 'busy', JSON.stringify({ code: 'steer_retract_busy' })))
    })

    try {
      act(() => { first.hook.result.current.onSteerAction('m-1', 'edit') })

      await waitFor(
        () => expect(second.hook.result.current.refusals).toEqual({ [secondMid]: { refusal: 'busy', action: 'edit' } }),
        { timeout: 500 },
      )
      expect(retractSteer).toHaveBeenCalledTimes(1)
      expect(storedTakeBacks()).toMatchObject({
        test: { mid: 'm-1', action: 'edit', content: 'first tab text' },
      })

      first.hook.unmount()
      second.hook.unmount()
      __resetSteerTakeBacksForTests()
      const reloaded = setup([row({ steerState: 'retracted', steerRetractAction: 'edit' }, '')])

      await waitFor(() => expect(reloaded.restoreDraft).toHaveBeenCalledTimes(1), { timeout: 500 })
      expect(reloaded.restoreDraft.mock.calls[0][0]).toBe('first tab text')
    } finally {
      Storage.prototype.setItem = originalSetItem
      restoreLocks()
    }
  })

  it('serializes concurrent Edit claims for different chats across tabs', async () => {
    const restoreLocks = installFakeLocks()
    const first = setup([row({ steerState: 'written' }, 'first chat text')], true, 'slot-a')
    const second = setup([row({ mid: 'm-2', steerState: 'written' }, 'second chat text')], true, 'slot-b')
    const originalSetItem = Storage.prototype.setItem
    let triggeredSecondTab = false
    Storage.prototype.setItem = function (key: string, value: string): void {
      if (key === TAKE_BACKS_KEY && !triggeredSecondTab) {
        triggeredSecondTab = true
        // Each browser tab has its own module state but shares localStorage.
        __resetSteerTakeBacksForTests()
        act(() => { second.hook.result.current.onSteerAction('m-2', 'edit') })
      }
      originalSetItem.call(this, key, value)
    }
    retractSteer.mockImplementation(() => new Promise(() => {}))

    try {
      act(() => { first.hook.result.current.onSteerAction('m-1', 'edit') })

      await waitFor(() => expect(retractSteer).toHaveBeenCalledTimes(2), { timeout: 500 })
      expect(retractSteer.mock.calls).toEqual(expect.arrayContaining([
        ['slot-a', 'm-1', 'edit'],
        ['slot-b', 'm-2', 'edit'],
      ]))
      expect(storedTakeBacks()).toMatchObject({
        'slot-a': { mid: 'm-1', action: 'edit', content: 'first chat text' },
        'slot-b': { mid: 'm-2', action: 'edit', content: 'second chat text' },
      })

      first.hook.unmount()
      second.hook.unmount()
      __resetSteerTakeBacksForTests()
      const reloadedFirst = setup(
        [row({ steerState: 'retracted', steerRetractAction: 'edit' }, '')],
        true,
        'slot-a',
      )
      await waitFor(() => expect(reloadedFirst.restoreDraft).toHaveBeenCalledTimes(1), { timeout: 500 })
      expect(reloadedFirst.restoreDraft.mock.calls[0][0]).toBe('first chat text')
      await waitFor(() => expect(storedTakeBacks()).not.toHaveProperty('slot-a'), { timeout: 500 })
      reloadedFirst.hook.unmount()

      __resetSteerTakeBacksForTests()
      const reloadedSecond = setup(
        [row({ mid: 'm-2', steerState: 'retracted', steerRetractAction: 'edit' }, '')],
        true,
        'slot-b',
      )
      await waitFor(() => expect(reloadedSecond.restoreDraft).toHaveBeenCalledTimes(1), { timeout: 500 })
      expect(reloadedSecond.restoreDraft.mock.calls[0][0]).toBe('second chat text')
    } finally {
      Storage.prototype.setItem = originalSetItem
      restoreLocks()
    }
  })

  it('keeps oversized Edit recovery records for different chats until each row settles', async () => {
    const largeTextSize = Math.floor(DRAFT_MAX_STORE_BYTES / 2) + 1024
    const firstText = 'a'.repeat(largeTextSize)
    const secondText = 'b'.repeat(largeTextSize)
    retractSteer.mockImplementation(() => new Promise(() => {}))
    const first = setup([row({ steerState: 'written' }, firstText)], true, 'slot-a')
    const second = setup([row({ mid: 'm-2', steerState: 'written' }, secondText)], true, 'slot-b')

    act(() => { first.hook.result.current.onSteerAction('m-1', 'edit') })
    act(() => { second.hook.result.current.onSteerAction('m-2', 'edit') })

    expect(localStorage.getItem(TAKE_BACKS_KEY)?.length).toBeGreaterThan(DRAFT_MAX_STORE_BYTES)
    expect(storedTakeBacks()).toHaveProperty('slot-a')
    expect(storedTakeBacks()).toHaveProperty('slot-b')

    first.hook.unmount()
    second.hook.unmount()
    __resetSteerTakeBacksForTests()
    const reloadedFirst = setup(
      [row({ steerState: 'retracted', steerRetractAction: 'edit' }, '')],
      true,
      'slot-a',
    )
    await waitFor(() => expect(reloadedFirst.restoreDraft).toHaveBeenCalledTimes(1), { timeout: 500 })
    expect(reloadedFirst.restoreDraft.mock.calls[0][0]).toBe(firstText)
    reloadedFirst.hook.unmount()

    __resetSteerTakeBacksForTests()
    const reloadedSecond = setup(
      [row({ mid: 'm-2', steerState: 'retracted', steerRetractAction: 'edit' }, '')],
      true,
      'slot-b',
    )
    await waitFor(() => expect(reloadedSecond.restoreDraft).toHaveBeenCalledTimes(1), { timeout: 500 })
    expect(reloadedSecond.restoreDraft.mock.calls[0][0]).toBe(secondText)
  })

  it('refuses an Edit that does not fit without changing another chat recovery', () => {
    retractSteer.mockImplementation(() => new Promise(() => {}))
    const first = setup([row({ steerState: 'written' }, 'first chat text')], true, 'slot-a')
    act(() => { first.hook.result.current.onSteerAction('m-1', 'edit') })
    const firstStored = localStorage.getItem(TAKE_BACKS_KEY)
    retractSteer.mockClear()

    const originalSetItem = Storage.prototype.setItem
    Storage.prototype.setItem = function (key: string, value: string): void {
      if (key === TAKE_BACKS_KEY) throw new DOMException('full', 'QuotaExceededError')
      originalSetItem.call(this, key, value)
    }
    try {
      const second = setup([row({ mid: 'm-2', steerState: 'written' }, 'second chat text')], true, 'slot-b')
      act(() => { second.hook.result.current.onSteerAction('m-2', 'edit') })

      expect(second.hook.result.current.pending).toEqual({})
      expect(second.hook.result.current.refusals).toEqual({ 'm-2': { refusal: 'failed', action: 'edit' } })
      expect(retractSteer).not.toHaveBeenCalled()
      expect(localStorage.getItem(TAKE_BACKS_KEY)).toBe(firstStored)
    } finally {
      Storage.prototype.setItem = originalSetItem
    }
  })

  it('preserves a fresh Edit recovery after this page cached another tab timestamp', () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    const oldRecovery = {
      slot: 'slot-x', mid: 'm-x', action: 'edit', content: 'old edited text',
      observedRunning: true, requestFailed: false,
    }
    localStorage.setItem(TAKE_BACKS_KEY, JSON.stringify({ 'slot-x': oldRecovery }))
    localStorage.setItem(`${TAKE_BACKS_KEY}-ts`, JSON.stringify({ 'slot-x': Date.now() }))
    __resetSteerTakeBacksForTests()

    vi.setSystemTime(Date.now() + DRAFT_TTL_MS - 1000)
    const cacheLoader = setup([row({ mid: 'm-cache', steerState: 'written' })], true, 'slot-cache')
    act(() => { cacheLoader.hook.result.current.onSteerAction('m-cache', 'queue') })
    cacheLoader.hook.rerender({
      messages: [row({ mid: 'm-cache', steerState: 'retracted', steerRetractAction: 'queue' })],
    })
    cacheLoader.hook.unmount()

    const freshRecovery = { ...oldRecovery, content: 'fresh edited text' }
    const freshStamp = Date.now()
    localStorage.setItem(TAKE_BACKS_KEY, JSON.stringify({ 'slot-x': freshRecovery }))
    localStorage.setItem(`${TAKE_BACKS_KEY}-ts`, JSON.stringify({ 'slot-x': freshStamp }))
    vi.setSystemTime(Date.now() + 2000)

    const otherChat = setup([row({ mid: 'm-y', steerState: 'written' })], true, 'slot-y')
    act(() => { otherChat.hook.result.current.onSteerAction('m-y', 'queue') })
    expect(storedTakeBacks()).toHaveProperty('slot-x')
    expect(JSON.parse(localStorage.getItem(`${TAKE_BACKS_KEY}-ts`) ?? '{}')['slot-x']).toBe(freshStamp)
    otherChat.hook.unmount()

    __resetSteerTakeBacksForTests()
    const reloaded = setup(
      [row({ mid: 'm-x', steerState: 'retracted', steerRetractAction: 'edit' }, '')],
      true,
      'slot-x',
    )
    expect(reloaded.restoreDraft).toHaveBeenCalledTimes(1)
    expect(reloaded.restoreDraft.mock.calls[0][0]).toBe('fresh edited text')
  })

  it('does not rewrite an unstamped recovery for another chat while mounting', () => {
    localStorage.setItem(TAKE_BACKS_KEY, JSON.stringify({
      'slot-b': {
        slot: 'slot-b', mid: 'm-2', action: 'edit', content: 'other chat text',
        observedRunning: true, requestFailed: false,
      },
    }))
    __resetSteerTakeBacksForTests()
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    const removeItem = vi.spyOn(Storage.prototype, 'removeItem')

    try {
      const mounted = setup([row({ steerState: 'written' })], true, 'slot-a')
      const protectedKeys = new Set([TAKE_BACKS_KEY, `${TAKE_BACKS_KEY}-ts`])
      expect(setItem.mock.calls.filter(([key]) => protectedKeys.has(key))).toEqual([])
      expect(removeItem.mock.calls.filter(([key]) => protectedKeys.has(key))).toEqual([])
      expect(storedTakeBacks()).toHaveProperty('slot-b')
      mounted.hook.unmount()
    } finally {
      setItem.mockRestore()
      removeItem.mockRestore()
    }
  })

  it('restores an edited steer after module state is lost and removes its stored recovery', async () => {
    retractSteer.mockImplementationOnce(() => new Promise(() => {}))
    const first = setup([row({ steerState: 'written' })])
    act(() => { first.hook.result.current.onSteerAction('m-1', 'edit') })
    expect(storedTakeBacks()).toHaveProperty('test')
    first.hook.unmount()

    __resetSteerTakeBacksForTests()
    const second = setup([row({ steerState: 'retracted', steerRetractAction: 'edit' })])

    await waitFor(() => expect(second.restoreDraft).toHaveBeenCalledTimes(1), { timeout: 500 })
    expect(second.restoreDraft.mock.calls[0][0]).toBe('read @notes.md')
    expect(second.hook.result.current.pending).toEqual({})
    expect(storedTakeBacks()).not.toHaveProperty('test')
  })

  it.each([
    ['corrupt', '{', null],
    ['expired', JSON.stringify({
      test: {
        slot: 'test', mid: 'm-1', action: 'edit', content: 'lost text',
        observedRunning: true, requestFailed: false,
      },
    }), JSON.stringify({ test: 0 })],
  ])('ignores a %s stored recovery record on mount and cleans it before a locked update', (_kind, stored, timestamps) => {
    localStorage.setItem(TAKE_BACKS_KEY, stored)
    if (timestamps !== null) localStorage.setItem(`${TAKE_BACKS_KEY}-ts`, timestamps)
    __resetSteerTakeBacksForTests()

    const { hook, restoreDraft } = setup([row({ steerState: 'retracted', steerRetractAction: 'edit' })])

    expect(hook.result.current.pending).toEqual({})
    expect(restoreDraft).not.toHaveBeenCalled()
    expect(localStorage.getItem(TAKE_BACKS_KEY)).toBe(stored)

    const removeItem = vi.spyOn(Storage.prototype, 'removeItem')
    act(() => { hook.result.current.onSteerAction('m-1', 'unsend') })
    expect(removeItem).toHaveBeenCalledWith(TAKE_BACKS_KEY)
    removeItem.mockRestore()
  })

  it.each(['unsend', 'queue'] as const)('removes stored recovery after a taken-back %s settles', (action) => {
    const { hook } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', action) })
    expect(storedTakeBacks()).toHaveProperty('test')

    hook.rerender({ messages: [row({ steerState: 'retracted', steerRetractAction: action })] })

    expect(hook.result.current.pending).toEqual({})
    expect(storedTakeBacks()).not.toHaveProperty('test')
  })

  it('does not send Edit when recovery persistence fails, but still sends Unsend and Queue instead', () => {
    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = () => { throw new DOMException('full', 'QuotaExceededError') }
    try {
      const edit = setup([row({ steerState: 'written' })], true, 'edit')
      const unsend = setup([row({ steerState: 'written' })], true, 'unsend')
      const queue = setup([row({ steerState: 'written' })], true, 'queue')

      act(() => { edit.hook.result.current.onSteerAction('m-1', 'edit') })
      act(() => { unsend.hook.result.current.onSteerAction('m-1', 'unsend') })
      act(() => { queue.hook.result.current.onSteerAction('m-1', 'queue') })

      expect(retractSteer.mock.calls).toEqual([
        ['unsend', 'm-1', 'unsend'],
        ['queue', 'm-1', 'queue'],
      ])
      expect(edit.hook.result.current.pending).toEqual({})
      expect(edit.hook.result.current.refusals).toEqual({ 'm-1': { refusal: 'failed', action: 'edit' } })
    } finally {
      Storage.prototype.setItem = orig
    }
  })

  it('keeps recovery after a failed composer save without restoring twice in one page session', async () => {
    retractSteer.mockImplementationOnce(() => new Promise(() => {}))
    const first = setup([row({ steerState: 'written' })])
    act(() => { first.hook.result.current.onSteerAction('m-1', 'edit') })
    first.hook.unmount()

    __resetSteerTakeBacksForTests()
    const failedRestore = vi.fn(() => false)
    const second = setup(
      [row({ steerState: 'retracted', steerRetractAction: 'edit' })],
      true,
      'test',
      failedRestore,
    )
    await waitFor(() => expect(failedRestore).toHaveBeenCalledTimes(1), { timeout: 500 })
    expect(second.hook.result.current.pending).toEqual({})
    expect(storedTakeBacks()).toHaveProperty('test')
    second.hook.unmount()

    const samePageRestore = vi.fn(() => true)
    const samePage = setup(
      [row({ steerState: 'retracted', steerRetractAction: 'edit' })],
      true,
      'test',
      samePageRestore,
    )
    expect(samePageRestore).not.toHaveBeenCalled()
    expect(samePage.hook.result.current.pending).toEqual({})
    samePage.hook.unmount()

    __resetSteerTakeBacksForTests()
    const freshLoadRestore = vi.fn(() => true)
    setup(
      [row({ steerState: 'retracted', steerRetractAction: 'edit' })],
      true,
      'test',
      freshLoadRestore,
    )
    await waitFor(() => expect(freshLoadRestore).toHaveBeenCalledTimes(1), { timeout: 500 })
    expect(storedTakeBacks()).not.toHaveProperty('test')
  })

  it.each(['edit', 'unsend', 'queue'] as const)(
    'preserves a failed Edit recovery when a second %s is requested',
    async (secondAction) => {
      const firstText = 'first steer text'
      const secondText = 'second steer text'
      const failedRestore = vi.fn(() => false)
      const current = setup(
        [row({ steerState: 'written' }, firstText)],
        true,
        'test',
        failedRestore,
      )
      act(() => { current.hook.result.current.onSteerAction('m-1', 'edit') })

      current.hook.rerender({
        messages: [row({ steerState: 'retracted', steerRetractAction: 'edit' }, '')],
      })
      await waitFor(() => expect(failedRestore).toHaveBeenCalledTimes(1), { timeout: 500 })
      expect(storedTakeBacks()).toMatchObject({
        test: { mid: 'm-1', action: 'edit', content: firstText },
      })

      current.hook.rerender({
        messages: [row({ mid: 'm-2', steerState: 'written' }, secondText)],
      })
      act(() => { current.hook.result.current.onSteerAction('m-2', secondAction) })

      expect(current.hook.result.current.pending).toEqual({})
      expect(current.hook.result.current.refusals).toEqual({ 'm-2': { refusal: 'failed', action: secondAction } })
      expect(current.hook.result.current.requestFailures).toEqual({})
      expect(retractSteer).toHaveBeenCalledTimes(1)
      expect(storedTakeBacks()).toMatchObject({
        test: { mid: 'm-1', action: 'edit', content: firstText },
      })

      current.hook.unmount()
      __resetSteerTakeBacksForTests()
      const restored = vi.fn(() => true)
      setup(
        [row({ steerState: 'retracted', steerRetractAction: 'edit' }, '')],
        true,
        'test',
        restored,
      )

      await waitFor(() => expect(restored).toHaveBeenCalledTimes(1), { timeout: 500 })
      expect(restored.mock.calls[0][0]).toBe(firstText)
      expect(storedTakeBacks()).not.toHaveProperty('test')
    },
  )

  it('puts an edited steer back in the composer once its row says it was taken back', () => {
    const { hook, restoreDraft } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', 'edit') })
    expect(restoreDraft).not.toHaveBeenCalled()

    hook.rerender({ messages: [row({ steerState: 'retracted', steerRetractAction: 'edit' })] })

    expect(restoreDraft).toHaveBeenCalledTimes(1)
    const [text, files] = restoreDraft.mock.calls[0]
    // The marker comes back as a mention, spelled by the shortest suffix no other
    // staged file shares, the way a cancelled queue card's does.
    expect(text).toBe('read @notes.md')
    expect(files).toEqual(['/tmp/notes.md'])
    expect(revealComposer).toHaveBeenCalledTimes(1)
    expect(hook.result.current.pending).toEqual({})
  })

  it('keeps an edited steer quote block in the restored draft text', () => {
    const quote = { role: 'assistant' as const, text: 'quoted context', ts: 'quote-ts' }
    const content = prependQuote('reply with this context', quote)
    const { hook, restoreDraft } = setup([
      row({ steerState: 'written', quote }, content),
    ])
    act(() => { hook.result.current.onSteerAction('m-1', 'edit') })

    hook.rerender({
      messages: [row({ steerState: 'retracted', steerRetractAction: 'edit', quote }, content)],
    })

    expect(restoreDraft).toHaveBeenCalledTimes(1)
    expect(restoreDraft.mock.calls[0][0]).toBe(content)
    expect(restoreDraft.mock.calls[0]).toHaveLength(3)
  })

  it.each(['unsend', 'queue'] as const)('leaves the composer alone for a taken-back %s', (action) => {
    const { hook, restoreDraft } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', action) })

    hook.rerender({ messages: [row({ steerState: 'retracted', steerRetractAction: action })] })

    expect(restoreDraft).not.toHaveBeenCalled()
    expect(revealComposer).not.toHaveBeenCalled()
    expect(hook.result.current.pending).toEqual({})
  })

  it('takes nothing back when the turn read the steer first', () => {
    const { hook, restoreDraft } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', 'edit') })

    hook.rerender({ messages: [row({ steerState: 'consumed' })] })

    expect(restoreDraft).not.toHaveBeenCalled()
    expect(hook.result.current.pending).toEqual({})
    expect(hook.result.current.refusals).toEqual({})
  })

  it('reports an agent that refused to take it back', () => {
    const { hook, restoreDraft } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', 'unsend') })

    hook.rerender({ messages: [row({ steerState: 'written', steerRetract: false })] })

    expect(restoreDraft).not.toHaveBeenCalled()
    expect(hook.result.current.refusals).toEqual({ 'm-1': { refusal: 'unsupported', action: 'unsend' } })
  })

  it('records which action a refusal refused, so its notice can name it', async () => {
    const { hook } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', 'edit') })

    hook.rerender({ messages: [row({ steerState: 'written', steerRetract: false })] })

    expect(hook.result.current.refusals).toEqual({ 'm-1': { refusal: 'unsupported', action: 'edit' } })

    retractSteer.mockRejectedValueOnce(new ApiError(409, 'busy', JSON.stringify({ code: 'steer_retract_busy' })))
    hook.rerender({ messages: [row({ steerState: 'written' })] })
    act(() => { hook.result.current.onSteerAction('m-1', 'queue') })
    await waitFor(
      () => expect(hook.result.current.refusals).toEqual({ 'm-1': { refusal: 'busy', action: 'queue' } }),
      { timeout: 500 },
    )
  })

  it.each(['edit', 'queue'] as const)('keeps a refused %s as a refusal, with nothing put in the composer', (action) => {
    const { hook, restoreDraft } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', action) })

    hook.rerender({ messages: [row({ steerState: 'written', steerRetract: false })] })

    expect(restoreDraft).not.toHaveBeenCalled()
    expect(hook.result.current.refusals).toEqual({ 'm-1': { refusal: 'unsupported', action } })
  })

  const expiredOffer = () => row({ steerState: 'written', steerRetract: false, steerRetractExpired: true })

  it.each(['edit', 'queue'] as const)('hands a %s back when a later turn withdrew its stopped turn offer', (action) => {
    const { hook, restoreDraft } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', action) })

    // A hard Stop left the row written, and the next turn, still running, withdrew the offer.
    hook.rerender({ messages: [expiredOffer()] })

    expect(restoreDraft).toHaveBeenCalledTimes(1)
    expect(restoreDraft.mock.calls[0][0]).toBe('read @notes.md')
    expect(hook.result.current.refusals).toEqual({})
    expect(hook.result.current.pending).toEqual({})
    expect(storedTakeBacks()).not.toHaveProperty('test')
  })

  it('settles an unsend whose stopped turn offer a later turn withdrew, with no refusal', () => {
    const { hook, restoreDraft } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', 'unsend') })

    hook.rerender({ messages: [expiredOffer()] })

    expect(restoreDraft).not.toHaveBeenCalled()
    expect(hook.result.current.refusals).toEqual({})
    expect(hook.result.current.pending).toEqual({})
  })

  it('hands an Edit back on returning to its chat after a hard Stop and a newer turn there', () => {
    retractSteer.mockImplementation(() => new Promise(() => {}))
    const { hook, restoreDraft } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', 'edit') })

    hook.rerender({ slot: 'other', messages: [row({ mid: 'm-9', steerState: 'consumed' }, 'elsewhere')] })
    expect(restoreDraft).not.toHaveBeenCalled()

    hook.rerender({ slot: 'test', messages: [expiredOffer()] })

    expect(restoreDraft).toHaveBeenCalledTimes(1)
    expect(hook.result.current.refusals).toEqual({})
    expect(hook.result.current.pending).toEqual({})
  })

  it('hands a stored Edit back after a reload once a later turn withdrew its stopped turn offer', async () => {
    retractSteer.mockImplementationOnce(() => new Promise(() => {}))
    const first = setup([row({ steerState: 'written' })])
    act(() => { first.hook.result.current.onSteerAction('m-1', 'edit') })
    first.hook.unmount()

    __resetSteerTakeBacksForTests()
    const second = setup([expiredOffer()], false)

    await waitFor(() => expect(second.restoreDraft).toHaveBeenCalledTimes(1), { timeout: 500 })
    expect(second.restoreDraft.mock.calls[0][0]).toBe('read @notes.md')
    expect(second.hook.result.current.refusals).toEqual({})
    expect(storedTakeBacks()).not.toHaveProperty('test')
  })

  it.each(['edit', 'queue'] as const)('hands a %s back to the composer when a hard stop discarded the steer', (action) => {
    const { hook, restoreDraft } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', action) })

    hook.rerender({ slotRunning: false })

    expect(restoreDraft).toHaveBeenCalledTimes(1)
    expect(hook.result.current.pending).toEqual({})
  })

  it('drops an unsend a hard stop discarded without touching the composer', () => {
    const { hook, restoreDraft } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', 'unsend') })

    hook.rerender({ slotRunning: false })

    expect(restoreDraft).not.toHaveBeenCalled()
    expect(hook.result.current.pending).toEqual({})
  })

  it('says why the gateway refused, and stays quiet when the steer is simply gone', async () => {
    retractSteer.mockRejectedValueOnce(new ApiError(409, 'busy', JSON.stringify({ code: 'steer_retract_busy' })))
    const { hook } = setup([row({ steerState: 'written' })])

    act(() => { hook.result.current.onSteerAction('m-1', 'unsend') })
    await waitFor(
      () => expect(hook.result.current.refusals).toEqual({ 'm-1': { refusal: 'busy', action: 'unsend' } }),
      { timeout: 500 },
    )
    expect(hook.result.current.pending).toEqual({})
    expect(hook.result.current.requestFailures).toEqual({})

    retractSteer.mockRejectedValueOnce(new ApiError(409, 'gone', JSON.stringify({ code: 'steer_not_pending' })))
    act(() => { hook.result.current.onSteerAction('m-1', 'unsend') })
    await waitFor(() => expect(hook.result.current.pending).toEqual({}), { timeout: 500 })
    expect(hook.result.current.refusals).toEqual({})
    expect(hook.result.current.requestFailures).toEqual({})
  })

  it.each([
    ['a network error', new TypeError('connection lost')],
    ['an HTTP 503', new ApiError(503, 'unavailable')],
  ])('keeps reconciliation pending after %s and exposes the request failure until settlement', async (_name, error) => {
    retractSteer.mockRejectedValueOnce(error)
    const { hook } = setup([row({ steerState: 'written' })])

    act(() => { hook.result.current.onSteerAction('m-1', 'unsend') })

    await waitFor(
      () => expect(hook.result.current.requestFailures).toEqual({ 'm-1': true }),
      { timeout: 500 },
    )
    expect(hook.result.current.pending).toEqual({ 'm-1': 'unsend' })
    expect(hook.result.current.refusals).toEqual({})

    hook.rerender({ messages: [row({ steerState: 'consumed' })] })

    expect(hook.result.current.pending).toEqual({})
    expect(hook.result.current.requestFailures).toEqual({})
  })

  it.each([
    {
      name: 'a transport failure',
      action: 'edit' as const,
      request: () => Promise.reject(new TypeError('connection lost')),
      messages: [row({ steerState: 'retracted', steerRetractAction: 'edit' })],
      slotRunning: true,
    },
    {
      name: 'a server failure',
      action: 'edit' as const,
      request: () => Promise.reject(new ApiError(503, 'unavailable')),
      messages: [row({ steerState: 'retracted', steerRetractAction: 'edit' })],
      slotRunning: true,
    },
    {
      name: 'no answer before a hard stop',
      action: 'queue' as const,
      request: () => new Promise(() => {}),
      messages: [row({ steerState: 'written' })],
      slotRunning: false,
    },
  ])('keeps recovery through $name and a remount', async ({ action, request, messages, slotRunning }) => {
    retractSteer.mockImplementationOnce(request)
    const first = setup([row({ steerState: 'written' })])
    act(() => { first.hook.result.current.onSteerAction('m-1', action) })

    await waitFor(
      () => expect(first.hook.result.current.pending).toEqual({ 'm-1': action }),
      { timeout: 500 },
    )
    first.hook.unmount()

    const second = setup(messages, slotRunning)
    await waitFor(() => expect(second.restoreDraft).toHaveBeenCalledTimes(1), { timeout: 500 })
    expect(second.hook.result.current.pending).toEqual({})
  })

  it('keeps a stored take-back until the reloaded transcript arrives', async () => {
    retractSteer.mockImplementationOnce(() => new Promise(() => {}))
    const first = setup([row({ steerState: 'written' })], true)
    act(() => { first.hook.result.current.onSteerAction('m-1', 'edit') })
    first.hook.unmount()
    __resetSteerTakeBacksForTests()

    const reloaded = setup([], false)

    expect(reloaded.restoreDraft).not.toHaveBeenCalled()
    expect(reloaded.hook.result.current.pending).toEqual({ 'm-1': 'edit' })
    expect(storedTakeBacks()).toHaveProperty('test')

    reloaded.hook.rerender({
      messages: [row({ steerState: 'retracted', steerRetractAction: 'edit' })],
      slotRunning: false,
    })

    await waitFor(() => expect(reloaded.restoreDraft).toHaveBeenCalledTimes(1), { timeout: 500 })
    expect(reloaded.hook.result.current.pending).toEqual({})
    expect(storedTakeBacks()).not.toHaveProperty('test')
  })

  it('waits to observe the slot running before an idle remount can mean a hard stop', async () => {
    retractSteer.mockImplementationOnce(() => new Promise(() => {}))
    const first = setup([row({ steerState: 'written' })], false, 'slot-a')
    act(() => { first.hook.result.current.onSteerAction('m-1', 'edit') })
    first.hook.unmount()
    __resetSteerTakeBacksForTests()

    const otherSlot = setup(
      [row({ steerState: 'retracted', steerRetractAction: 'edit' })],
      true,
      'slot-b',
    )
    expect(otherSlot.restoreDraft).not.toHaveBeenCalled()
    expect(otherSlot.hook.result.current.pending).toEqual({})

    const ownSlot = setup([row({ steerState: 'written' })], false, 'slot-a')
    expect(ownSlot.restoreDraft).not.toHaveBeenCalled()
    expect(ownSlot.hook.result.current.pending).toEqual({ 'm-1': 'edit' })

    ownSlot.hook.rerender({ slotRunning: true })
    expect(ownSlot.restoreDraft).not.toHaveBeenCalled()

    ownSlot.hook.rerender({
      slotRunning: true,
      messages: [row({ steerState: 'retracted', steerRetractAction: 'edit' })],
    })
    await waitFor(() => expect(ownSlot.restoreDraft).toHaveBeenCalledTimes(1), { timeout: 500 })
    expect(otherSlot.restoreDraft).not.toHaveBeenCalled()
    expect(ownSlot.hook.result.current.pending).toEqual({})
  })

  it('keeps a stored edit through unrelated provisional rows until its retracted row loads', () => {
    retractSteer.mockImplementation(() => new Promise(() => {}))
    const { hook, restoreDraft } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', 'edit') })

    const unrelated = row({ mid: 'm-2', steerState: 'written' }, 'another steer')
    hook.rerender({
      messages: [unrelated],
      transcriptAuthoritative: false,
    } as Partial<SteerRetractOptions>)

    expect(restoreDraft).not.toHaveBeenCalled()
    expect(hook.result.current.pending).toEqual({ 'm-1': 'edit' })
    expect(storedTakeBacks()).toHaveProperty('test')

    hook.rerender({
      messages: [row({ steerState: 'retracted', steerRetractAction: 'edit' })],
      transcriptAuthoritative: true,
    } as Partial<SteerRetractOptions>)

    expect(restoreDraft).toHaveBeenCalledTimes(1)
    expect(hook.result.current.pending).toEqual({})
    expect(storedTakeBacks()).not.toHaveProperty('test')
  })

  it('does not restore a written edit when a provisional transcript says the turn ended', () => {
    retractSteer.mockImplementation(() => new Promise(() => {}))
    const { hook, restoreDraft } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', 'edit') })

    hook.rerender({
      messages: [row({ steerState: 'written' })],
      slotRunning: false,
      transcriptAuthoritative: false,
    } as Partial<SteerRetractOptions>)

    expect(restoreDraft).not.toHaveBeenCalled()
    expect(hook.result.current.pending).toEqual({ 'm-1': 'edit' })
    expect(storedTakeBacks()).toHaveProperty('test')
  })

  it('restores a stored edit when its row is outside authoritative history', () => {
    retractSteer.mockImplementation(() => new Promise(() => {}))
    const first = setup([row({ steerState: 'written' })])
    act(() => { first.hook.result.current.onSteerAction('m-1', 'edit') })
    first.hook.unmount()
    __resetSteerTakeBacksForTests()

    const paged = setup([row({ mid: 'm-2', steerState: 'written' }, 'newer steer')])

    expect(paged.restoreDraft).toHaveBeenCalledTimes(1)
    expect(paged.restoreDraft.mock.calls[0][0]).toBe('read @notes.md')
    expect(paged.hook.result.current.pending).toEqual({})
    expect(storedTakeBacks()).not.toHaveProperty('test')
  })

  it('settles a stored unsend without restoring when its row is outside authoritative history', () => {
    retractSteer.mockImplementation(() => new Promise(() => {}))
    const first = setup([row({ steerState: 'written' })])
    act(() => { first.hook.result.current.onSteerAction('m-1', 'unsend') })
    first.hook.unmount()
    __resetSteerTakeBacksForTests()

    const paged = setup([row({ mid: 'm-2', steerState: 'written' }, 'newer steer')])

    expect(paged.restoreDraft).not.toHaveBeenCalled()
    expect(paged.hook.result.current.pending).toEqual({})
    expect(storedTakeBacks()).not.toHaveProperty('test')
  })

  it('settles a missing row without disturbing another slot take-back', () => {
    retractSteer.mockImplementation(() => new Promise(() => {}))
    const slotB = setup([row({ steerState: 'written' })], true, 'slot-b')
    act(() => { slotB.hook.result.current.onSteerAction('m-1', 'queue') })

    const slotA = setup([row({ steerState: 'written' })], true, 'slot-a')
    act(() => { slotA.hook.result.current.onSteerAction('m-1', 'edit') })
    const other = row({ mid: 'm-2', steerState: 'written' }, 'another steer')
    slotA.hook.rerender({ messages: [other] })

    expect(slotA.restoreDraft).toHaveBeenCalledTimes(1)
    expect(slotA.restoreDraft.mock.calls[0][0]).toBe('read @notes.md')
    expect(slotA.hook.result.current.pending).toEqual({})
    expect(slotB.hook.result.current.pending).toEqual({ 'm-1': 'queue' })
    expect(storedTakeBacks()).not.toHaveProperty('slot-a')
    expect(storedTakeBacks()).toHaveProperty('slot-b')

    act(() => { slotA.hook.result.current.onSteerAction('m-2', 'queue') })
    expect(retractSteer).toHaveBeenCalledTimes(3)
    expect(slotA.hook.result.current.pending).toEqual({ 'm-2': 'queue' })
    expect(slotB.hook.result.current.pending).toEqual({ 'm-1': 'queue' })
  })
})
