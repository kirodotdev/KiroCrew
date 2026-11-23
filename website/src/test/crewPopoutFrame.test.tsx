import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'

vi.mock('../api/client', () => ({
  ApiError: class extends Error {},
  api: { listInstances: vi.fn(), connectInstance: vi.fn(), embedInstanceToken: vi.fn() },
}))

import { api } from '../api/client'
import CrewPopoutFrame from '../pages/CrewPopoutFrame'
import { PANE_FRAME_ALLOW } from '../components/InstancesViewport'
import { i18nT } from '../i18n/t'

const CREW_PORT = 7101
const CREW = `http://localhost:${CREW_PORT}`
const NOT_CONNECTED = 'pages.crewPopoutFrame.this_crew_is_not_connected'
const CANNOT_OPEN = 'pages.crewPopoutFrame.this_pop_out_cannot_be_opened'

function route(path: string, port = CREW_PORT): string {
  return `/popout/crew?${new URLSearchParams({ port: String(port), path })}`
}

async function frame(): Promise<HTMLIFrameElement> {
  return await screen.findByTitle('Cloud One') as HTMLIFrameElement
}

// happy-dom loads no frame, so give the iframe a window to speak from.
async function frameWindow(): Promise<Window> {
  const child = { postMessage: vi.fn() } as unknown as Window
  Object.defineProperty(await frame(), 'contentWindow', { value: child, configurable: true })
  return child
}

beforeEach(() => {
  vi.mocked(api.listInstances).mockResolvedValue({
    active: true,
    instances: [{ id: 'cd-1', name: 'Cloud One', status: { instance_id: 'cd-1', state: 'connected', local_port: CREW_PORT } }],
  } as unknown as Awaited<ReturnType<typeof api.listInstances>>)
  vi.mocked(api.connectInstance).mockResolvedValue({
    instance_id: 'cd-1', state: 'connected', local_port: CREW_PORT, token: 'tok',
  })
})
afterEach(() => {
  vi.clearAllMocks()
  vi.restoreAllMocks()
})

describe('CrewPopoutFrame', () => {
  it.each([
    ['an authority smuggled into the path', `@127.0.0.1:6776/`],
    ['a protocol-relative path to another host', '//evil/popout/chat'],
    ['a crew page that is not a pop-out', '/settings'],
    ['a pop-out path that walks out of /popout/', '/popout/../settings'],
  ])('refuses %s, with no iframe and no instances call', async (_name, path) => {
    renderWithProviders(<CrewPopoutFrame />, { route: route(path) })
    expect(await screen.findByText(i18nT(CANNOT_OPEN))).toBeTruthy()
    expect(document.querySelector('iframe')).toBeNull()
    expect(api.listInstances).not.toHaveBeenCalled()
  })

  it('refuses a port that is not a connected crew, and brings no tunnel up', async () => {
    renderWithProviders(<CrewPopoutFrame />, { route: route('/popout/chat?sid=s1', 7999) })
    expect(await screen.findByText(i18nT(NOT_CONNECTED))).toBeTruthy()
    expect(document.querySelector('iframe')).toBeNull()
    expect(api.connectInstance).not.toHaveBeenCalled()
  })

  it('refuses when the crew answers that its tunnel is not up', async () => {
    vi.mocked(api.connectInstance).mockResolvedValue({ instance_id: 'cd-1', state: 'disconnected' })
    renderWithProviders(<CrewPopoutFrame />, { route: route('/popout/chat?sid=s1') })
    expect(await screen.findByText(i18nT(NOT_CONNECTED))).toBeTruthy()
    expect(document.querySelector('iframe')).toBeNull()
  })

  it('shows a failed connect through ErrorNotice, with no iframe', async () => {
    vi.mocked(api.connectInstance).mockRejectedValue(new Error('check that its remote gateway is running'))
    renderWithProviders(<CrewPopoutFrame />, { route: route('/popout/chat?sid=s1') })
    expect((await screen.findByRole('alert')).textContent).toContain('check that its remote gateway is running')
    expect(screen.queryByText(i18nT(CANNOT_OPEN))).toBeNull()
    expect(document.querySelector('iframe')).toBeNull()
  })

  it('frames the crew pop-out with its token in the query and the pane allow list', async () => {
    renderWithProviders(<CrewPopoutFrame />, { route: route('/popout/chat/my-chat?sid=s1&token=stale') })
    const el = await frame()
    const src = new URL(el.getAttribute('src') ?? '')
    expect(src.origin).toBe(CREW)
    expect(src.pathname).toBe('/popout/chat/my-chat')
    expect(src.searchParams.get('sid')).toBe('s1')
    expect(src.searchParams.getAll('token')).toEqual(['tok'])
    expect(el.getAttribute('allow')).toBe(PANE_FRAME_ALLOW)
    expect(api.connectInstance).toHaveBeenCalledWith('cd-1', { onlyIfConnected: true })
    await waitFor(() => expect(document.title).toBe('Cloud One'))
  })

  it('answers its own frame with an ack and a desktop host model, posted to the crew origin only', async () => {
    renderWithProviders(<CrewPopoutFrame />, { route: route('/popout/chat?sid=s1') })
    const child = await frameWindow()
    const post = vi.mocked(child.postMessage)
    act(() => {
      window.dispatchEvent(new MessageEvent('message', { source: window, origin: CREW, data: { type: 'mc-embedded-ready', v: 1 } }))
    })
    expect(post).not.toHaveBeenCalled()
    act(() => {
      window.dispatchEvent(new MessageEvent('message', { source: child, origin: CREW, data: { type: 'mc-embedded-ready', v: 1 } }))
    })
    expect(post).toHaveBeenCalledWith({ type: 'mc-embedded-ack', v: 1 }, CREW)
    expect(post).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'mc-host-model', v: 1, tabs: [], electron: true, crewPopout: 1 }), CREW,
    )
  })

  it('closes its window only when its own frame asks', async () => {
    renderWithProviders(<CrewPopoutFrame />, { route: route('/popout/chat?sid=s1') })
    const child = await frameWindow()
    const close = vi.spyOn(window, 'close').mockImplementation(() => {})
    act(() => {
      window.dispatchEvent(new MessageEvent('message', { source: window, origin: CREW, data: { type: 'mc-close-popout', v: 1 } }))
      window.dispatchEvent(new MessageEvent('message', { source: child, origin: 'http://localhost:7999', data: { type: 'mc-close-popout', v: 1 } }))
    })
    expect(close).not.toHaveBeenCalled()
    act(() => {
      window.dispatchEvent(new MessageEvent('message', { source: child, origin: CREW, data: { type: 'mc-close-popout', v: 1 } }))
    })
    expect(close).toHaveBeenCalledTimes(1)
  })
})
