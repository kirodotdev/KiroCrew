/**
 * A second Dashboard card reply after the first one was sent.
 *
 * A sent reply starts a turn, the turn moves the crewmate's fold, and the page is
 * minted again: the new document loads in the off-screen frame and that element is
 * promoted, which REMOVES the frame the person just clicked in. These cases run the
 * real link-bridge bookkeeping (`focusEntry`, `hostInput`, `tiedToFrame`), so unlike
 * the send cases in CrewDynamicDashboard.test.tsx nothing about the tie is mocked.
 *
 * jsdom fires no window `focus`/`blur` for an iframe, so `clickInto` stands in for
 * the browser: a click into a frame blurs the dashboard's window only while the
 * dashboard holds focus. That is what Chromium does: once the focused frame is
 * removed, focus is left on no element and the window does not hold focus, so the
 * next click into a frame fires no window `blur` at all.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import { api, type DashboardManifest } from '../api/client'
import { renderWithProviders } from './helpers'
import CrewDynamicDashboard, { READY_MESSAGE_TYPE, SEND_MESSAGE_TYPE } from '../pages/members/CrewDynamicDashboard'
import { checkEntry, focusEntry, frameInputs, hostInput } from '../hooks/useFrameLinkBridge'

const mints: string[] = []
vi.mock('../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (srcdoc: string | null) => {
    if (srcdoc && mints[mints.length - 1] !== srcdoc) mints.push(srcdoc)
    return { url: srcdoc ? `/sandbox-doc/${mints.indexOf(srcdoc)}` : null, pending: false, failed: false, stalled: false, retry: vi.fn() }
  },
}))

const MANIFEST = { id: 'project-report', version: 1, title: 'Project report', fields: [] } as unknown as DashboardManifest
const page = (turns: number) => ({
  instance_version: 1,
  template: { id: 'project-report', version: 1 },
  html: '<p>stored</p>',
  rendered_html: `<p>${turns} turns</p>`,
  manifest: MANIFEST,
  state: 'live' as const,
})
const KEY = ['member-dashboard', 'oncall', 'oncall']

/** Whether the dashboard's own window holds focus, as the browser would track it. */
let windowFocused = true
function onFocusIn(e: FocusEvent) {
  if (e.target instanceof HTMLIFrameElement || windowFocused) return
  windowFocused = true
  window.dispatchEvent(new FocusEvent('focus'))
}

/** A person's click into *frame*: focus moves in; the window blurs only if it had focus. */
function clickInto(frame: HTMLIFrameElement) {
  // jsdom only focuses an iframe that carries a tab index; a browser focuses it on click.
  if (!frame.hasAttribute('tabindex')) frame.tabIndex = 0
  frame.focus()
  if (windowFocused) {
    windowFocused = false
    window.dispatchEvent(new FocusEvent('blur'))
  }
}

/** The element, given a window of its own when jsdom left it without one, so a
 *  message can name it as its source. The component compares identity only. */
function withWindow(frame: HTMLIFrameElement): HTMLIFrameElement {
  if (!frame.contentWindow) {
    Object.defineProperty(frame, 'contentWindow', { configurable: true, value: { postMessage: () => {} } as unknown as Window })
  }
  return frame
}
const frameEl = () => withWindow(screen.getByTestId('crew-dashboard-iframe') as HTMLIFrameElement)
const post = (frame: HTMLIFrameElement, text: string) =>
  act(() => {
    window.dispatchEvent(new MessageEvent('message', { data: { type: SEND_MESSAGE_TYPE, text }, source: frame.contentWindow }))
  })

beforeEach(() => {
  mints.length = 0
  windowFocused = true
  document.addEventListener('focusin', onFocusIn)
  hostInput.lastAt = -Infinity
  frameInputs.clear()
  focusEntry.frame = null
  focusEntry.pointer = null
  checkEntry()
  Object.defineProperty(navigator, 'userActivation', { value: { isActive: true }, configurable: true })
})

afterEach(() => {
  document.removeEventListener('focusin', onFocusIn)
  Reflect.deleteProperty(navigator, 'userActivation')
  vi.restoreAllMocks()
})

async function mountMember(onSend: (t: string) => Promise<'sent'>) {
  const read = vi.spyOn(api, 'memberDashboard').mockResolvedValueOnce(page(1)).mockResolvedValue(page(2))
  const onAct = vi.fn()
  const view = renderWithProviders(
    <CrewDynamicDashboard target={{ kind: 'member', slug: 'oncall', member: 'oncall' }} displayName="On Call" onAct={onAct} onSend={onSend} />,
  )
  await screen.findByTestId('crew-dashboard-iframe')
  return { ...view, onAct, read }
}

/** The turn the first reply started moved the fold: the page is minted again and promoted. */
async function remintAndPromote(queryClient: { invalidateQueries: (f: { queryKey: string[] }) => Promise<unknown> }) {
  const before = frameEl()
  await queryClient.invalidateQueries({ queryKey: KEY })
  const stage = withWindow((await screen.findByTestId('crew-dashboard-swap')) as HTMLIFrameElement)
  act(() => {
    window.dispatchEvent(new MessageEvent('message', { data: { type: READY_MESSAGE_TYPE }, source: stage.contentWindow }))
  })
  await waitFor(() => expect(frameEl()).not.toBe(before))
}

describe('a second card reply after a sent one', () => {
  it('sends again after the page is re-minted, without a click in the dashboard first', async () => {
    const clock = vi.spyOn(Date, 'now').mockReturnValue(1_000_000)
    const onSend = vi.fn().mockResolvedValue('sent' as const)
    const { queryClient, onAct } = await mountMember(onSend)
    clickInto(frameEl())
    post(frameEl(), 'Approve it')
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1))

    await remintAndPromote(queryClient)
    // Past the 3 second interval between two accepted sends.
    clock.mockReturnValue(1_010_000)
    clickInto(frameEl())
    post(frameEl(), 'Hold it')
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2))
    expect(onSend).toHaveBeenLastCalledWith('Hold it')
    expect(onAct).not.toHaveBeenCalled()
  })

  it('still holds back a fast second click on the same reply', async () => {
    const onSend = vi.fn().mockResolvedValue('sent' as const)
    const { onAct } = await mountMember(onSend)
    clickInto(frameEl())
    // The message handler runs inside dispatchEvent, so both clicks are decided here.
    post(frameEl(), 'Approve it')
    post(frameEl(), 'Approve it')
    expect(onSend).toHaveBeenCalledTimes(1)
    expect(onAct).not.toHaveBeenCalled()
  })
})
