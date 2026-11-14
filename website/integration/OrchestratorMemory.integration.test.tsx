import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { renderWithProviders } from './helpers'
import { server } from './mocks/server'
import { AgentBackendTab } from '../src/pages/developer/AgentBackendTab'

vi.mock('../src/pages/developer/KiroSignInCard', () => ({ KiroSignInCard: () => null }))

let agent: { acp_backend: string; orchestrator_memory?: boolean }
let writes: unknown[]

beforeEach(() => {
  agent = { acp_backend: 'kas' }
  writes = []
  server.use(
    http.get('/api/config/kirocrew', () => HttpResponse.json({ agent })),
    http.get('/api/config/schema', () => HttpResponse.json({ entries: [
      { path: 'agent.acp_backend', type: 'enum', enumValues: ['kas', 'kiro', 'claude'] },
      { path: 'agent.orchestrator_memory', type: 'boolean', defaultValue: false },
    ] })),
    http.get('/api/acp-backends', () => HttpResponse.json({ backends: [] })),
    http.patch('/api/config/kirocrew', async ({ request }) => {
      const body = await request.json() as { path: string; value: boolean }
      writes.push(body)
      if (body.path !== 'agent.orchestrator_memory') return new HttpResponse(null, { status: 400 })
      agent = { ...agent, orchestrator_memory: body.value }
      return HttpResponse.json({ ok: true })
    }),
  )
})

const toggle = () => screen.getByRole('switch', { name: 'Orchestrator memory' })
const open = (route = '/settings/agent') => renderWithProviders(<AgentBackendTab />, { route })

async function ready() {
  await screen.findByRole('switch', { name: 'Orchestrator memory' })
  await waitFor(() => expect(toggle()).toBeEnabled())
}

describe('Orchestrator memory setting', () => {
  it('labels the preview and explains read-only memory without opting in when the info tip opens', async () => {
    const user = userEvent.setup()
    open()
    await ready()
    const row = toggle().closest('[data-setting-key]') as HTMLElement
    const label = within(row).getByText('Orchestrator memory')
    const badge = within(row).getByText('Preview')
    const info = within(row).getByRole('button')
    expect(badge).toBeVisible()
    expect(label.parentElement).toContainElement(badge)
    expect(label.parentElement).toContainElement(info)
    expect(label.compareDocumentPosition(badge) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(badge.compareDocumentPosition(info) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(toggle()).toHaveAccessibleName('Orchestrator memory')
    expect(within(row).getByText('Use existing Kiro memories in personal chats. Changes apply when an agent session starts; start a new chat to use the updated setting.')).toBeVisible()

    await user.click(info)
    expect(await screen.findByRole('tooltip')).toHaveTextContent("Uses memories saved in other Kiro clients alongside Kiro Crew's existing memory. Kiro memories are read-only here. Kiro Crew's learning behavior and each Bot's separate memory are unchanged. Kiro memories are available in Incognito chats, but excluded from Temporary chats.")
    expect(toggle()).toHaveAttribute('aria-checked', 'false')
    expect(writes).toEqual([])
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('tooltip')).toBeNull()
    expect(toggle()).toHaveAttribute('aria-checked', 'false')
  })

  it('defaults off, saves both values, and reads the saved value after reopening', async () => {
    const user = userEvent.setup()
    const view = open()
    await ready()
    expect(toggle()).toHaveAttribute('aria-checked', 'false')
    await user.click(toggle())
    await waitFor(() => expect(toggle()).toHaveAttribute('aria-checked', 'true'))
    expect(writes).toEqual([{ path: 'agent.orchestrator_memory', value: true }])
    expect(agent.acp_backend).toBe('kas')

    view.unmount()
    open()
    await ready()
    expect(toggle()).toHaveAttribute('aria-checked', 'true')
    await user.click(toggle())
    await waitFor(() => expect(toggle()).toHaveAttribute('aria-checked', 'false'))
    expect(writes).toEqual([
      { path: 'agent.orchestrator_memory', value: true },
      { path: 'agent.orchestrator_memory', value: false },
    ])
  })

  it('keeps the saved value and offers a retry after a rejected save', async () => {
    const user = userEvent.setup()
    server.use(http.patch('/api/config/kirocrew', () => HttpResponse.json({ error: 'refused' }, { status: 403 }), { once: true }))
    open()
    await ready()
    await user.click(toggle())
    expect(await screen.findByText('Could not save orchestrator memory. Try again.')).toBeInTheDocument()
    expect(toggle()).toHaveAttribute('aria-checked', 'false')
    await user.click(toggle())
    await waitFor(() => expect(toggle()).toHaveAttribute('aria-checked', 'true'))
    expect(screen.queryByText('Could not save orchestrator memory. Try again.')).toBeNull()
  })

  it('shows only in KAS detail and opens it from the setting deep link without switching harness', async () => {
    agent = { acp_backend: 'claude', orchestrator_memory: true }
    const user = userEvent.setup()
    open('/settings/agent?highlight=key%3Aagent.orchestrator_memory')
    await ready()
    expect(screen.getByRole('radio', { name: 'Kiro Agent' })).toBeChecked()
    expect(toggle()).toHaveAttribute('aria-checked', 'true')
    await user.click(screen.getByRole('radio', { name: 'Claude Code' }))
    expect(screen.queryByRole('switch', { name: 'Orchestrator memory' })).toBeNull()
    await user.click(screen.getByRole('radio', { name: 'Kiro CLI' }))
    expect(screen.queryByRole('switch', { name: 'Orchestrator memory' })).toBeNull()
    expect(writes).toEqual([])
    expect(agent.acp_backend).toBe('claude')
  })
})
