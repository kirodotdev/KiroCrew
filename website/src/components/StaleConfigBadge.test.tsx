import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

// `real` routes the header button through the actual hook, so the failure
// tests exercise the real request and its alert-or-notice choice.
const actions = vi.hoisted(() => ({ reload: vi.fn(), real: false }))
vi.mock('../hooks/useSessionActions', async importOriginal => {
  const mod = await importOriginal<typeof import('../hooks/useSessionActions')>()
  return {
    ...mod,
    useSessionActions: (mode?: string) => (actions.real ? mod.useSessionActions(mode) : { reload: actions.reload }),
  }
})

import StaleConfigBadge, { StaleConfigCue, StaleConfigReloadButton } from './StaleConfigBadge'
import { ChatHeaderMenu } from '../pages/chat/ChatHeaderMenu'
import { renderWithProviders, createTestStore } from '../test/helpers'
import { sseSlots } from '../store/dashboardSlice'
import { sseSubagentSpawn } from '../store/chatSlice'
import type { ChatSlot } from '../types'

const STALE = { key: 'zzq-slot', messages: 0, running: false, config_stale: true, config_stale_inputs: '.kiro/settings/mcp.json' } as ChatSlot

function renderButton(slot: ChatSlot = STALE, onError?: (message: string) => void) {
  const store = createTestStore()
  store.dispatch(sseSlots([slot]))
  return renderWithProviders(<StaleConfigReloadButton slot={slot} onError={onError} />, { store })
}

beforeEach(() => {
  actions.reload.mockReset()
  actions.real = false
})

describe('StaleConfigBadge (sidebar mark)', () => {
  it('renders nothing for a chat on its current config', () => {
    const { container } = render(<StaleConfigBadge slot={{ config_stale: false, config_stale_inputs: '' }} />)
    expect(container).toBeEmptyDOMElement()
    render(<StaleConfigBadge slot={undefined} />)
    expect(screen.queryByTestId('stale-config-badge')).toBeNull()
  })

  it('is an alert glyph, not a refresh arrow that reads as syncing', () => {
    render(<StaleConfigBadge slot={{ config_stale: true }} />)
    const mark = screen.getByTestId('stale-config-badge')
    expect(mark.querySelector('.lucide-circle-alert')).not.toBeNull()
    expect(mark.querySelector('.lucide-refresh-cw')).toBeNull()
  })

  it('names what changed and the remedy in plain words', () => {
    const { unmount } = render(<StaleConfigBadge slot={{ config_stale: true, config_stale_inputs: '.kiro/settings/mcp.json' }} />)
    expect(screen.getByTestId('stale-config-badge')).toHaveAttribute(
      'title',
      '.kiro/settings/mcp.json changed since this session started. Reload the session to apply.',
    )
    unmount()
    render(<StaleConfigBadge slot={{ config_stale: true }} />)
    const title = screen.getByTestId('stale-config-badge').getAttribute('title')
    expect(title).toBe("This session's config changed since it started. Reload the session to apply.")
    expect(title).not.toMatch(/stale/i)
  })
})

describe('StaleConfigReloadButton (chat header)', () => {
  it('reloads the session in one click', () => {
    renderButton()
    const btn = screen.getByRole('button', { name: 'Config changed · Reload' })
    expect(btn).toHaveTextContent('Config changed · Reload')
    expect(btn).toHaveAttribute('title', '.kiro/settings/mcp.json changed since this session started. Reload the session to apply.')
    fireEvent.click(btn)
    expect(actions.reload).toHaveBeenCalledWith('zzq-slot', expect.objectContaining({ onError: expect.any(Function) }))
  })

  it('is disabled while a turn runs', () => {
    renderButton({ ...STALE, running: true })
    const btn = screen.getByTestId('stale-config-reload')
    expect(btn).toBeDisabled()
    fireEvent.click(btn)
    expect(actions.reload).not.toHaveBeenCalled()
  })

  it('is disabled while sub-agent children are attached', () => {
    const store = createTestStore()
    store.dispatch(sseSlots([STALE]))
    store.dispatch(sseSubagentSpawn({ slot: 'zzq-slot', id: 'sa-1', task: 't', agent: 'a' }))
    renderWithProviders(<StaleConfigReloadButton slot={STALE} />, { store })
    expect(screen.getByTestId('stale-config-reload')).toBeDisabled()
  })

  it('keeps its place at every width: no order classes, only the words drop on a phone', () => {
    renderButton()
    const btn = screen.getByTestId('stale-config-reload')
    for (const el of [btn, ...btn.querySelectorAll('*')]) {
      expect(String(el.getAttribute('class') ?? '')).not.toMatch(/(^|\s)(sm|md|lg):order-|(^|\s)order-/)
    }
    expect(screen.getByText('Config changed · Reload')).toHaveClass('hidden', 'sm:inline')
    const short = screen.getByTestId('stale-config-reload-short')
    expect(short).toHaveClass('sm:hidden')
    expect(short).toHaveTextContent('Config · Reload')
    // A touch user has no tooltip: the phone form keeps the alert glyph as its cue.
    expect(short.querySelector('.lucide-circle-alert')).not.toBeNull()
    expect(btn).toHaveAccessibleName('Config changed · Reload')
  })

  it('renders nothing once the config is current again', () => {
    renderButton({ ...STALE, config_stale: false })
    expect(screen.queryByTestId('stale-config-reload')).toBeNull()
  })

  it('is not part of the session-menu trigger', () => {
    const store = createTestStore()
    store.dispatch(sseSlots([STALE]))
    renderWithProviders(<ChatHeaderMenu activeSlot="zzq-slot" agent="kirocrew" mode="" triggerLabel="Title" />, { store })
    const trigger = screen.getByTestId('session-title-menu')
    expect(trigger.querySelector('[data-testid^="stale-config"]')).toBeNull()
    expect(trigger).not.toHaveTextContent(/config/i)
  })
})

describe('StaleConfigReloadButton failure (real reload request)', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>
  let alertSpy: ReturnType<typeof vi.spyOn>
  const reloadCalls = () => fetchSpy.mock.calls.filter(([url]) => String(url).endsWith('/zzq-slot/reload'))
  const respond = (status: number, body: unknown) => new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  })

  beforeEach(() => {
    actions.real = true
    fetchSpy = vi.spyOn(globalThis, 'fetch')
    alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {})
  })
  afterEach(() => {
    fetchSpy.mockRestore()
    alertSpy.mockRestore()
  })

  it('shows a sub-agents 409 through ErrorNotice beside the button, with no alert', async () => {
    fetchSpy.mockImplementation(async () => respond(409, { error: 'sub-agents are running', code: 'slot_subagents_running' }))
    renderButton()
    fireEvent.click(screen.getByTestId('stale-config-reload'))
    const notice = await screen.findByTestId('stale-config-reload-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent('Sub-agents are still working on this session. Try again when they finish.')
    expect(notice).toHaveTextContent('Ask the agent')
    expect(alertSpy).not.toHaveBeenCalled()
    // A failed press can be tried again.
    expect(screen.getByTestId('stale-config-reload')).toBeEnabled()
  })

  it('shows the generic copy for any other failure', async () => {
    fetchSpy.mockImplementation(async () => respond(409, { error: 'turn in flight', code: 'turn_in_flight' }))
    renderButton()
    fireEvent.click(screen.getByTestId('stale-config-reload'))
    const notice = await screen.findByTestId('stale-config-reload-error')
    expect(notice).toHaveTextContent('Session reload failed.')
    expect(notice).not.toHaveTextContent('Sub-agents')
    expect(alertSpy).not.toHaveBeenCalled()
  })

  it('hands a failure to the host\'s onError instead of rendering a notice of its own', async () => {
    fetchSpy.mockImplementation(async () => respond(409, { error: 'sub-agents are running', code: 'slot_subagents_running' }))
    const onError = vi.fn()
    renderButton(STALE, onError)
    fireEvent.click(screen.getByTestId('stale-config-reload'))
    await waitFor(() => expect(onError).toHaveBeenCalledWith('Sub-agents are still working on this session. Try again when they finish.'))
    expect(screen.queryByTestId('stale-config-reload-error')).toBeNull()
    expect(alertSpy).not.toHaveBeenCalled()
    // Still retryable, and the generic copy reaches the host the same way.
    await waitFor(() => expect(screen.getByTestId('stale-config-reload')).toBeEnabled())
    fetchSpy.mockImplementation(async () => respond(409, { error: 'turn in flight', code: 'turn_in_flight' }))
    fireEvent.click(screen.getByTestId('stale-config-reload'))
    await waitFor(() => expect(String(onError.mock.lastCall?.[0])).toMatch(/^Session reload failed\./))
    expect(onError).toHaveBeenCalledTimes(2)
  })

  it('clears the notice when a following press succeeds', async () => {
    fetchSpy.mockImplementation(async () => respond(409, { error: 'turn in flight', code: 'turn_in_flight' }))
    renderButton()
    fireEvent.click(screen.getByTestId('stale-config-reload'))
    await screen.findByTestId('stale-config-reload-error')
    fetchSpy.mockImplementation(async () => respond(200, { ok: true }))
    fireEvent.click(screen.getByTestId('stale-config-reload'))
    await waitFor(() => expect(screen.queryByTestId('stale-config-reload-error')).toBeNull())
    await waitFor(() => expect(reloadCalls()).toHaveLength(2))
    await waitFor(() => expect(screen.getByTestId('stale-config-reload')).toBeEnabled())
    expect(screen.queryByTestId('stale-config-reload-error')).toBeNull()
  })

  it('sends one request for a double press', async () => {
    let answer: (r: Response) => void = () => {}
    fetchSpy.mockImplementation(async input => String(input).endsWith('/zzq-slot/reload')
      ? new Promise<Response>(resolve => { answer = resolve })
      : respond(200, {}))
    renderButton()
    const btn = screen.getByTestId('stale-config-reload')
    fireEvent.click(btn)
    fireEvent.click(btn)
    await waitFor(() => expect(reloadCalls()).toHaveLength(1))
    expect(btn).toBeDisabled()
    answer(respond(200, { ok: true }))
    await waitFor(() => expect(btn).toBeEnabled())
    expect(reloadCalls()).toHaveLength(1)
  })
})

describe('StaleConfigCue (phone title trigger)', () => {
  it('is a glyph plus "Config · Reload", with the full tooltip as its accessible text, and no control', () => {
    render(<StaleConfigCue slot={{ config_stale: true, config_stale_inputs: '.kiro/settings/mcp.json' }} />)
    const cue = screen.getByTestId('stale-config-cue')
    expect(cue).toHaveTextContent('Config · Reload')
    expect(cue.querySelector('.lucide-circle-alert')).toHaveAttribute(
      'aria-label', '.kiro/settings/mcp.json changed since this session started. Reload the session to apply.',
    )
    expect(cue.tagName).toBe('SPAN')
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('renders nothing on a current config', () => {
    const { container } = render(<StaleConfigCue slot={{ config_stale: false }} />)
    expect(container).toBeEmptyDOMElement()
  })
})
