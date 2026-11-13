/** The interrupted sidebar row under a sign-in error, and the shared table that
 *  holds the composer's `authRequiredIsNewest` and the gateway's
 *  `newest_error_is_sign_in` (which feeds the row's `interrupted_by_sign_in`)
 *  to the same answers. */
import { describe, it, expect } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import signInFixture from '../../../test/fixtures/sign_in_newest_rows.json'
import type { ChatMessage } from '../types'
import { authRequiredIsNewest } from '../pages/chat/transcriptRenderers'
import { useInterruptedRowLabel } from '../pages/chat-sidebar/interruptedRowLabel'
import { setResumeAllWaiting } from '../lib/resumeAllWaiting'
import type { Slot } from '../pages/chat-sidebar/types'

type SignInCase = { name: string; rows: ChatMessage[]; expected: boolean }

describe('the shared sign-in table (test/fixtures/sign_in_newest_rows.json)', () => {
  for (const c of (signInFixture as unknown as { cases: SignInCase[] }).cases) {
    it(`answers as the gateway does: ${c.name}`, () => {
      expect(authRequiredIsNewest(c.rows)).toBe(c.expected)
    })
  }
})

describe('useInterruptedRowLabel', () => {
  const label = (extra: Partial<Slot>) =>
    renderHook(() => useInterruptedRowLabel({ key: 'chat-1', running: false, interrupted: true, ...extra } as Slot)).result.current

  it('names the sign-in first when the turn ended on a sign-in error', () => {
    expect(label({ interrupted_by_sign_in: true })).toBe('Turn interrupted · Sign in, then Resume')
  })

  it('names Resume alone otherwise', () => {
    expect(label({})).toBe('Turn interrupted · Resume')
    expect(label({ interrupted_by_sign_in: false })).toBe('Turn interrupted · Resume')
  })

  it('names no instruction on a crew-bound row, sign-in or not', () => {
    expect(label({ interrupted_by_sign_in: true, executor: 'remote' })).toBe('Turn interrupted')
  })

  it('says it is waiting while a Resume-all run is about to reach it', () => {
    act(() => setResumeAllWaiting(['chat-1']))
    try {
      expect(label({ interrupted_by_sign_in: true })).toBe('Turn interrupted · Waiting to resume')
    } finally {
      act(() => setResumeAllWaiting([]))
    }
  })
})
