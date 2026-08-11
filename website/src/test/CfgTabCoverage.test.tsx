/**
 * KiroCrewCfgTab — the Kiro Crew config table on the developer page.
 *
 * The file sat at ~3% before this suite: only its module-level constants ran.
 * Everything below aims at the cold paths — the query error/loading boundaries,
 * the three tables' per-cell fallbacks, and the three editor primitives
 * (CfgNumber / CfgSelect / CfgToggle) whose validation, dirty-tick and patch
 * plumbing had no coverage at all.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent, within, act } from '@testing-library/react'
import KiroCrewCfgTab from '../pages/overview/KiroCrewCfgTab'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

vi.mock('../api/client')

/* SimpleSelect is stubbed for the same reason as CrewEditorSelect.test.tsx and
   WorkspaceModal.test.tsx: it wraps a Radix Select, which commits its selection
   inside `ReactDOM.flushSync(...)`, and this tab mounts FIVE of them at once —
   driving them for real costs an open/close cycle per assertion for a dropdown
   that is not the code under test. What IS under test is CfgSelect's own
   `onChange` (markDirty → setLocal → onSave), which the stub reaches directly.
   Each stub is a role="group" named after its row label so duplicated option
   values ('auto' belongs to both Approval Mode and Sandbox) stay unambiguous;
   the current selection rides on each option's aria-selected, so the stub needs
   no trigger of its own. */
vi.mock('../components/SimpleSelect', () => ({
  default: ({ options, optionLabels, value, onChange, 'aria-label': ariaLabel }: {
    options: string[]
    optionLabels?: string[]
    value: string
    onChange: (v: string) => void
    'aria-label'?: string
  }) => (
    <div role="group" aria-label={ariaLabel}>
      {options.map((o, i) => (
        <button key={o} type="button" role="option" aria-selected={o === value} onClick={() => onChange(o)}>
          {optionLabels?.[i] ?? o}
        </button>
      ))}
    </div>
  ),
}))

type Cfg = Record<string, unknown>

/**
 * A config with something interesting in every conditional cell: `crew-beta`
 * carries an empty `kiro_agent` (em-dash fallback), `mem-spare` has neither a
 * description nor an embedding provider (inherited-provider italic), and
 * `ws-idle` is bound by nobody (empty Used By). Every name is distinct so a
 * within(row) query can never collide with a neighbouring cell.
 */
const CFG = {
  agents: {
    'crew-alpha': { kiro_agent: 'tmpl-alpha', workspace: 'ws-main', memory_store: 'mem-main', description: '', source: 'config' },
    'crew-beta': { kiro_agent: '', workspace: 'ws-main', memory_store: 'mem-spare', description: '', source: 'config' },
  },
  default_agent: 'crew-alpha',
  workspaces: { 'ws-main': { dir: 'dir-main' }, 'ws-idle': { dir: 'dir-idle' } },
  default_workspace: 'ws-main',
  memory_stores: {
    'mem-main': { description: 'legacy-default store', embedding_provider: 'bge-m3' },
    'mem-spare': { description: '', embedding_provider: '' },
  },
  default_memory_store: 'mem-main',
  agent: {
    default_agent: 'crew-alpha',
    provider: 'kiroacp',
    model: 'claude-opus',
    approval_mode: 'auto',
    sandbox: 'auto',
    subagent_max_turns: 100,
    max_subagents: 3,
    subagent_auto_max: 16,
    conductor_skill: false,
    tool_search: true,
    max_channels: 7,
    max_channel_agents: 5,
    enforce_denied_commands: 'all',
  },
  session: { timeout_secs: 3600, pool_size: 2, pool_agent: '', pool_ttl_secs: 600 },
  memory: { embedding_provider: 'inherited-embedder' },
  auto_update: true,
}

/** Clone so a per-test tweak never leaks into the shared fixture. */
const clone = (): typeof CFG => JSON.parse(JSON.stringify(CFG))

function seed(cfg: Cfg = CFG, patched: Cfg = CFG) {
  const m = vi.mocked(api)
  m.kirocrewConfig = vi.fn().mockResolvedValue(cfg)
  m.patchConfig = vi.fn().mockResolvedValue(patched)
  m.saveKirocrewConfig = vi.fn().mockResolvedValue({ ok: true })
  // ThemeProvider (installed by renderWithProviders) boots its own query; the
  // automock would resolve undefined, which React Query rejects out loud.
  m.themeBoot = vi.fn().mockResolvedValue({})
  return m
}

/** Render and wait for the first table to replace the skeleton. */
async function renderTab() {
  const view = renderWithProviders(<KiroCrewCfgTab />)
  expect(await screen.findByText('Kiro Crew Agents')).toBeInTheDocument()
  return view
}

/** Agents, Workspaces, Memory Stores — in DOM order. */
const tables = () => screen.getAllByRole('table')

const num = (label: string) => screen.getByRole('spinbutton', { name: label }) as HTMLInputElement

const optionIn = (groupLabel: string, optionName: string) =>
  within(screen.getByRole('group', { name: groupLabel })).getByRole('option', { name: optionName })

/**
 * A config row keyed by its visible label.
 *
 * CfgToggle's button carries no accessible name of its own — the label lives in
 * a sibling span — so the row has to be located by that span and the control
 * read from inside it. Matching on the span's leading TEXT node (rather than its
 * textContent) keeps the trailing InfoTip glyph out of the comparison.
 */
function rowFor(label: string): HTMLElement {
  const span = screen.getByText(
    (_content, el) => el?.tagName === 'SPAN' && el.firstChild?.nodeValue?.trim() === label,
  )
  return span.parentElement as HTMLElement
}

/** The on/off button of a CfgToggle row (the InfoTip button is named differently). */
const toggleFor = (label: string) =>
  within(rowFor(label)).getByRole('button', { name: /^(on|off)$/ })

beforeEach(() => {
  vi.clearAllMocks()
  seed()
})

// ── Query boundaries ─────────────────────────────────────────────────────
describe('KiroCrewCfgTab — query boundaries', () => {
  it('shows a skeleton until the config resolves', async () => {
    const m = vi.mocked(api)
    let release: (v: unknown) => void = () => {}
    m.kirocrewConfig = vi.fn().mockReturnValue(new Promise((res) => { release = res }))

    const { container } = renderWithProviders(<KiroCrewCfgTab />)
    expect(container.querySelector('.skeleton')).not.toBeNull()
    expect(screen.queryByText('Kiro Crew Agents')).toBeNull()

    // Settle it before the test ends so the query never resolves after teardown.
    await act(async () => { release(CFG) })
    expect(await screen.findByText('Kiro Crew Agents')).toBeInTheDocument()
  })

  it('renders an Error rejection by its message', async () => {
    const m = vi.mocked(api)
    m.kirocrewConfig = vi.fn().mockRejectedValue(new Error('config file unreadable'))

    renderWithProviders(<KiroCrewCfgTab />)
    expect(await screen.findByText('config file unreadable')).toBeInTheDocument()
    expect(screen.queryByText('Config Summary')).toBeNull()
  })

  it('stringifies a non-Error rejection', async () => {
    const m = vi.mocked(api)
    m.kirocrewConfig = vi.fn().mockRejectedValue('gateway offline')

    renderWithProviders(<KiroCrewCfgTab />)
    expect(await screen.findByText('gateway offline')).toBeInTheDocument()
  })
})

// ── The three read-only tables ───────────────────────────────────────────
describe('KiroCrewCfgTab — tables', () => {
  it('lists agents, badges the default one, and em-dashes a blank template', async () => {
    await renderTab()
    const agents = tables()[0]

    const beta = within(agents).getByText('crew-beta').closest('tr') as HTMLElement
    expect(within(beta).getByText('—')).toBeInTheDocument()
    expect(within(beta).getByText('mem-spare')).toBeInTheDocument()

    const alpha = within(agents).getByText('crew-alpha').closest('tr') as HTMLElement
    expect(within(alpha).getByText('default')).toBeInTheDocument()
    expect(within(alpha).getByText('tmpl-alpha')).toBeInTheDocument()
  })

  it('derives Used By per workspace and dashes one nobody binds', async () => {
    await renderTab()
    const workspaces = tables()[1]

    const bound = within(workspaces).getByText('dir-main').closest('tr') as HTMLElement
    // Both agents live in ws-main, so both surface as tags.
    expect(within(bound).getByText('crew-alpha')).toBeInTheDocument()
    expect(within(bound).getByText('crew-beta')).toBeInTheDocument()
    expect(within(bound).getByText('default')).toBeInTheDocument()

    const idle = within(workspaces).getByText('dir-idle').closest('tr') as HTMLElement
    expect(within(idle).getByText('—')).toBeInTheDocument()
  })

  it('falls back to the global embedder when a store sets none', async () => {
    await renderTab()
    const stores = tables()[2]

    const spare = within(stores).getByText('mem-spare').closest('tr') as HTMLElement
    expect(within(spare).getByText('inherited (inherited-embedder)')).toBeInTheDocument()
    expect(within(spare).getByText('—')).toBeInTheDocument()
    expect(within(spare).getByText('crew-beta')).toBeInTheDocument()

    const main = within(stores).getByText('legacy-default store').closest('tr') as HTMLElement
    expect(within(main).getByText('bge-m3')).toBeInTheDocument()
  })

  it('renders empty states when there are no agents and no stores', async () => {
    const bare = clone()
    bare.agents = {}
    bare.memory_stores = {}
    seed(bare)

    await renderTab()
    expect(screen.getByText('No agents defined')).toBeInTheDocument()
    expect(screen.getByText('Using legacy mode — agent.default_agent as agent template')).toBeInTheDocument()
    expect(screen.getByText('No memory stores')).toBeInTheDocument()
    expect(screen.getByText('Using global memory settings')).toBeInTheDocument()
    // Only the workspaces table survives.
    expect(tables()).toHaveLength(1)
  })

  it('shows the summary values the tab never lets you edit', async () => {
    await renderTab()
    expect(screen.getByText('kiroacp')).toBeInTheDocument()
    expect(screen.getByText('inherited-embedder')).toBeInTheDocument()
    expect(screen.getByText('7')).toBeInTheDocument()
    expect(screen.getByText('5')).toBeInTheDocument()
  })
})

// ── CfgNumber validation + commit ────────────────────────────────────────
describe('KiroCrewCfgTab — numeric rows', () => {
  it('rejects a non-numeric value without patching', async () => {
    await renderTab()

    const input = num('Session Timeout')
    fireEvent.change(input, { target: { value: 'abc' } })
    fireEvent.blur(input)

    expect(screen.getByText('invalid')).toBeInTheDocument()
    expect(vi.mocked(api).patchConfig).not.toHaveBeenCalled()
  })

  it('reports the floor and the ceiling instead of saving', async () => {
    await renderTab()
    const input = num('Session Timeout')

    fireEvent.change(input, { target: { value: '10' } })
    fireEvent.blur(input)
    expect(screen.getByText('min 60')).toBeInTheDocument()

    fireEvent.change(input, { target: { value: '99999' } })
    fireEvent.blur(input)
    expect(screen.getByText('max 86400')).toBeInTheDocument()

    expect(vi.mocked(api).patchConfig).not.toHaveBeenCalled()
  })

  it('patches on blur and confirms with a tick once the new value lands', async () => {
    const updated = clone()
    updated.session.timeout_secs = 7200
    seed(CFG, updated)

    const { container } = await renderTab()
    const input = num('Session Timeout')
    fireEvent.change(input, { target: { value: '7200' } })
    fireEvent.blur(input)

    await waitFor(() => {
      expect(vi.mocked(api).patchConfig).toHaveBeenCalledWith('session.timeout_secs', 7200)
    })
    // useDirtyTrack flips `ok` once the prop echoes the save back.
    await waitFor(() => expect(container.querySelector('.text-ok')).not.toBeNull())
  })

  it('commits on Enter as well as blur', async () => {
    const updated = clone()
    updated.session.pool_ttl_secs = 900
    seed(CFG, updated)

    await renderTab()
    const input = num('Pool TTL')
    fireEvent.change(input, { target: { value: '900' } })

    // Any other key is a keystroke mid-edit, not a commit.
    fireEvent.keyDown(input, { key: 'ArrowUp' })
    expect(vi.mocked(api).patchConfig).not.toHaveBeenCalled()

    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => {
      expect(vi.mocked(api).patchConfig).toHaveBeenCalledWith('session.pool_ttl_secs', 900)
    })
  })

  it('does not patch when the committed value equals the current one', async () => {
    await renderTab()
    const input = num('Pool Size')
    fireEvent.change(input, { target: { value: '2' } })
    fireEvent.blur(input)

    expect(vi.mocked(api).patchConfig).not.toHaveBeenCalled()
    expect(screen.queryByText('invalid')).toBeNull()
  })
})

// ── CfgSelect + CfgToggle ────────────────────────────────────────────────
describe('KiroCrewCfgTab — select and toggle rows', () => {
  it('patches the selected option for the row that owns it', async () => {
    const updated = clone()
    updated.agent.sandbox = 'off'
    seed(CFG, updated)

    await renderTab()
    fireEvent.click(optionIn('Sandbox', 'off'))

    await waitFor(() => {
      expect(vi.mocked(api).patchConfig).toHaveBeenCalledWith('agent.sandbox', 'off')
    })
  })

  it('keeps same-valued options on different rows apart', async () => {
    const updated = clone()
    updated.agent.approval_mode = 'interactive'
    seed(CFG, updated)

    await renderTab()
    expect(optionIn('Approval Mode', 'auto')).toHaveAttribute('aria-selected', 'true')
    fireEvent.click(optionIn('Approval Mode', 'interactive'))

    await waitFor(() => {
      expect(vi.mocked(api).patchConfig).toHaveBeenCalledWith('agent.approval_mode', 'interactive')
    })
  })

  it('labels the empty pool agent with the configured default', async () => {
    await renderTab()
    expect(optionIn('Pool Agent', '(crew-alpha)')).toBeInTheDocument()

    fireEvent.click(optionIn('Pool Agent', 'crew-beta'))
    await waitFor(() => {
      expect(vi.mocked(api).patchConfig).toHaveBeenCalledWith('session.pool_agent', 'crew-beta')
    })
  })

  it('flips a boolean row and patches the negated value', async () => {
    const updated = clone()
    updated.auto_update = false
    seed(CFG, updated)

    await renderTab()
    const toggle = toggleFor('Auto Update')
    expect(toggle).toHaveTextContent('on')

    fireEvent.click(toggle)
    expect(toggle).toHaveTextContent('off')
    await waitFor(() => {
      expect(vi.mocked(api).patchConfig).toHaveBeenCalledWith('auto_update', false)
    })
  })

  it('surfaces a failed patch in both cards that host the save banner', async () => {
    const m = vi.mocked(api)
    m.patchConfig = vi.fn().mockRejectedValue(new Error('read-only config'))

    await renderTab()
    fireEvent.click(toggleFor('MCP Tool Search'))

    // The banner is rendered once in Warm Pool and once in Config Summary.
    await waitFor(() => {
      expect(screen.getAllByText('read-only config')).toHaveLength(2)
    })
    // onError also invalidates the config query, so it refetches.
    await waitFor(() => expect(m.kirocrewConfig).toHaveBeenCalledTimes(2))
  })

  it('applies defaults for the keys an older config file omits', async () => {
    const sparse = clone() as Cfg
    const agent = sparse.agent as Record<string, unknown>
    delete agent.enforce_denied_commands
    delete agent.tool_search
    const session = sparse.session as Record<string, unknown>
    delete session.pool_size
    delete session.pool_agent
    sparse.default_agent = ''
    seed(sparse)

    await renderTab()
    expect(num('Pool Size').value).toBe('0')
    expect(toggleFor('MCP Tool Search')).toHaveTextContent('on')
    expect(optionIn('Enforce Denied Commands', 'all')).toHaveAttribute('aria-selected', 'true')
    // With no default agent configured, the empty pool-agent option falls back
    // to a generic placeholder instead of naming one.
    expect(optionIn('Pool Agent', '(default agent)')).toBeInTheDocument()
  })

  it('renders the warm pool card for a provider that advertises the capability', async () => {
    await renderTab()
    // The active ACP adapter sets capabilities.warmPool, so the card is present
    // and owns the only Pool Size row on the page.
    expect(screen.getByText('Warm Pool')).toBeInTheDocument()
    expect(screen.getAllByRole('spinbutton', { name: 'Pool Size' })).toHaveLength(1)
  })
})

// ── SubagentSettings ─────────────────────────────────────────────────────
describe('KiroCrewCfgTab — subagent settings', () => {
  const saveBtn = () => screen.getByRole('button', { name: 'Save' })

  it('keeps Save disabled until something actually differs', async () => {
    await renderTab()
    expect(saveBtn()).toBeDisabled()

    fireEvent.change(num('Max Turns per Subagent'), { target: { value: '120' } })
    expect(saveBtn()).toBeEnabled()

    fireEvent.change(num('Max Turns per Subagent'), { target: { value: '100' } })
    expect(saveBtn()).toBeDisabled()
  })

  it('sends the whole subagent block and confirms', async () => {
    const m = vi.mocked(api)
    await renderTab()

    fireEvent.change(num('Max Turns per Subagent'), { target: { value: '150' } })
    fireEvent.click(screen.getByRole('button', { name: 'Orchestrator Mode' }))
    fireEvent.click(saveBtn())

    expect(await screen.findByText('Saved')).toBeInTheDocument()
    expect(m.saveKirocrewConfig).toHaveBeenCalledWith({
      subagent_max_turns: 150,
      max_subagents: 3,
      subagent_auto_max: 16,
      conductor_skill: true,
    })
    // onSaved invalidates the config query.
    await waitFor(() => expect(m.kirocrewConfig).toHaveBeenCalledTimes(2))
  })

  it('shows a rejection returned in the payload', async () => {
    const m = vi.mocked(api)
    m.saveKirocrewConfig = vi.fn().mockResolvedValue({ error: 'value out of range' })

    await renderTab()
    fireEvent.change(num('Max Turns per Subagent'), { target: { value: '7' } })
    fireEvent.click(saveBtn())

    expect(await screen.findByText('value out of range')).toBeInTheDocument()
    // A rejected save must not refetch as if it had landed.
    expect(m.kirocrewConfig).toHaveBeenCalledTimes(1)
  })

  it('shows a thrown save error and re-enables the button', async () => {
    const m = vi.mocked(api)
    m.saveKirocrewConfig = vi.fn().mockRejectedValue(new Error('socket hang up'))

    await renderTab()
    fireEvent.change(num('Max Concurrent Subagents'), { target: { value: '5' } })
    fireEvent.click(saveBtn())

    expect(await screen.findByText('socket hang up')).toBeInTheDocument()
    expect(saveBtn()).toBeEnabled()
  })

  it('stringifies a non-Error thrown by the save call', async () => {
    const m = vi.mocked(api)
    m.saveKirocrewConfig = vi.fn().mockRejectedValue('gateway went away')

    await renderTab()
    fireEvent.change(num('Max Turns per Subagent'), { target: { value: '9' } })
    fireEvent.click(saveBtn())

    expect(await screen.findByText('gateway went away')).toBeInTheDocument()
  })

  it('reveals the auto-size ceiling only while concurrency is auto', async () => {
    await renderTab()
    expect(screen.queryByRole('spinbutton', { name: 'Auto-Size Max' })).toBeNull()

    fireEvent.change(num('Max Concurrent Subagents'), { target: { value: '0' } })
    expect(within(rowFor('Max Concurrent Subagents')).getByText('auto')).toBeInTheDocument()
    expect(num('Auto-Size Max').value).toBe('16')

    fireEvent.change(num('Max Concurrent Subagents'), { target: { value: '4' } })
    expect(screen.queryByRole('spinbutton', { name: 'Auto-Size Max' })).toBeNull()
  })

  it('clamps every numeric input to its own bounds', async () => {
    await renderTab()

    // A blank max-turns collapses to the minimum rather than NaN.
    fireEvent.change(num('Max Turns per Subagent'), { target: { value: '' } })
    expect(num('Max Turns per Subagent').value).toBe('1')

    // Negative concurrency floors at 0, which means auto.
    fireEvent.change(num('Max Concurrent Subagents'), { target: { value: '-3' } })
    expect(num('Max Concurrent Subagents').value).toBe('0')

    // A blank one lands on auto too.
    fireEvent.change(num('Max Concurrent Subagents'), { target: { value: '' } })
    expect(num('Max Concurrent Subagents').value).toBe('0')

    fireEvent.change(num('Auto-Size Max'), { target: { value: '999' } })
    expect(num('Auto-Size Max').value).toBe('64')
    fireEvent.change(num('Auto-Size Max'), { target: { value: '' } })
    expect(num('Auto-Size Max').value).toBe('1')
  })

  it('resyncs local edits when a fresh config arrives', async () => {
    const updated = clone()
    updated.agent.subagent_max_turns = 42
    updated.agent.conductor_skill = true
    seed(CFG, updated)

    await renderTab()
    fireEvent.change(num('Max Turns per Subagent'), { target: { value: '150' } })
    expect(num('Max Turns per Subagent').value).toBe('150')

    // A patch elsewhere on the page replaces the cached config; the subagent
    // block must follow the server, discarding the uncommitted 150.
    fireEvent.click(toggleFor('Auto Update'))
    await waitFor(() => expect(num('Max Turns per Subagent').value).toBe('42'))
    expect(screen.getByRole('button', { name: 'Orchestrator Mode' })).toHaveTextContent('Enabled')
    expect(saveBtn()).toBeDisabled()
  })

  it('falls back to built-in subagent defaults when the block is absent', async () => {
    const sparse = clone() as Cfg
    const agent = sparse.agent as Record<string, unknown>
    delete agent.subagent_max_turns
    delete agent.max_subagents
    delete agent.subagent_auto_max
    delete agent.conductor_skill
    seed(sparse)

    await renderTab()
    expect(num('Max Turns per Subagent').value).toBe('100')
    expect(num('Max Concurrent Subagents').value).toBe('3')
    expect(screen.getByRole('button', { name: 'Orchestrator Mode' })).toHaveTextContent('Disabled')
    expect(saveBtn()).toBeDisabled()
  })
})
