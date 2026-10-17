import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const LABEL = 'New Session Folders'
const PATH = 'dashboard.session_folder_root'

// A stateful stand-in for the gateway: a save changes what the next GET returns,
// and a refused path answers 400 the way the dashboard-config PUT does.
const { server, updateDashboardConfigMock } = vi.hoisted(() => {
  const server = { root: '', refuse: '' as '' | 'path' | 'write' }
  return {
    server,
    updateDashboardConfigMock: vi.fn(async (body: { session_folder_root?: string }) => {
      const { ApiError } = await import('../api/apiError')
      if (server.refuse === 'path') {
        throw new ApiError(400, 'refused', JSON.stringify({ error: 'refused', code: 'invalid_session_folder_root' }))
      }
      if (server.refuse === 'write') {
        throw new ApiError(500, 'failed', JSON.stringify({ error: 'failed', code: 'dashboard_config_write_failed' }))
      }
      if (typeof body.session_folder_root === 'string') server.root = body.session_folder_root
      return { ok: true }
    }),
  }
})

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: () => Promise.resolve({ restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more', session_folder_root: server.root }),
    voiceConfig: () => Promise.resolve({ enabled: false, voice: 'Ruth', engine: 'neural', rate: '100%', autoSpeak: false, aws_profile: '', region: '' }),
    sttConfig: () => Promise.resolve({ enabled: false, provider: '', model: '', available: false, streaming: false, transcribe_region: '', transcribe_profile: '', language_code: 'en-US', models: {}, language_codes: [] }),
    kirocrewConfig: () => Promise.resolve({}),
    models: () => Promise.resolve([{ model_name: 'auto', description: 'Default' }]),
    patchConfig: () => Promise.resolve({}),
    updateDashboardConfig: updateDashboardConfigMock,
    updateVoiceConfig: () => Promise.resolve({}),
    updateSttConfig: () => Promise.resolve({}),
    tipsStatus: () => Promise.resolve({ enabled_config: true, opted_out: false }),
    tipsFeedback: () => Promise.resolve({ ok: true }),
    featureVideoStatus: () => Promise.resolve({
      enabled: true, download_enabled: false, release: 'r1',
      cached: 0, total: 0, downloading: null,
    }),
    featureVideoFetchAll: () => Promise.resolve({ ok: true }),
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'

import { Provider } from 'react-redux'
import { createTestStore } from './helpers'

function renderSessions(root = '') {
  server.root = root
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<MemoryRouter initialEntries={['/settings?tab=chat&sub=sessions']}><Provider store={createTestStore()}><QueryClientProvider client={qc}><ChatPanel /></QueryClientProvider></Provider></MemoryRouter>)
}

/** The row's text box, once the dashboard config has loaded and enabled it. */
async function field(): Promise<HTMLInputElement> {
  await screen.findByText(LABEL)
  const row = document.querySelector(`[data-setting-key="${PATH}"]`) as HTMLElement
  expect(row).not.toBeNull()
  const input = row.querySelector('input') as HTMLInputElement
  await waitFor(() => expect(input).not.toBeDisabled())
  return input
}

describe('ChatPanel settings -- new session folders', () => {
  beforeEach(() => {
    updateDashboardConfigMock.mockClear()
    server.refuse = ''
    localStorage.clear()
  })

  it('shows the saved root and saves an edited one on blur', async () => {
    renderSessions('/home/me/sessions')
    const input = await field()
    await waitFor(() => expect(input.value).toBe('/home/me/sessions'))

    fireEvent.change(input, { target: { value: '  /home/me/other  ' } })
    fireEvent.blur(input)

    await waitFor(() => expect(updateDashboardConfigMock).toHaveBeenCalledWith({ session_folder_root: '/home/me/other' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')

    fireEvent.change(input, { target: { value: '/home/me/third' } })
    expect(screen.queryByText('Saved')).toBeNull()
  })

  it('saves nothing for a blur without an edit', async () => {
    renderSessions('/home/me/sessions')
    const input = await field()
    fireEvent.blur(input)
    fireEvent.change(input, { target: { value: '/home/me/sessions' } })
    fireEvent.blur(input)
    expect(updateDashboardConfigMock).not.toHaveBeenCalled()
  })

  it('clearing the field turns the feature off', async () => {
    renderSessions('/home/me/sessions')
    const input = await field()
    await waitFor(() => expect(input.value).toBe('/home/me/sessions'))
    fireEvent.change(input, { target: { value: '' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(updateDashboardConfigMock).toHaveBeenCalledWith({ session_folder_root: '' }))
  })

  it('keeps a refused path on screen and says why at the field', async () => {
    server.refuse = 'path'
    renderSessions()
    const input = await field()
    fireEvent.change(input, { target: { value: '/etc' } })
    fireEvent.blur(input)

    await screen.findByText(/Could not use that folder/)
    expect(input.value).toBe('/etc')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    const notice = document.getElementById(input.getAttribute('aria-describedby') ?? '')
    expect(notice).not.toBeNull()
    expect(notice?.textContent).toMatch(/Could not use that folder/)
    expect(notice?.closest(`[data-setting-key="${PATH}"]`)).not.toBeNull()

    fireEvent.change(input, { target: { value: '/etc/x' } })
    expect(screen.queryByText(/Could not use that folder/)).toBeNull()
    expect(input).not.toHaveAttribute('aria-invalid')
  })

  it('reports a failed write as a save failure, not as a bad path', async () => {
    server.refuse = 'write'
    renderSessions()
    const input = await field()
    fireEvent.change(input, { target: { value: '/home/me/chats' } })
    fireEvent.blur(input)

    expect(await screen.findByText('Failed to save dashboard config')).toBeInTheDocument()
    expect(screen.queryByText(/Could not use that folder/)).toBeNull()
    expect(input.value).toBe('/home/me/chats')
  })

  it('shows an example path and says that empty turns it off', async () => {
    renderSessions()
    const input = await field()
    expect(input.placeholder).toBe('For example, ~/chats')
    expect(screen.getByText(/Leave empty to turn this off/)).toBeInTheDocument()
  })
})
