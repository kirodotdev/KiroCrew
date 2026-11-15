/**
 * Tests for `useAppInfo().session` -- the chat session a host surface is bound to.
 *
 * A side-panel tab is mounted per chat and its scoped requests already carry that
 * chat's `X-Session-Key`; this field lets the tab's own code read the same key.
 * The contract: present, and equal to the key the scoped client sends, when the
 * host binds a session; ABSENT (not undefined) on a surface with no binding, such
 * as a routed `ui.pages` page.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, act } from '@testing-library/react'
import React from 'react'
import { AppApiProvider, useAppApi, useAppInfo, type AppApi, type AppInfo } from '../app-sdk/index'

function mount(sessionKey?: string): { info: AppInfo; api: AppApi } {
  let info: AppInfo | null = null
  let api: AppApi | null = null

  function Probe() {
    info = useAppInfo()
    api = useAppApi()
    return null
  }

  act(() => {
    render(
      React.createElement(
        AppApiProvider,
        {
          appName: 'test-app',
          appVersion: '1.0.0',
          allowedApiPaths: ['/api/apps/test-app/*'],
          allowedEvents: [],
          subscribeFn: () => () => {},
          navigateFn: () => {},
          notifyFn: () => {},
          sessionKey,
        },
        React.createElement(Probe),
      ),
    )
  })

  if (!info || !api) throw new Error('provider did not publish app info')
  return { info, api }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useAppInfo().session', () => {
  it('is absent on a surface with no session binding', () => {
    const { info } = mount()
    expect('session' in info).toBe(false)
    expect(info.session).toBeUndefined()
  })

  it('names the bound session', () => {
    const { info } = mount('dashboard:chat-2-1787502679')
    expect(info.session).toEqual({ sessionKey: 'dashboard:chat-2-1787502679' })
  })

  it('matches the key the scoped client sends as X-Session-Key', async () => {
    const fetchMock = vi.fn(async () => new Response('{}', {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }))
    vi.stubGlobal('fetch', fetchMock)

    const { info, api } = mount('dashboard:chat-7-1787502700')
    await api.get('/api/apps/test-app/ping')

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const init = (fetchMock.mock.calls[0] as unknown[])[1] as RequestInit
    const sent = new Headers(init.headers).get('X-Session-Key')
    expect(sent).toBe(info.session?.sessionKey)
  })

  it('is absent on a routed page, which sends the dashboard identity, not a chat', async () => {
    const fetchMock = vi.fn(async () => new Response('{}', {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }))
    vi.stubGlobal('fetch', fetchMock)

    // InstalledAppSurface (the /apps/<name> route) mounts AppHost with this key.
    const { info, api } = mount('dashboard:ui')
    expect('session' in info).toBe(false)

    await api.get('/api/apps/test-app/ping')
    const init = (fetchMock.mock.calls[0] as unknown[])[1] as RequestInit
    expect(new Headers(init.headers).get('X-Session-Key')).toBe('dashboard:ui')
  })

  it('leaves the rest of the app info untouched', () => {
    const { info } = mount('dashboard:chat-2-1787502679')
    expect(info.name).toBe('test-app')
    expect(info.version).toBe('1.0.0')
  })
})
