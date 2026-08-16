/**
 * The shared screenshot stub's /api/config/kirocrew fixture (#3696).
 *
 * `website/scripts/lib/stub-dashboard-api.mjs` used to answer every path
 * matching /config/ from a catch-all that returns `{}`. `/api/config/kirocrew`
 * matched, so `KiroCrewCfgTab` got `cfg.agents === undefined`,
 * `Object.entries(undefined)` threw, the app-shell error boundary swallowed it,
 * and the WHOLE PAGE rendered blank -- while the harness still exited 0 and
 * still wrote a PNG. The failure fails toward a FALSE PASS: a PR can cite a
 * screenshot of an error boundary as visual evidence.
 *
 * The stub is a Playwright-only module, so these assert the contract that
 * actually matters: the fixture it now serves is a shape this component can
 * render. A future field added to `KiroCrewCfg` and read unguarded (the exact
 * shape of the original bug) fails here rather than silently blanking ~140
 * capture harnesses.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import KiroCrewCfgTab from '../pages/overview/KiroCrewCfgTab'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import {
  KIROCREW_CONFIG_FIXTURE,
  AGENT_CONFIG_FIXTURE,
} from '../../scripts/lib/stub-dashboard-api.mjs'

vi.mock('../api/client')

vi.mock('../components/SimpleSelect', () => ({
  default: ({ options, value, 'aria-label': ariaLabel }: {
    options: string[]
    value: string
    'aria-label'?: string
  }) => (
    <div role="group" aria-label={ariaLabel}>
      {options.map(o => (
        <button key={o} type="button" role="option" aria-selected={o === value}>{o}</button>
      ))}
    </div>
  ),
}))

beforeEach(() => {
  vi.clearAllMocks()
  const m = vi.mocked(api)
  m.kirocrewConfig = vi.fn().mockResolvedValue(KIROCREW_CONFIG_FIXTURE)
  m.patchConfig = vi.fn().mockResolvedValue(KIROCREW_CONFIG_FIXTURE)
  m.saveKirocrewConfig = vi.fn().mockResolvedValue({ ok: true })
  m.themeBoot = vi.fn().mockResolvedValue({})
})

describe('stub-dashboard-api config fixture', () => {
  it('renders the config surface instead of blanking the page', async () => {
    renderWithProviders(<KiroCrewCfgTab />)
    // Reaching the first table means the three Object.entries() calls that
    // threw under the catch-all's `{}` all survived.
    expect(await screen.findByText('Kiro Crew Agents')).toBeInTheDocument()
    expect(screen.getAllByRole('table').length).toBeGreaterThanOrEqual(3)
  })

  it('populates every table the tab renders, not just the headers', async () => {
    renderWithProviders(<KiroCrewCfgTab />)
    await screen.findByText('Kiro Crew Agents')
    // An empty-but-present object would still render three tables with zero
    // rows -- a screenshot of empty tables is barely better evidence than a
    // blank page, so the fixture has to carry actual entries.
    const [agents, workspaces, stores] = screen.getAllByRole('table')
    for (const table of [agents, workspaces, stores]) {
      expect(table.querySelectorAll('tbody tr').length).toBeGreaterThan(0)
    }
  })

  it('spells the workspace directory the way the component reads it', async () => {
    // The hand-rolled per-harness copies of this fixture had drifted to `path`
    // where WorkspaceCfg declares `dir`, so the directory cell rendered blank
    // in screenshots meant to show it. One shared fixture is the fix; this
    // pins the field name so the drift cannot silently return.
    expect(Object.values(KIROCREW_CONFIG_FIXTURE.workspaces)[0]).toHaveProperty('dir')
    renderWithProviders(<KiroCrewCfgTab />)
    await screen.findByText('Kiro Crew Agents')
    expect(screen.getByText('~/.kiro/crew/workspace')).toBeInTheDocument()
  })

  it('carries the nested objects the tab dereferences unguarded', () => {
    // cfg.agent.*, cfg.session.* and cfg.memory.* are read without optional
    // chaining, so a missing branch is a throw, not a blank row.
    expect(KIROCREW_CONFIG_FIXTURE.agent).toBeTypeOf('object')
    expect(KIROCREW_CONFIG_FIXTURE.session).toBeTypeOf('object')
    expect(KIROCREW_CONFIG_FIXTURE.memory).toBeTypeOf('object')
    expect(KIROCREW_CONFIG_FIXTURE.auto_update).toBeTypeOf('boolean')
    for (const key of ['agents', 'workspaces', 'memory_stores'] as const) {
      expect(KIROCREW_CONFIG_FIXTURE[key]).toBeTypeOf('object')
      expect(KIROCREW_CONFIG_FIXTURE[key]).not.toBeNull()
    }
    for (const key of ['default_agent', 'default_workspace', 'default_memory_store'] as const) {
      expect(KIROCREW_CONFIG_FIXTURE[key]).toBeTypeOf('string')
    }
  })

  it('names each default as a key that actually exists', async () => {
    // The tab badges the default row by matching name === cfg.default_*. A
    // default naming a missing key renders three tables with no badge at all,
    // which is exactly the detail a Config screenshot exists to show.
    expect(KIROCREW_CONFIG_FIXTURE.agents).toHaveProperty(KIROCREW_CONFIG_FIXTURE.default_agent)
    expect(KIROCREW_CONFIG_FIXTURE.workspaces).toHaveProperty(KIROCREW_CONFIG_FIXTURE.default_workspace)
    expect(KIROCREW_CONFIG_FIXTURE.memory_stores).toHaveProperty(KIROCREW_CONFIG_FIXTURE.default_memory_store)
    renderWithProviders(<KiroCrewCfgTab />)
    await screen.findByText('Kiro Crew Agents')
    expect(screen.getAllByText('default').length).toBeGreaterThan(0)
  })

  it('serves an agent-config fixture the MCP view can read', () => {
    expect(AGENT_CONFIG_FIXTURE.name).toBeTypeOf('string')
    expect(AGENT_CONFIG_FIXTURE.mcpServers).toBeTypeOf('object')
  })
})
