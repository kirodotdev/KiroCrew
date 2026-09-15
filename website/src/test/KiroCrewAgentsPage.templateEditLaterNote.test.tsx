/* The template "edit later" note is create-only.
 *
 * The note reassures a first-time creator that picking a template is not a
 * commitment. In the editor the reassurance would be noise — there the fields
 * being edited are themselves the answer — so TemplateField only renders it
 * when the create form passes `editLaterNote`. These tests pin both sides so
 * neither call site can drift: the note showing up in Edit would contradict
 * the editor's own affordances, and losing it from Create re-opens the
 * hesitation the note exists to remove.
 */
import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import KiroCrewAgentsPage from '../pages/KiroCrewAgentsPage'

globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as typeof ResizeObserver

vi.mock('../api/client', () => ({
  api: {
    kirocrewAgents: vi.fn().mockResolvedValue({
      agents: [{ name: 'oncall', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' }],
      default_agent: 'kirocrew',
    }),
    agentsInstalled: vi.fn().mockResolvedValue([{ name: 'kirocrew' }]),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [{ name: 'default', dir: 'workspace' }] }),
    kirocrewConfig: vi.fn().mockResolvedValue({ memory_stores: { default: {} } }),
    agentResolvedModel: vi.fn().mockResolvedValue({ model: '' }),
    createKirocrewAgent: vi.fn().mockResolvedValue({ ok: true }),
    updateKirocrewAgent: vi.fn().mockResolvedValue({}),
    deleteKirocrewAgent: vi.fn().mockResolvedValue({}),
    setDefaultAgent: vi.fn().mockResolvedValue({}),
    createWorkspace: vi.fn().mockResolvedValue({}),
    crons: vi.fn().mockResolvedValue({ jobs: [] }),
    webhooks: vi.fn().mockResolvedValue({ tokens: [] }),
    models: vi.fn().mockResolvedValue([]),
  },
}))

const NOTE = /switch this agent's template anytime/
const MEMBER_NOTE = /switch this member's template anytime/

describe('template edit-later note — create-only', () => {
  it('renders under the Agent Template field in the create sheet', async () => {
    renderWithProviders(<KiroCrewAgentsPage />)
    fireEvent.click(await screen.findByTestId('new-crew'))
    await screen.findByRole('combobox', { name: 'Agent Template' })
    expect(screen.getByText(NOTE)).toBeTruthy()
  })

  it('names the thing the way the surface that opened the form does: members-origin create says "member"', async () => {
    // The Crew Members roster's "+" lands here with ?new=1&from=members and
    // every string in that form says "member", never "agent" — the note is
    // one of those strings, so it follows `subject` like the field hints do.
    renderWithProviders(<KiroCrewAgentsPage />, { route: '/capabilities?tab=crews&new=1&from=members' })
    await screen.findByRole('dialog', { name: 'Add crew member' })
    expect(screen.getByText(MEMBER_NOTE)).toBeTruthy()
    expect(screen.queryByText(/this agent/)).toBeNull()
  })

  it('does not render in the edit sheet, template pane included', async () => {
    renderWithProviders(<KiroCrewAgentsPage />)
    fireEvent.click(await screen.findByTestId('crew-card'))
    // The template pane mounts the same TemplateField component the create
    // form uses — asserting there, not just on the default pane, is what
    // actually pins the editLaterNote prop split.
    fireEvent.click(await screen.findByTestId('crew-rail-template'))
    await screen.findByRole('combobox', { name: 'Agent Template' })
    expect(screen.queryByText(NOTE)).toBeNull()
  })
})
