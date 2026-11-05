/**
 * The crewmate page's document Submit sends with the page's LATEST Auto (Jev)
 * answer, even from a callback the side panel was handed in an earlier render
 * (the panel sits under AnimatePresence and can keep one).
 *
 * The busy-send decision is stubbed to report what it was given: `'auto'` when
 * consent is on, a plain steer otherwise.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

const consent = vi.hoisted(() => ({ on: false }))
const sent = vi.hoisted(() => ({ calls: [] as Array<{ steer?: unknown }> }))

vi.mock('../pages/chat/useJevAutoSend', () => ({ useJevAutoSend: () => consent.on }))
vi.mock('../store', () => ({ useAppStore: () => ({ getState: () => ({}) }) }))
vi.mock('../components/chat-input/busySend', () => ({
  slotBusySteer: (_state: unknown, _slot: string, jevAutoConsented: boolean) => (jevAutoConsented ? 'auto' : true),
}))
vi.mock('../chat-core/transport/sendTurn', () => ({
  sendTurn: (req: { steer?: unknown }) => { sent.calls.push(req); return Promise.resolve({ status: 'dispatched' }) },
}))

import { useMemberDocumentComments } from '../hooks/useMemberDocumentComments'

const opts = () => ({
  connected: true,
  slotRef: { current: 'dm-slot' },
  besideRef: { current: true },
  crewmateLabel: 'Ada',
  showActionError: vi.fn(),
  clearActionError: vi.fn(),
})

beforeEach(() => { consent.on = false; sent.calls = [] })

describe('useMemberDocumentComments', () => {
  it('a Submit handed out before consent arrived sends with steer=auto', async () => {
    const options = opts()
    const { result, rerender } = renderHook(() => useMemberDocumentComments(options))
    const handedBeforeConsent = result.current.submitComments
    consent.on = true
    rerender()
    let verdict: unknown
    await act(async () => { verdict = await handedBeforeConsent('notes', 1) })
    expect(verdict).toBe(true)
    expect(sent.calls[0].steer).toBe('auto')
  })

  it('hands the panel the same function across renders', () => {
    const options = opts()
    const { result, rerender } = renderHook(() => useMemberDocumentComments(options))
    const first = result.current.submitComments
    consent.on = true
    rerender()
    expect(result.current.submitComments).toBe(first)
  })
})
