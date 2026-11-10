import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../../api/client', () => ({
  api: { chatMode: vi.fn().mockResolvedValue({}) },
}))

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'

import TrustResetNoticeCard, { type LostMode } from './TrustResetNoticeCard'
import { createTranscriptRenderers } from './transcriptRenderers'
import { searchByName } from '../../guide/findByName'
import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { createTestStore } from '../../test/helpers'
import dashboardReducer from '../../store/dashboardSlice'
import type { ChatSlot } from '../../types'

const SLOT = 'chat-1-lead'

function renderCard(mode: LostMode, slot: Partial<ChatSlot> = {}, cardSlot: string | null = SLOT, disabled: string[] = []) {
  const dashboard = dashboardReducer(undefined, { type: '@@test/init' })
  const store = createTestStore({
    dashboard: {
      ...dashboard,
      status: { ...(dashboard.status ?? {}), disabled_approval_modes: disabled } as never,
      slots: [{ key: SLOT, messages: 1, running: false, ...slot } as ChatSlot],
    },
  })
  render(
    <Provider store={store}>
      <TrustResetNoticeCard slot={cardSlot ?? undefined} mode={mode} />
    </Provider>,
  )
  return store
}

describe('TrustResetNoticeCard', () => {
  beforeEach(() => {
    cleanup()
    vi.mocked(api.chatMode).mockClear()
  })

  it('says the restart turned Trust off and sends no pick by itself', () => {
    renderCard('trust')
    const text = screen.getByTestId('trust-reset-notice-text').textContent ?? ''
    expect(text).toMatch(/Trust was turned off because .* restarted/)
    expect(text).not.toMatch(/trustResetNotice/)
    expect(screen.getByTestId('trust-reset-notice-again').textContent).toBe('Turn Trust back on')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(api.chatMode).not.toHaveBeenCalled()
  })

  it('picks the lost mode for this chat alone, through the picker path, on click', async () => {
    renderCard('trust')
    await act(async () => { fireEvent.click(screen.getByTestId('trust-reset-notice-again')) })
    expect(api.chatMode).toHaveBeenCalledTimes(1)
    expect(api.chatMode).toHaveBeenCalledWith('trust', SLOT)
  })

  it('offers Reads back for a chat that lost Reads', async () => {
    renderCard('trust_reads')
    const again = screen.getByTestId('trust-reset-notice-again')
    expect(again.textContent).toBe('Turn Reads back on')
    await act(async () => { fireEvent.click(again) })
    expect(api.chatMode).toHaveBeenCalledWith('trust_reads', SLOT)
  })

  it('drops the button once the chat holds the mode again', () => {
    renderCard('trust', { trust: true })
    expect(screen.queryByTestId('trust-reset-notice-again')).not.toBeInTheDocument()
    expect(screen.getByTestId('trust-reset-notice-text').textContent).toMatch(/on again/)
  })

  it('says so through ErrorNotice when the pick fails, and keeps the button', async () => {
    vi.mocked(api.chatMode).mockRejectedValueOnce(new Error('boom'))
    renderCard('trust')
    await act(async () => { fireEvent.click(screen.getByTestId('trust-reset-notice-again')) })
    // Through ErrorNotice, keyed on the rejection's own message.
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toMatch(/Could not change/)
    expect(alert.textContent).toMatch(/boom/)
    expect(screen.getByTestId('trust-reset-notice-again')).not.toBeDisabled()
  })

  it('sends no pick when it has no chat to scope it to (an unscoped pick would trust every chat)', async () => {
    renderCard('trust', {}, null)
    await act(async () => { fireEvent.click(screen.getByTestId('trust-reset-notice-again')) })
    expect(api.chatMode).not.toHaveBeenCalled()
  })

  it('is a guide trust root: ui.find refuses both buttons', () => {
    const realRect = HTMLElement.prototype.getBoundingClientRect
    HTMLElement.prototype.getBoundingClientRect = function () {
      return { top: 10, left: 10, width: 40, height: 20, right: 50, bottom: 30, x: 10, y: 10, toJSON: () => ({}) } as DOMRect
    }
    try {
      renderCard('trust_reads')
      expect(searchByName({ label: 'Turn Reads back on' }).result).toBe('sensitive')
      cleanup()
      renderCard('trust')
      expect(searchByName({ label: 'Turn Trust back on' }).result).toBe('sensitive')
    } finally {
      HTMLElement.prototype.getBoundingClientRect = realRect
    }
  })

  it('is drawn for a lost Trust or Reads row only', () => {
    const entry = createTranscriptRenderers({}).find(r => r.id === 'trust_reset_notice')
    expect(entry?.match).toBeDefined()
    const row = (approval_mode: string) => ({ role: 'system', content: '', ts: '1', meta: { kind: 'trust_reset_notice', approval_mode } })
    expect(entry!.match!(row('trust') as never)).toBe(true)
    expect(entry!.match!(row('trust_reads') as never)).toBe(true)
    expect(entry!.match!(row('yolo') as never)).toBe(false)
  })

  it('offers no pick a policy denies, and says the policy is why', () => {
    renderCard('trust', {}, SLOT, ['trust'])
    expect(screen.queryByTestId('trust-reset-notice-again')).not.toBeInTheDocument()
    expect(screen.getByTestId('trust-reset-notice-policy').textContent).toMatch(/security policy/)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows a policy 403 through ErrorNotice and drops the button', async () => {
    vi.mocked(api.chatMode).mockRejectedValueOnce(
      new ApiError(403, 'trust is turned off by the approval_modes policy', JSON.stringify({ code: 'mode_disabled_by_policy' })),
    )
    renderCard('trust')
    await act(async () => { fireEvent.click(screen.getByTestId('trust-reset-notice-again')) })
    // A refused request: ErrorNotice with the policy line, and no retry button.
    expect(screen.getByRole('alert').textContent).toMatch(/security policy/)
    expect(screen.queryByTestId('trust-reset-notice-again')).not.toBeInTheDocument()
  })
})
