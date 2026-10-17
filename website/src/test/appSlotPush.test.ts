/**
 * The dashboard socket's per-slot change signal for app embeds: an app is
 * signalled only for slots it owns, the signal carries nothing, ownership is
 * read at delivery, and connectivity reaches every watcher.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  createAppSlotPush,
  noteAppSlotFrame,
  resetAppSlotPushForTests,
  setAppSlotPushConnected,
} from '../utils/appSlotPush'

const owners = new Map<string, string>()

function watcher() {
  return { onChange: vi.fn(), onConnection: vi.fn() }
}

beforeEach(() => {
  owners.clear()
  resetAppSlotPushForTests((key) => owners.get(key) ?? null)
})

afterEach(() => {
  resetAppSlotPushForTests()
})

describe('appSlotPush', () => {
  it('signals a slot change to the app that owns the slot, with no payload', () => {
    owners.set('s1', 'spec-builder')
    const w = watcher()
    createAppSlotPush('spec-builder').watch('s1', w)
    noteAppSlotFrame('chat_chunk', { slot: 's1', content: 'secret text' })
    expect(w.onChange).toHaveBeenCalledTimes(1)
    expect(w.onChange).toHaveBeenCalledWith()
  })

  it('does not signal an app for a slot another app owns', () => {
    owners.set('s1', 'other-app')
    const w = watcher()
    createAppSlotPush('spec-builder').watch('s1', w)
    noteAppSlotFrame('chat_message', { slot: 's1' })
    expect(w.onChange).not.toHaveBeenCalled()
  })

  it('does not signal any app for an unowned or unlisted slot', () => {
    const w = watcher()
    createAppSlotPush('spec-builder').watch('s1', w)
    noteAppSlotFrame('chat_message', { slot: 's1' })
    expect(w.onChange).not.toHaveBeenCalled()
  })

  it('reads ownership at delivery, so a slot that changes hands stops signalling', () => {
    owners.set('s1', 'spec-builder')
    const w = watcher()
    createAppSlotPush('spec-builder').watch('s1', w)
    noteAppSlotFrame('chat_done', { slot: 's1' })
    owners.set('s1', 'other-app')
    noteAppSlotFrame('chat_done', { slot: 's1' })
    expect(w.onChange).toHaveBeenCalledTimes(1)
  })

  it('ignores frames that do not change the slot read, and frames for other slots', () => {
    owners.set('s1', 'spec-builder')
    owners.set('s2', 'spec-builder')
    const w = watcher()
    createAppSlotPush('spec-builder').watch('s1', w)
    noteAppSlotFrame('voice_chunk', { slot: 's1' })
    noteAppSlotFrame('subagent_spawn', { slot: 's1' })
    noteAppSlotFrame('chat_chunk', { slot: 's2' })
    noteAppSlotFrame('chat_chunk', null)
    noteAppSlotFrame('chat_chunk', { slot: 42 })
    expect(w.onChange).not.toHaveBeenCalled()
  })

  it('stops signalling once unwatched', () => {
    owners.set('s1', 'spec-builder')
    const w = watcher()
    const off = createAppSlotPush('spec-builder').watch('s1', w)
    off()
    noteAppSlotFrame('chat_chunk', { slot: 's1' })
    setAppSlotPushConnected(true)
    expect(w.onChange).not.toHaveBeenCalled()
    expect(w.onConnection).not.toHaveBeenCalled()
  })

  it('reports connectivity to every watcher, and again on each reconnect', () => {
    owners.set('s1', 'spec-builder')
    const push = createAppSlotPush('spec-builder')
    const w = watcher()
    push.watch('s1', w)
    expect(push.connected('s1')).toBe(false)
    setAppSlotPushConnected(true)
    setAppSlotPushConnected(true)
    expect(push.connected('s1')).toBe(true)
    setAppSlotPushConnected(false)
    expect(push.connected('s1')).toBe(false)
    expect(w.onConnection.mock.calls).toEqual([[true], [true], [false]])
  })

  it('reads a slot the app does not own, or one not listed yet, as not live', () => {
    owners.set('theirs', 'ops-mission-control')
    const push = createAppSlotPush('spec-builder')
    const mine = watcher()
    const theirs = watcher()
    const unlisted = watcher()
    owners.set('mine', 'spec-builder')
    push.watch('mine', mine)
    push.watch('theirs', theirs)
    push.watch('unlisted', unlisted)
    setAppSlotPushConnected(true)
    expect(push.connected('mine')).toBe(true)
    expect(push.connected('theirs')).toBe(false)
    expect(push.connected('unlisted')).toBe(false)
    expect(mine.onConnection.mock.calls).toEqual([[true]])
    expect(theirs.onConnection.mock.calls).toEqual([[false]])
    expect(unlisted.onConnection.mock.calls).toEqual([[false]])
    // Ownership is read live: the slot arriving in the list makes it live.
    owners.set('unlisted', 'spec-builder')
    expect(push.connected('unlisted')).toBe(true)
  })

  it('is fed every decoded frame by the socket router', () => {
    const src = readFileSync(
      path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'hooks', 'useWebSocket.ts'),
      'utf-8',
    )
    const decode = src.indexOf('decodeFrame(e.data)')
    const note = src.indexOf('noteAppSlotFrame(type, data)')
    const router = src.indexOf('switch (type)', decode)
    expect(decode).toBeGreaterThan(-1)
    expect(note).toBeGreaterThan(decode)
    expect(note).toBeLessThan(router)
  })
})
