/**
 * The unsent-draft warning ENRICHES a close confirmation; it never summons one.
 *
 * An earlier revision of this guard forced the dialog whenever a composer held unsent
 * work, on every route. That overrode `confirmCloseSession` on the sidebar `✕` and the
 * session menu — routes this change was not otherwise touching — so a user who had
 * silenced the dialog got one anyway. The chip is the only affordance a MODEL authored,
 * and it asks for the confirm explicitly via `forceConfirm`; the product's own routes
 * keep the user's preference. So the draft warning upgrades a dialog that was already
 * going to appear, and stays silent where the user asked for silence.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'

const mockDeleteSlot = vi.fn()

// Mutable so both postures are reachable: the DEFAULT (off) is where the override was
// visible, and the on-posture proves the warning still reaches a user who wants dialogs.
const chatCfg = vi.hoisted(() => ({ confirmCloseSession: false }))

vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ confirmCloseSession: chatCfg.confirmCloseSession }),
}))

vi.mock('../store/chatSlice', async (orig) => {
  const actual = (await orig()) as Record<string, unknown>
  return {
    ...actual,
    deleteSlot: (key: string) => {
      mockDeleteSlot(key)
      return { type: 'test/deleteSlot', payload: key }
    },
  }
})

import { useSessionActions } from '../hooks/useSessionActions'
import { nextComposerId, registerSlotComposer } from '../utils/slotComposerRegistry'
import { createTestStore } from './helpers'
import { i18nT } from '../i18n/t'

const SLOT = 'slot-unsent-guard-1'

function host() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <Provider store={createTestStore({})}>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    </Provider>
  )
  return renderHook(() => useSessionActions(), { wrapper })
}

describe('unsent work summons its own close confirm, on every route', () => {
  let confirmSpy: ReturnType<typeof vi.spyOn>
  let release: (() => void) | null = null

  const draft = () => registerSlotComposer(nextComposerId(), {
    getSlot: () => SLOT,
    hasWork: () => true,
  })

  beforeEach(() => {
    vi.clearAllMocks()
    chatCfg.confirmCloseSession = false
    confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
  })

  afterEach(() => {
    confirmSpy.mockRestore()
    release?.()
    release = null
  })

  it('names every KIND of unsent work, not just a composer draft', () => {
    // Mid-recording or mid-upload the composer looks EMPTY, so a prompt naming only a
    // draft invites the user to dismiss it as stale and lose what the guard protects.
    const base = 'Close this session?'
    const prompt = i18nT('hooks.useSessionActions.close_unsent_confirm', { base })
    expect(prompt).toContain(base)
    for (const kind of ['draft', 'attachment', 'recording']) {
      expect(prompt.toLowerCase()).toContain(kind)
    }
    // And it must not assert the work is visible in THIS composer.
    expect(prompt).not.toContain('in the composer')
  })

  it('does NOT confirm when the slot is clean', async () => {
    // Positive control: proves a confirm below is caused by the registered draft and not
    // by this harness confirming unconditionally.
    const h = host()
    await act(async () => { await h.result.current.close(SLOT) })
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(mockDeleteSlot).toHaveBeenCalledWith(SLOT)
  })

  it('CONFIRMS on a product route with the preference OFF when a draft exists', async () => {
    // The session menu, the sidebar close control and Alt+Shift+W with `confirmCloseSession`
    // at its default OFF, where an unsent draft used to be deleted in silence.
    release = draft()
    const h = host()
    await act(async () => { await h.result.current.close(SLOT) })

    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(String(confirmSpy.mock.calls[0][0])).toContain('unsent work')
    expect(mockDeleteSlot).toHaveBeenCalledWith(SLOT)
  })

  it('CONFIRMS, naming the draft loss, on the model-authored chip path', async () => {
    release = draft()
    const h = host()
    await act(async () => { await h.result.current.close(SLOT, { forceConfirm: true }) })

    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(String(confirmSpy.mock.calls[0][0])).toContain('unsent work')
  })

  it('CONFIRMS, naming the draft loss, for a user who asked for dialogs', async () => {
    chatCfg.confirmCloseSession = true
    release = draft()
    const h = host()
    await act(async () => { await h.result.current.close(SLOT) })

    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(String(confirmSpy.mock.calls[0][0])).toContain('unsent work')
  })

  it('ABORTS the close when the user declines that confirmation', async () => {
    confirmSpy.mockReturnValue(false)
    release = draft()
    const h = host()
    await act(async () => { await h.result.current.close(SLOT, { forceConfirm: true }) })

    expect(mockDeleteSlot).not.toHaveBeenCalled()
  })

  it('keeps a caller-supplied message AND adds the draft warning', async () => {
    // Appended rather than substituted: the chip path's message names the label the user
    // clicked, and the draft loss is a separate fact. Neither should silence the other.
    release = draft()
    const h = host()
    await act(async () => {
      await h.result.current.close(SLOT, { forceConfirm: true, confirmMessage: 'You chose “Done”.' })
    })
    const shown = String(confirmSpy.mock.calls[0][0])
    expect(shown).toContain('You chose “Done”.')
    expect(shown).toContain('unsent work')
  })
})
