/**
 * Settings > Developer > Agent MCP servers: the rows read the default agent's spec
 * (on through the mount-only record, added by hand, or off); Add records each set
 * that is off; Remove asks first and deletes both records; a refusal that already
 * matches the action counts as done, and any other one is shown with its reason.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, focusManager } from '@tanstack/react-query'
import React from 'react'

vi.mock('../../api/client', () => ({
  api: {
    agentDetail: vi.fn(),
    kirocrewConfig: vi.fn(),
    defaultMcpSetAdd: vi.fn(),
    defaultMcpSetRemove: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { DefaultMcpGrantsSection } from './DefaultMcpGrantsSection'

const DASH = 'kirocrew-dashboard'
const DEBUG = 'kirocrew-debug'
const STAMP = 'x-kirocrew-card-addition'

type Mount = 'recorded' | 'by_hand' | 'off'
const spec = (dash: Mount, debug: Mount = dash) => {
  const mcpServers: Record<string, unknown> = { 'kirocrew-core': { command: 'kirocrew' } }
  const tools: string[] = ['@kirocrew-core']
  for (const [name, mount] of [[DASH, dash], [DEBUG, debug]] as const) {
    if (mount === 'off') continue
    mcpServers[name] = mount === 'recorded' ? { command: 'kirocrew', [STAMP]: true } : { command: 'kirocrew' }
    tools.push(`@${name}`)
  }
  return { name: 'kirocrew', mcpServers, tools }
}
const refusal = (status: number, code: string, message: string) =>
  new ApiError(status, message, JSON.stringify({ error: message, code }))

let qc: QueryClient
const wrap = (ui: React.ReactElement) => render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
const addButton = () => screen.getByRole('button', { name: /Add dashboard and debug MCPs/i }) as HTMLButtonElement
const removeButton = () => screen.getAllByRole('button', { name: /^Remove dashboard and debug MCPs$/i })[0] as HTMLButtonElement
const stateText = (name: string) => screen.getByTestId(`default-mcp-state-${name}`).textContent ?? ''

beforeEach(() => {
  vi.clearAllMocks()
  // The app's own default: nothing goes stale on its own.
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } })
  vi.mocked(api.agentDetail).mockResolvedValue(spec('off'))
  vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { session_control: true } })
  vi.mocked(api.defaultMcpSetAdd).mockResolvedValue({ sessions_reset: 0 })
  vi.mocked(api.defaultMcpSetRemove).mockResolvedValue({ sessions_reset: 0 })
})

describe('DefaultMcpGrantsSection', () => {
  it('reads each row from the default agent spec', async () => {
    vi.mocked(api.agentDetail).mockResolvedValue(spec('recorded', 'by_hand'))
    wrap(<DefaultMcpGrantsSection />)
    await waitFor(() => expect(stateText(DASH)).toMatch(/^On the default agent/))
    expect(stateText(DEBUG)).toMatch(/Added by hand/)
    expect(api.agentDetail).toHaveBeenCalledWith('kirocrew')
  })

  it('adds only the sets that are off and says so', async () => {
    vi.mocked(api.agentDetail).mockResolvedValue(spec('off', 'by_hand'))
    wrap(<DefaultMcpGrantsSection />)
    await waitFor(() => expect(addButton().disabled).toBe(false))
    vi.mocked(api.agentDetail).mockResolvedValue(spec('recorded', 'by_hand'))
    fireEvent.click(addButton())
    await waitFor(() => expect(screen.getByTestId('default-mcp-grants-done').textContent).toMatch(/Added/))
    expect(api.defaultMcpSetAdd).toHaveBeenCalledTimes(1)
    expect(api.defaultMcpSetAdd).toHaveBeenCalledWith(DASH)
    await waitFor(() => expect(stateText(DASH)).toMatch(/^On the default agent/))
  })

  it('reads an add the record already holds as done', async () => {
    vi.mocked(api.defaultMcpSetAdd).mockRejectedValueOnce(refusal(409, 'already_added', 'already added'))
    wrap(<DefaultMcpGrantsSection />)
    await waitFor(() => expect(addButton().disabled).toBe(false))
    fireEvent.click(addButton())
    await waitFor(() => expect(screen.getByTestId('default-mcp-grants-done')).toBeTruthy())
    expect(api.defaultMcpSetAdd).toHaveBeenCalledTimes(2)
    expect(screen.queryByTestId('default-mcp-grants-save-error')).toBeNull()
  })

  it('asks before removing, names the hand-added case, and deletes both records', async () => {
    vi.mocked(api.agentDetail).mockResolvedValue(spec('recorded'))
    vi.mocked(api.defaultMcpSetRemove).mockRejectedValueOnce(refusal(409, 'not_found', 'not found'))
    wrap(<DefaultMcpGrantsSection />)
    await waitFor(() => expect(removeButton().disabled).toBe(false))
    fireEvent.click(removeButton())
    await screen.findByText(/including a set you approved from a chat/)
    expect(screen.getByText(/A server you added to the agent config by hand stays/)).toBeTruthy()
    expect(api.defaultMcpSetRemove).not.toHaveBeenCalled()
    fireEvent.click(screen.getAllByRole('button', { name: /^Remove dashboard and debug MCPs$/i }).at(-1)!)
    await waitFor(() => expect(screen.getByTestId('default-mcp-grants-done').textContent).toMatch(/Removed/))
    expect(vi.mocked(api.defaultMcpSetRemove).mock.calls).toEqual([[DASH], [DEBUG]])
  })

  it('offers Remove only while a set came from the record', async () => {
    vi.mocked(api.agentDetail).mockResolvedValue(spec('by_hand'))
    wrap(<DefaultMcpGrantsSection />)
    await waitFor(() => expect(stateText(DASH)).toMatch(/Added by hand/))
    expect(removeButton().disabled).toBe(true)
    expect(addButton().disabled).toBe(true)
  })

  it("shows the gateway's reason when an add is refused", async () => {
    vi.mocked(api.defaultMcpSetAdd).mockRejectedValue(
      refusal(503, 'spec_not_rewritten', 'the agent settings file could not be rewritten'),
    )
    wrap(<DefaultMcpGrantsSection />)
    await waitFor(() => expect(addButton().disabled).toBe(false))
    fireEvent.click(addButton())
    const notice = await screen.findByTestId('default-mcp-grants-save-error')
    expect(notice.textContent).toMatch(/Couldn't add them\. the agent settings file could not be rewritten/)
  })

  it('says the gateway was not reached when no answer came back', async () => {
    vi.mocked(api.defaultMcpSetAdd).mockRejectedValue(new TypeError('Failed to fetch'))
    wrap(<DefaultMcpGrantsSection />)
    await waitFor(() => expect(addButton().disabled).toBe(false))
    fireEvent.click(addButton())
    const notice = await screen.findByTestId('default-mcp-grants-save-error')
    expect(notice.textContent).toMatch(/Couldn't reach the gateway/)
  })

  it('names agent.session_control when it is off', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { session_control: false } })
    wrap(<DefaultMcpGrantsSection />)
    const note = await screen.findByTestId('default-mcp-session-control-note')
    expect(note.textContent).toMatch(/agent\.session_control/)
  })

  it('says the Session Control read failed and retries it', async () => {
    vi.mocked(api.kirocrewConfig).mockRejectedValue(refusal(500, 'internal', 'config read failed'))
    wrap(<DefaultMcpGrantsSection />)
    const notice = await screen.findByTestId('default-mcp-config-error')
    expect(notice.textContent).toMatch(/Session Control/)
    expect(screen.queryByTestId('default-mcp-session-control-note')).toBeNull()
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { session_control: false } })
    fireEvent.click(screen.getByTestId('default-mcp-config-retry'))
    await screen.findByTestId('default-mcp-session-control-note')
    expect(api.kirocrewConfig).toHaveBeenCalledTimes(2)
    expect(screen.queryByTestId('default-mcp-config-error')).toBeNull()
  })

  it('says nothing about Session Control until the config read answers', async () => {
    let answer: (cfg: unknown) => void = () => {}
    vi.mocked(api.kirocrewConfig).mockReturnValue(new Promise(resolve => { answer = resolve }))
    wrap(<DefaultMcpGrantsSection />)
    await waitFor(() => expect(stateText(DASH)).toMatch(/Not on the default agent/))
    expect(screen.queryByTestId('default-mcp-session-control-note')).toBeNull()
    expect(screen.queryByTestId('default-mcp-config-error')).toBeNull()
    act(() => { answer({ agent: { session_control: false } }) })
    await screen.findByTestId('default-mcp-session-control-note')
  })

  it('reads the spec again when the window regains focus', async () => {
    wrap(<DefaultMcpGrantsSection />)
    await waitFor(() => expect(stateText(DASH)).toMatch(/Not on the default agent/))
    // Another tab added both sets.
    vi.mocked(api.agentDetail).mockResolvedValue(spec('recorded'))
    act(() => {
      focusManager.setFocused(false)
      focusManager.setFocused(true)
    })
    await waitFor(() => expect(stateText(DASH)).toMatch(/^On the default agent/))
    focusManager.setFocused(undefined)
  })
})
