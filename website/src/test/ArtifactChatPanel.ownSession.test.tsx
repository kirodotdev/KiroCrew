/**
 * The artifact companion embeds ChatPage, which renders the dashboard's ACTIVE
 * slot. When the bound session is gone or fails to open, the switch leaves
 * another chat active, and the panel must not show it (#25341): whatever the
 * user typed there would go to that other session. ChatPage is stubbed, so a
 * rendered chat is the active slot on screen.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { Route, Routes, useNavigate } from 'react-router-dom'
import { renderWithProviders, createTestStore } from './helpers'
import { api } from '../api/client'
import { useAppDispatch } from '../store'
import { addSlotOptimistic } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
import { i18nT } from '../i18n/t'
import { ArtifactChatPanel } from '../components/ArtifactChatPanel'

vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="zzq-chatpage" />,
}))

// Mirrors the real thunk: a live session becomes the active slot; a deleted one
// rejects with the 404 payload and leaves the previous active slot in place; a
// gateway failure rejects with its status. A key in `realSlots` runs the real
// thunk, so its switch stays in flight until its read answers.
const goneSlots = vi.hoisted(() => new Set<string>())
const failingSlots = vi.hoisted(() => new Set<string>())
const realSlots = vi.hoisted(() => new Set<string>())
const realSwitch = vi.hoisted(() => ({ thunk: null as null | ((key: string) => unknown) }))
// A key here answers its switch only when the test calls the stored reject.
const lateSlots = vi.hoisted(() => new Map<string, (err: unknown) => void>())
const switchSlot = vi.hoisted(() => vi.fn((key: string) => (dispatch: (a: unknown) => unknown) => {
  if (realSlots.has(key) && realSwitch.thunk) return dispatch(realSwitch.thunk(key))
  if (lateSlots.has(key)) {
    const pending = new Promise<void>((_resolve, reject) => { lateSlots.set(key, reject) })
    return { unwrap: () => pending }
  }
  if (goneSlots.has(key)) return { unwrap: () => Promise.reject({ status: 404, message: 'not found' }) }
  if (failingSlots.has(key)) return { unwrap: () => Promise.reject({ status: 503, message: 'gateway unavailable' }) }
  dispatch({ type: 'chat/setActiveSlot', payload: key })
  return { unwrap: () => Promise.resolve() }
}))
vi.mock('../store/chatSlice', async (importOriginal) => {
  const real = await importOriginal<Record<string, unknown>>()
  realSwitch.thunk = real.switchSlot as (key: string) => unknown
  return { ...real, switchSlot }
})

const slot = (key: string) => ({ key, title: key, messages: 0, running: false }) as ChatSlot

// The dashboard has another chat open, and the panel's session is listed.
function storeWith(...keys: string[]) {
  const store = createTestStore()
  act(() => {
    for (const key of ['zzq-other', ...keys]) store.dispatch(addSlotOptimistic(slot(key)))
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'zzq-other' })
  })
  return store
}

const switchesTo = (key: string) => switchSlot.mock.calls.filter(([k]) => k === key).length

const panel = (slotKey: string) => (
  <ArtifactChatPanel slotKey={slotKey} creating={false} onNewChat={() => {}} onOpenFull={() => {}} onClose={() => {}} />
)

beforeEach(() => {
  switchSlot.mockClear()
  goneSlots.clear()
  failingSlots.clear()
  realSlots.clear()
  lateSlots.clear()
})

describe('ArtifactChatPanel on its bound session', () => {
  it('drops a deleted session from the slot list instead of showing the open chat', async () => {
    goneSlots.add('zzq-gone')
    const store = storeWith('zzq-gone')
    renderWithProviders(panel('zzq-gone'), { store })
    await waitFor(() => expect(store.getState().dashboard.slots.map(s => s.key)).not.toContain('zzq-gone'))
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()
    expect(store.getState().dashboard.closingSlots['zzq-gone']).toBeDefined()
  })

  it('keeps a session whose 404 arrives after the panel closed', async () => {
    // The user may have reopened it from History meanwhile: a late answer to
    // the closed panel's open must not remove it.
    lateSlots.set('zzq-late', () => {})
    const store = storeWith('zzq-late')
    const { unmount } = renderWithProviders(panel('zzq-late'), { store })
    await waitFor(() => expect(switchesTo('zzq-late')).toBe(1))
    unmount()
    await act(async () => { lateSlots.get('zzq-late')?.({ status: 404, message: 'not found' }) })
    expect(store.getState().dashboard.slots.map(s => s.key)).toContain('zzq-late')
    expect(store.getState().dashboard.closingSlots['zzq-late']).toBeUndefined()
  })

  it('shows a failed open with Try again, and the chat once it opens', async () => {
    failingSlots.add('zzq-down')
    const store = storeWith('zzq-down')
    renderWithProviders(panel('zzq-down'), { store })
    expect(await screen.findByText('gateway unavailable')).toBeInTheDocument()
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()
    failingSlots.delete('zzq-down')
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.webPreviewPanel.try_again') }))
    expect(await screen.findByTestId('zzq-chatpage')).toBeInTheDocument()
    expect(store.getState().chat.activeSlot).toBe('zzq-down')
  })

  it('makes its session active again when another chat becomes active', async () => {
    const store = storeWith('zzq-bound')
    renderWithProviders(panel('zzq-bound'), { store })
    expect(await screen.findByTestId('zzq-chatpage')).toBeInTheDocument()
    act(() => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'zzq-other' }) })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('zzq-bound'))
    expect(switchesTo('zzq-bound')).toBe(2)
  })

  it('lets a control that opens another session and leaves the page keep that session', async () => {
    function GoToOther() {
      const dispatch = useAppDispatch()
      const navigate = useNavigate()
      return <button type="button" onClick={() => { void dispatch(switchSlot('zzq-other')); navigate('/chat') }}>zzq go</button>
    }
    const store = storeWith('zzq-bound')
    renderWithProviders(
      <Routes>
        <Route path="/artifacts/doc" element={<>{panel('zzq-bound')}<GoToOther /></>} />
        <Route path="/chat" element={<div>zzq chat route</div>} />
      </Routes>,
      { store, route: '/artifacts/doc' },
    )
    expect(await screen.findByTestId('zzq-chatpage')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'zzq go' }))
    expect(await screen.findByText('zzq chat route')).toBeInTheDocument()
    expect(store.getState().chat.activeSlot).toBe('zzq-other')
    expect(switchesTo('zzq-bound')).toBe(1)
  })

  it('lets a control that awaits its switch before leaving the page keep that session', async () => {
    realSlots.add('zzq-other')
    let answer: (page: unknown) => void = () => {}
    vi.spyOn(api, 'chatSlotDetail').mockImplementation(() => new Promise(resolve => { answer = resolve }))
    function GoToOther() {
      const dispatch = useAppDispatch()
      const navigate = useNavigate()
      const go = async () => {
        await (dispatch(switchSlot('zzq-other')) as unknown as { unwrap: () => Promise<unknown> }).unwrap()
        navigate('/chat')
      }
      return <button type="button" onClick={() => { void go() }}>zzq go</button>
    }
    const store = storeWith('zzq-bound')
    renderWithProviders(
      <Routes>
        <Route path="/artifacts/doc" element={<>{panel('zzq-bound')}<GoToOther /></>} />
        <Route path="/chat" element={<div>zzq chat route</div>} />
      </Routes>,
      { store, route: '/artifacts/doc' },
    )
    expect(await screen.findByTestId('zzq-chatpage')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'zzq go' }))
    // The read is still loading: the panel does not take the active slot back,
    // and draws no chat, which would be the other session's.
    expect(store.getState().chat.slotSwitchTarget).toBe('zzq-other')
    expect(switchesTo('zzq-bound')).toBe(1)
    expect(screen.queryByTestId('zzq-chatpage')).not.toBeInTheDocument()
    await act(async () => { answer({ messages: [], has_more: false, total: 0 }) })
    expect(await screen.findByText('zzq chat route')).toBeInTheDocument()
    expect(store.getState().chat.activeSlot).toBe('zzq-other')
    expect(switchesTo('zzq-bound')).toBe(1)
  })
})
