/**
 * The chat pane's notices (pages/chat/page/ChatPaneNotices.tsx): each one's
 * dismiss clears exactly the state that raised it.
 *
 * Page-level suites raise these notices (unresumableNotice, sid, dirStaging,
 * sendCreateFailure, ...) and read their copy; none presses their dismiss. The
 * notices share one rendering site, so a dismiss wired to the wrong setter
 * would clear a different notice than the one the user closed. Rendered with
 * every notice up at once, each ✕ is pressed and its own clear asserted, and
 * nothing else.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import ChatPaneNotices from '../pages/chat/page/ChatPaneNotices'
import { i18nT } from '../i18n/t'
import { clearSwitchSlotGone, clearUndeletableHistory, clearUnresumableResume } from '../store/chatSlice'
import { createTestStore } from './helpers'

type Props = ComponentProps<typeof ChatPaneNotices>

function allUp(over: Partial<Props> = {}): Props {
  return {
    uploadHint: 'Too many files',
    setUploadHint: vi.fn(),
    uploadError: 'Upload failed',
    setUploadError: vi.fn(),
    sidError: 'That link is dead',
    setSidError: vi.fn(),
    activeSlot: 'slot-a',
    provider: { capabilities: { reasoningEffort: true } } as never,
    selectionCapabilitiesQ: { isError: true },
    chipDefault: { failed: false },
    actionError: { title: 'Could not rename', message: 'server said no' },
    setActionError: vi.fn(),
    switchSlotGone: { name: 'Old chat', kind: 'gone' },
    setVoiceRecoverySlot: vi.fn(),
    pinError: 'Pin failed',
    pinStatus: 'Pin pending',
    dismissPinStatus: vi.fn(),
    unresumableResume: { key: 'slack:C1:t', title: 'Thread', surface: 'slack', reason: 'failed' },
    undeletableHistory: { key: 'dashboard:x', title: 'Cron chat', code: 'other' },
    dispatch: vi.fn() as never,
    ...over,
  }
}

function renderNotices(p: Props) {
  return render(
    <Provider store={createTestStore()}>
      <MemoryRouter>
        <ChatPaneNotices {...p} />
      </MemoryRouter>
    </Provider>,
  )
}

const dismissIn = (el: HTMLElement) => within(el).getByRole('button', { name: i18nT('components.errorNotice.dismiss') })

describe('ChatPaneNotices dismissals', () => {
  it('each ErrorNotice clears only its own state', () => {
    const p = allUp()
    renderNotices(p)
    fireEvent.click(dismissIn(screen.getByTestId('upload-error')))
    expect(p.setUploadError).toHaveBeenCalledWith('')
    fireEvent.click(dismissIn(screen.getByTestId('sid-error')))
    expect(p.setSidError).toHaveBeenCalledWith('')
    fireEvent.click(dismissIn(screen.getByTestId('action-error')))
    expect(p.setActionError).toHaveBeenCalledWith(null)
    fireEvent.click(dismissIn(screen.getByTestId('pin-error')))
    expect(p.dismissPinStatus).toHaveBeenCalledTimes(1)
    fireEvent.click(dismissIn(screen.getByTestId('switch-slot-gone')))
    expect(p.dispatch).toHaveBeenCalledWith(clearSwitchSlotGone())
    fireEvent.click(dismissIn(screen.getByTestId('unresumable-resume-error')))
    expect(p.dispatch).toHaveBeenCalledWith(clearUnresumableResume())
    fireEvent.click(dismissIn(screen.getByTestId('undeletable-history-error')))
    expect(p.dispatch).toHaveBeenCalledWith(clearUndeletableHistory())
    expect(p.dispatch).toHaveBeenCalledTimes(3)
    expect(p.setUploadHint).not.toHaveBeenCalled()
  })

  it('the two plain status rows dismiss through their own setters', () => {
    const p = allUp()
    renderNotices(p)
    const statusRows = screen.getAllByRole('status').filter(el => /Too many files|Pin pending/.test(el.textContent ?? ''))
    expect(statusRows).toHaveLength(2)
    for (const row of statusRows) fireEvent.click(within(row).getByRole('button', { name: i18nT('app.dismiss') }))
    expect(p.setUploadHint).toHaveBeenCalledWith('')
    expect(p.dismissPinStatus).toHaveBeenCalledTimes(1)
  })

  it('says effort options are unavailable only while the capability read failed for an open slot', () => {
    const { unmount } = renderNotices(allUp())
    expect(screen.getByTestId('effort-capabilities-error')).toHaveTextContent(i18nT('pages.chatPage.effort_options_unavailable'))
    unmount()
    renderNotices(allUp({ activeSlot: null }))
    expect(screen.queryByText(i18nT('pages.chatPage.effort_options_unavailable'))).toBeNull()
  })

  it('treats a 404 capability read as not-yet-known, not as a failure (#14817)', () => {
    // A new chat's slot is not registered yet, so the read answers slot_not_found.
    const notFound = Object.assign(new Error('not found'), { status: 404 })
    const { unmount } = renderNotices(allUp({ selectionCapabilitiesQ: { isError: true, error: notFound } }))
    expect(screen.queryByText(i18nT('pages.chatPage.effort_options_unavailable'))).toBeNull()
    unmount()
    // A real fault (peer unavailable) still says so.
    const peerDown = Object.assign(new Error('peer unavailable'), { status: 503 })
    renderNotices(allUp({ selectionCapabilitiesQ: { isError: true, error: peerDown } }))
    expect(screen.getByTestId('effort-capabilities-error')).toHaveTextContent(i18nT('pages.chatPage.effort_options_unavailable'))
  })

  it('says the Settings default model could not be read only for an open slot', () => {
    const { unmount } = renderNotices(allUp({ chipDefault: { failed: true } }))
    expect(screen.getByTestId('model-default-error')).toHaveTextContent(i18nT('pages.settings.chatPanel.failed_to_load_config'))
    unmount()
    renderNotices(allUp({ chipDefault: { failed: true }, activeSlot: null }))
    expect(screen.queryByText(i18nT('pages.settings.chatPanel.failed_to_load_config'))).toBeNull()
    renderNotices(allUp())
    expect(screen.queryByText(i18nT('pages.settings.chatPanel.failed_to_load_config'))).toBeNull()
  })

  it('names a failed resume without guessing its surface, and a refused delete by its title', () => {
    renderNotices(allUp())
    expect(screen.getByTestId('unresumable-resume-error')).toHaveTextContent(i18nT('pages.chatPage.could_not_open_this_session', { title: 'Thread' }))
    expect(screen.getByTestId('undeletable-history-error')).toHaveTextContent(i18nT('pages.chatPage.could_not_delete_this_session', { title: 'Cron chat' }))
  })
})
