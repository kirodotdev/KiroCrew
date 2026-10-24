/**
 * A create that asked for a folder and answered with a different `folder_id`
 * opened outside that folder. The filing was refused, so the person sees the
 * App shell's not-filed notice as an ErrorNotice (an alert, so it is
 * announced) that stays until it is dismissed or handed off, not only a
 * console warning. It is its own notice: the shared agent-switch toast keeps
 * its six-second expiry (App.test.tsx pins that).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { api, __resetAuthRecoveryStateForTests } from '../api/client'
import { i18nT } from '../i18n/t'
import { SessionNotFiledNotice } from '../shell/SessionNotFiledNotice'
import { SESSION_NOT_FILED_EVENT } from '../lib/sessionFolderNotice'
import { queryClient } from '../api/queryClient'

// The hand-off's own outcome is what clears the notice: true when the prompt
// was staged and the navigation ran, false when the page's leave guard vetoed.
const handoff = vi.hoisted(() => ({ proceeds: true }))
vi.mock('../utils/errorReport', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/errorReport')>()),
  sendErrorToChat: vi.fn(() => handoff.proceeds),
}))

function answer(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
}

const fetchMock = vi.fn()

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
  __resetAuthRecoveryStateForTests()
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  handoff.proceeds = true
})

afterEach(() => {
  // A test that times out never reaches its finally: put the real clock back.
  vi.useRealTimers()
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  __resetAuthRecoveryStateForTests()
  queryClient.removeQueries({ queryKey: ['chat-folders'] })
})

const createInFolder = () =>
  api.createChatSlot('demo', 'worker', undefined, undefined, 'persistent', undefined, undefined, 'f1')

const announce = () => act(() => { window.dispatchEvent(new Event(SESSION_NOT_FILED_EVENT)) })
const FALLBACK = () => i18nT('store.chatSlice.session_not_filed_untitled_folder_gone')

describe('a create that opened outside its folder', () => {
  it('shows the reason through ErrorNotice, as an alert', async () => {
    render(<SessionNotFiledNotice />)
    fetchMock.mockResolvedValue(answer({ key: 'demo', folder_id: '' }))
    await act(async () => { await createInFolder() })
    const notice = await screen.findByRole('alert')
    expect(notice).toHaveAttribute('data-testid', 'session-not-filed-notice')
    expect(screen.queryByRole('status')).toBeNull()
    // No title and no cached folder: the notice falls back to "a new chat"
    // and the folder-gone cause.
    expect(notice).toHaveTextContent(FALLBACK())
    expect(FALLBACK()).toBe('A new chat opened outside any folder because its folder no longer exists.')
    // The hand-off is offered: the notice holds no draft of its own.
    expect(screen.getByRole('button', { name: i18nT('components.askAgent.ask_the_agent') })).toBeInTheDocument()
  })

  it('names the chat and the folder it could not be filed into', async () => {
    queryClient.setQueryData(['chat-folders'], [{ id: 'f0', name: 'Archive' }, { id: 'f1', name: 'Research' }])
    render(<SessionNotFiledNotice />)
    fetchMock.mockResolvedValue(answer({ key: 'demo', title: 'Quarterly plan', folder_id: '' }))
    await act(async () => { await createInFolder() })
    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent('"Quarterly plan" opened outside any folder because it could not be filed into "Research".')
    expect(notice).not.toHaveTextContent('Archive')
  })

  it('says its folder no longer exists when the folder is no longer in the list', async () => {
    queryClient.setQueryData(['chat-folders'], [{ id: 'f0', name: 'Archive' }])
    render(<SessionNotFiledNotice />)
    fetchMock.mockResolvedValue(answer({ key: 'demo', title: 'Quarterly plan', folder_id: '' }))
    await act(async () => { await createInFolder() })
    expect(await screen.findByRole('alert')).toHaveTextContent('"Quarterly plan" opened outside any folder because its folder no longer exists.')
  })

  it('says "a new chat" when the create has no title', async () => {
    queryClient.setQueryData(['chat-folders'], [{ id: 'f1', name: 'Research' }])
    render(<SessionNotFiledNotice />)
    fetchMock.mockResolvedValue(answer({ key: 'demo', folder_id: '' }))
    await act(async () => { await createInFolder() })
    expect(await screen.findByRole('alert')).toHaveTextContent('A new chat opened outside any folder because it could not be filed into "Research".')
  })

  it('shows nothing when the slot was filed', async () => {
    render(<SessionNotFiledNotice />)
    fetchMock.mockResolvedValue(answer({ key: 'demo', folder_id: 'f1' }))
    await act(async () => { await createInFolder() })
    expect(screen.queryByRole('status')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('stays past six seconds until Dismiss clears it', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      render(<SessionNotFiledNotice />)
      announce()
      await act(async () => { await vi.advanceTimersByTimeAsync(60000) })
      const notice = screen.getByTestId('session-not-filed-notice')
      expect(notice).toHaveTextContent(FALLBACK())
      fireEvent.click(screen.getByRole('button', { name: i18nT('components.errorNotice.dismiss') }))
      expect(screen.queryByRole('alert')).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  for (const [outcome, proceeds] of [['a hand-off that proceeds clears it', true], ['a vetoed hand-off keeps it', false]] as const) {
    it(outcome, () => {
      handoff.proceeds = proceeds
      render(<SessionNotFiledNotice />)
      announce()
      fireEvent.click(screen.getByRole('button', { name: i18nT('components.askAgent.ask_the_agent') }))
      if (proceeds) expect(screen.queryByRole('alert')).toBeNull()
      else expect(screen.getByRole('alert')).toHaveTextContent(FALLBACK())
    })
  }
})
