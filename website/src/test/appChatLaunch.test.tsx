/**
 * App chat launch (`permissions.chatLaunch`, App Kit seam level 10), dashboard half.
 *
 * `useChatLauncher().createChat` creates or reopens the person's chat an app's
 * manifest declares, through the host rather than the app's scoped client: the
 * page declares no `/api/chat/slots`. The create names the app twice
 * (`app_launch`, which the gateway holds to the declaration, and `embed_app`,
 * which records the app on the conversation it begins) and nothing else rides
 * along. The trust prompt and the detail page show the whole declaration: the
 * agents, the folder, the key prefix and the memory modes.
 */
import React from 'react'
import { render, renderHook, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../i18n/t', () => ({
  i18nT: (key: string, vars?: Record<string, unknown>) =>
    vars && Object.keys(vars).length ? `${key} ${Object.values(vars).join(' | ')}` : key,
}))

import { AppApiProvider, useChatLauncher } from '../app-sdk'
import TrustAppModal, { type TrustAppTarget } from '../components/appstore/TrustAppModal'
import { trustGrants } from '../components/appstore/trustGrants'
import { knownChatLaunch } from '../components/appstore/chatLaunch'
import { ChatLaunchBox } from '../pages/AppDetailGrantBoxes'

const LAUNCH = { keyPrefix: 'notes-followup-', agents: ['notes-reviewer', 'kirocrew'], folder: 'Notes', memoryModes: ['persistent'] }

function launcher(allowedApiPaths: string[] = ['/api/apps/notes/*'], sessionKey?: string) {
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <AppApiProvider appName="notes" appVersion="1" allowedApiPaths={allowedApiPaths} allowedEvents={[]}
      subscribeFn={() => () => {}} navigateFn={() => {}} notifyFn={() => {}} sessionKey={sessionKey}>
      {children}
    </AppApiProvider>
  )
  return renderHook(() => useChatLauncher(), { wrapper })
}

function stubFetch(status: number, body: unknown) {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => { vi.unstubAllGlobals() })

describe('useChatLauncher().createChat', () => {
  it('creates through the host with the app named, without a declared /api/chat/slots', async () => {
    const fetchMock = stubFetch(200, { key: 'notes-followup-abc' })
    const { result } = launcher()
    const key = await result.current.createChat({
      slotKey: 'notes-followup-abc', agent: 'notes-reviewer', title: 'Follow-up', folderId: 'f1',
    })
    expect(key).toBe('notes-followup-abc')
    const [path, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(path).toBe('/api/chat/slots')
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({
      name: 'notes-followup-abc', agent: 'notes-reviewer', memory_mode: 'persistent',
      title: 'Follow-up', folder_id: 'f1', app_launch: 'notes', embed_app: 'notes',
    })
  })

  it('sends only the launch fields, and the host session binding when there is one', async () => {
    const fetchMock = stubFetch(200, { key: 'notes-followup-x' })
    const { result } = launcher([], 'dashboard:chat-1-1')
    await result.current.createChat({ slotKey: 'notes-followup-x', agent: 'kirocrew', memoryMode: 'incognito' })
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(JSON.parse(String(init.body))).toEqual({
      name: 'notes-followup-x', agent: 'kirocrew', memory_mode: 'incognito', app_launch: 'notes', embed_app: 'notes',
    })
    expect(new Headers(init.headers).get('X-Session-Key')).toBe('dashboard:chat-1-1')
  })

  it('rejects with the gateway refusal and its status outside the declaration', async () => {
    stubFetch(403, { error: 'this app may not open that chat', code: 'app_chat_launch_refused' })
    const { result } = launcher()
    await expect(result.current.createChat({ slotKey: 'chat-1-2', agent: 'kirocrew' }))
      .rejects.toMatchObject({ status: 403, body: expect.stringContaining('app_chat_launch_refused') })
  })

  it('answers the requested key when the create names none', async () => {
    stubFetch(200, {})
    const { result } = launcher()
    await expect(result.current.createChat({ slotKey: 'notes-followup-y', agent: 'kirocrew' }))
      .resolves.toBe('notes-followup-y')
  })
})

describe('chatLaunch disclosure', () => {
  const BASE: TrustAppTarget = {
    name: 'notes', displayName: 'Notes', origin: 'registry',
    trustRepository: 'https://git.example.test/org/notes', retryInstalls: true,
  }

  function renderModal(app: TrustAppTarget) {
    render(<TrustAppModal app={app} pending={false} failed={false} granted={false} onCancel={() => {}} onConfirm={() => {}} />)
    return within(screen.getByRole('dialog'))
  }

  it('names the agents, the folder, the key prefix and the memory modes on the trust prompt', () => {
    const dialog = renderModal({ ...BASE, ...trustGrants({ chatLaunch: LAUNCH }) })
    const box = within(dialog.getByTestId('trust-chat-launch'))
    expect(box.getByText('components.appstore.trustAppModal.chat_launch_heading')).toBeTruthy()
    expect(box.getByText('components.appstore.trustAppModal.chat_launch_desc notes-reviewer, kirocrew | Notes')).toBeTruthy()
    expect(box.getByText(
      'components.appstore.trustAppModal.chat_launch_scope notes-followup- | components.appstore.trustAppModal.chat_launch_memory_persistent',
    )).toBeTruthy()
  })

  it('names every declared memory mode, so an incognito or temporary chat is not installed unseen', () => {
    const dialog = renderModal({ ...BASE, ...trustGrants({ chatLaunch: { ...LAUNCH, memoryModes: ['temporary', 'persistent', 'incognito'] } }) })
    const scope = within(dialog.getByTestId('trust-chat-launch')).getByText(/chat_launch_scope/).textContent ?? ''
    expect(scope).toContain('notes-followup-')
    const named = ['persistent', 'incognito', 'temporary'].map(mode => scope.indexOf(`chat_launch_memory_${mode}`))
    expect(named.every(at => at >= 0)).toBe(true)
    // In the gateway's order, whatever order the manifest lists them in.
    expect([...named].sort((a, b) => a - b)).toEqual(named)
  })

  it('words an unfiled declaration without a folder, and shows nothing for none', () => {
    const { folder: _folder, ...unfiled } = LAUNCH
    const dialog = renderModal({ ...BASE, ...trustGrants({ chatLaunch: unfiled }) })
    expect(dialog.getByText('components.appstore.trustAppModal.chat_launch_desc_unfiled notes-reviewer, kirocrew')).toBeTruthy()
    screen.getByRole('dialog').remove()
    expect(renderModal(BASE).queryByTestId('trust-chat-launch')).toBeNull()
  })

  it('reads a malformed declaration as none, as the gateway grants nothing for it', () => {
    for (const bad of [
      true, 'x', [], {}, { keyPrefix: 'a-' }, { keyPrefix: 'a-', agents: [] }, { keyPrefix: 'a-', agents: [1] },
      { keyPrefix: 'a-', agents: ['x'], folder: 3 },
      { keyPrefix: 'a-', agents: ['x'], memoryModes: [] },
      { keyPrefix: 'a-', agents: ['x'], memoryModes: ['forever'] },
      { keyPrefix: 'a-', agents: ['x'], memoryModes: 'persistent' },
    ]) {
      expect(knownChatLaunch(bad), JSON.stringify(bad)).toBeUndefined()
    }
    expect(knownChatLaunch(LAUNCH)).toEqual({
      keyPrefix: 'notes-followup-', agents: ['notes-reviewer', 'kirocrew'], folder: 'Notes', memoryModes: ['persistent'],
    })
    // Omitted, the gateway's default: persistent only.
    expect(knownChatLaunch({ keyPrefix: 'a-', agents: ['x'] })?.memoryModes).toEqual(['persistent'])
  })

  it('names the declaration on the detail page, and renders nothing without one', () => {
    const { container } = render(<ChatLaunchBox declared={undefined} />)
    expect(container.innerHTML).toBe('')
    render(<ChatLaunchBox declared={LAUNCH} />)
    const box = within(screen.getByTestId('chat-launch'))
    expect(box.getByText('pages.appDetailPage.chat_launch_desc notes-reviewer, kirocrew | Notes')).toBeTruthy()
    expect(box.getByText(
      'components.appstore.trustAppModal.chat_launch_scope notes-followup- | components.appstore.trustAppModal.chat_launch_memory_persistent',
    )).toBeTruthy()
    expect(box.getByText('chatLaunch')).toBeTruthy()
  })
})
