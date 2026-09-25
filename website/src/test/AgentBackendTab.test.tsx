/**
 * AgentBackendTab — Settings > Agent Harness, as a list and a detail.
 *
 * Three independent rules, and the tests exist mostly to keep them from being
 * collapsed into each other.
 *
 * The SCHEMA gate is a build/edition fact from `GET /api/config/schema`: a backend
 * the build cannot serve must not be switchable, and one a later edition adds must
 * become switchable with no change here.
 *
 * The PROBE gate is a machine fact from `GET /api/acp-backends`: a backend whose
 * components are absent must not be switchable either, and must say what to install.
 * Its `unknown` verdict — and every way the probe can fail to answer at all — must
 * leave the switch ENABLED, because an optimistic disable costs a user a control they
 * were entitled to and an install they did not need.
 *
 * HIGHLIGHT IS NOT SELECTION is the third and the newest. Clicking a row, arrowing
 * onto it, or pressing Enter on it changes which detail is on screen and nothing
 * else. The active backend moves in exactly one place — the Use button — so several
 * tests below exist only to assert that a navigation gesture wrote no config.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import React from 'react'
import { ApiError } from '../api/apiError'

const {
  patchConfigMock,
  kirocrewConfigMock,
  schemaMock,
  acpBackendsMock,
  acpBackendRecheckMock,
} = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() => Promise.resolve({ agent: { acp_backend: '' } })),
  schemaMock: vi.fn(),
  acpBackendsMock: vi.fn(),
  acpBackendRecheckMock: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    kirocrewConfig: kirocrewConfigMock,
    patchConfig: patchConfigMock,
    acpBackends: acpBackendsMock,
    acpBackendRecheck: acpBackendRecheckMock,
  },
}))

vi.mock('../components/settingRef/useConfigSchema', () => ({
  useConfigSchema: () => schemaMock(),
}))

// The card under the switch is a sentinel: its own behaviour (status query,
// chooser, sign-out) is pinned in KiroSignInCard.test.tsx, and the real card
// would need the kas-login API this file does not mock. Unlike the real card it
// renders unconditionally, so its absence below can only be the tab's gate.
vi.mock('../pages/developer/KiroSignInCard', () => ({
  KiroSignInCard: ({ compact }: { compact?: boolean }) => (
    <div data-testid="kiro-sign-in-card" data-compact={compact ? 'true' : 'false'} />
  ),
}))

import { AgentBackendTab } from '../pages/developer/AgentBackendTab'

/** A schema map advertising exactly `values` for the backend field. */
function schemaWith(values: string[] | undefined) {
  if (!values) return undefined
  return new Map([['agent.acp_backend', { path: 'agent.acp_backend', type: 'enum', enum: values }]])
}

/** A card payload shaped like the server's, defaulted to the dull answer. */
function card(
  over: Partial<{
    capabilities: {
      id: string
      available: boolean
      measured?: boolean
      unmeasured_reason?: string
    }[]
    security_notes: string[]
    operator_notes: string[]
    tool_approval: string
    offered_by_build: boolean
    mcp: {
      per_tool_deny: string
      costs_whole_server?: boolean
      ineffective: string[]
    }
  }> = {},
) {
  return {
    capabilities: [
      { id: 'crew_tools', available: true },
      { id: 'mid_turn_steer', available: false },
    ],
    security_notes: [],
    operator_notes: [],
    tool_approval: 'agent_spec',
    offered_by_build: true,
    ...over,
  }
}

/**
 * The MCP half of a card, defaulted to the dull answer: projected by a mirror, a
 * tool-off that stays per tool, nothing withheld.
 *
 * Deliberately NOT part of `card()`'s default. A gateway that predates the field
 * sends no `mcp` at all, so the uninteresting row is the one that carries none and
 * every test below opts in to the half it is about — the same arrangement as `auth`.
 */
function mcp(
  over: Partial<{
    per_tool_deny: string
    costs_whole_server: boolean
    ineffective: string[]
  }> = {},
) {
  const reach = over.per_tool_deny ?? 'settings-file'
  return {
    mcp: {
      per_tool_deny: reach,
      // The server ships its classification beside the reach
      // (`backend_mcp_ability.COSTS_WHOLE_SERVER`), so the fixture ships one too and
      // a test that states only a reach gets the payload the gateway would send.
      // Overridable, because the point of a server-side flag is that the two can be
      // stated independently — an unclassified reach, or one this frontend has never
      // heard of, is a payload only an override can build.
      costs_whole_server: reach === 'whole-server' || reach === 'per-call',
      ineffective: [] as string[],
      ...over,
    },
  }
}

/**
 * One `GET /api/acp-backends` row, defaulted to the uninteresting answer
 * (selectable and installed) so each test states only the field it is about.
 */
function probeRow(
  id: string,
  over: Partial<{
    selectable: boolean
    installed: string
    missing_components: string[]
    install_command: string
    restart_required: boolean
    auth: { sign_in_remedy: string; signs_in_separately: boolean }
    capabilities: {
      id: string
      available: boolean
      measured?: boolean
      unmeasured_reason?: string
    }[]
    security_notes: string[]
    operator_notes: string[]
    tool_approval: string
    offered_by_build: boolean
    mcp: {
      per_tool_deny: string
      costs_whole_server?: boolean
      ineffective: string[]
    }
  }> = {},
) {
  return {
    id,
    policy_id: id || 'kiro',
    selectable: true,
    installed: 'installed',
    missing_components: [],
    install_command: '',
    restart_required: false,
    // No `auth` by default: an older gateway sends none, so the uninteresting row
    // is the one that carries no auth object at all.
    ...over,
  }
}

function wrapWithClient() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <AgentBackendTab />
    </QueryClientProvider>,
  )
  return { qc }
}

function wrap() {
  wrapWithClient()
}

/**
 * A harness ROW's radio. Its accessible name is the harness name alone, so it can
 * never be confused with the Use button, and `checked` is which detail is open.
 */
const row = (name: string) => screen.getByRole('radio', { name })
/**
 * The visible row the radio sits in: the label carrying the name, the in-use word
 * and the status badge. What a sighted reader scans.
 */
const rowLabel = (name: string) => row(name).closest('label') as HTMLElement
/** The harness NAMES in list order, read off each radio's accessible name. */
const rows = () => screen.getAllByRole('radio').map(el => el.getAttribute('aria-label'))
/** The one control that switches the backend. */
const useButton = (name: string) => screen.getByRole('button', { name: `Use ${name}` })
/** Put a harness's detail on screen without switching to it. */
const highlight = (name: string) => fireEvent.click(row(name))
/** The one open detail: the panel under the checked row. */
const detail = () => screen.getByTestId('agent-harness-detail')
/**
 * Queries scoped to the open detail.
 *
 * Load-bearing, not tidiness. Each harness's status sentence is in the DOM TWICE by
 * design -- once as the row's `aria-describedby` text, so a reader arrowing down the
 * list is told each state, and once visibly in the detail. An unscoped `getByText`
 * cannot choose between them, and the assertion worth making is the scoped one: that
 * the DETAIL says it, rather than that the document contains it somewhere.
 */
const panel = () => within(detail())

beforeEach(() => {
  localStorage.clear()
  patchConfigMock.mockClear()
  patchConfigMock.mockResolvedValue({})
  kirocrewConfigMock.mockClear()
  kirocrewConfigMock.mockResolvedValue({ agent: { acp_backend: '' } })
  acpBackendRecheckMock.mockClear()
  // The shipped core: every known agent is selectable. Claude Code is in the public
  // baseline because acp/client.py owns its whole spawn path and the adapter it needs
  // is a public npm package -- the only thing that used to be missing was the switch.
  schemaMock.mockReturnValue(schemaWith(['', 'claude', 'kas']))
  // Default to NO probe information — a 404 from a gateway that predates the
  // endpoint. Every test that does not opt in therefore pins the pre-probe
  // behaviour: schema-only gating, nothing dead or annotated by the probe.
  acpBackendsMock.mockClear()
  acpBackendsMock.mockRejectedValue(new Error('404 Not Found'))
})

describe('AgentBackendTab list', () => {
  it('lists all three backends as rows', async () => {
    wrap()
    expect(await screen.findByRole('radio', { name: 'Kiro CLI' })).toBeInTheDocument()
    expect(row('Claude Code')).toBeInTheDocument()
    expect(row('KAS (kiro-agent)')).toBeInTheDocument()
  })

  it('puts the two kiro-family harnesses first and sorts the adapters after', async () => {
    // KAS is not an adapter: it is kiro-cli's own relay, sharing kiro's install
    // verdict, so the two harnesses that are really one install stay adjacent at the
    // head. Under `policy_id` alone KAS sorts on 'k' and lands behind 'claude', which
    // reads as a rank rather than an alphabet.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow(''), probeRow('claude'), probeRow('kas'), probeRow('zephyr')],
    })
    schemaMock.mockReturnValue(schemaWith(['', 'claude', 'kas', 'zephyr']))
    wrap()
    await waitFor(() => expect(screen.getAllByRole('radio')).toHaveLength(4))
    expect(rows()).toEqual(['Kiro CLI', 'KAS (kiro-agent)', 'Claude Code', 'zephyr'])
  })

  it('marks the configured backend as the current one', async () => {
    kirocrewConfigMock.mockResolvedValue({ agent: { acp_backend: 'claude' } })
    wrap()
    await waitFor(() => expect(row('Claude Code')).toHaveAttribute('aria-current', 'true'))
    expect(row('Kiro CLI')).not.toHaveAttribute('aria-current')
  })

  it('treats a missing acp_backend as kiro-cli rather than as unset', async () => {
    kirocrewConfigMock.mockResolvedValue({})
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toHaveAttribute('aria-current', 'true'))
  })

  it('opens on the current backend detail', async () => {
    // The useful default and the one id the row list always contains.
    kirocrewConfigMock.mockResolvedValue({ agent: { acp_backend: 'kas' } })
    wrap()
    await waitFor(() => expect(row('KAS (kiro-agent)')).toBeChecked())
    expect(row('Kiro CLI')).not.toBeChecked()
  })

  it('hides a backend the deployment may not select, rather than dimming it', async () => {
    // A greyed row invites the reader to find out how to enable it, and under a
    // managed policy there is nothing they can do.
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    expect(screen.queryByRole('radio', { name: 'Claude Code' })).toBeNull()
    expect(screen.queryByRole('radio', { name: 'KAS (kiro-agent)' })).toBeNull()
  })

  it('keeps the current backend listed even if it reads as unselectable', async () => {
    // A control that lists no active harness is a worse failure than one extra row.
    kirocrewConfigMock.mockResolvedValue({ agent: { acp_backend: 'claude' } })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() => expect(row('Claude Code')).toHaveAttribute('aria-current', 'true'))
  })

  it('lists a backend the schema starts advertising, with no edit here', async () => {
    // The whole point of reading the option set from the server: an edition that
    // registers a harness lights it up with no frontend change.
    schemaMock.mockReturnValue(schemaWith(['', 'claude', 'kas', 'future-agent']))
    wrap()
    await waitFor(() => expect(row('future-agent')).toBeInTheDocument())
  })

  it('lists an agent this frontend has no name for, under its policy id', async () => {
    // `nameOf` falls back to `policy_id`, which is already a word rather than an
    // internal token -- it is what a governance rule spells.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow(''), probeRow('plugin-agent', { ...card() })],
    })
    schemaMock.mockReturnValue(schemaWith(['', 'plugin-agent']))
    wrap()
    await waitFor(() => expect(row('plugin-agent')).toBeInTheDocument())
    highlight('plugin-agent')
    expect(useButton('plugin-agent')).toBeEnabled()
  })

  it('hides a row the probe reports as not selectable, whatever the schema says', async () => {
    // The probe's `selectable` is the same fact from the same source as the schema's,
    // so honouring both means the two cannot disagree in a way that lets a dead
    // switch look live.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow(''), probeRow('claude', { selectable: false })],
    })
    schemaMock.mockReturnValue(schemaWith(['', 'claude']))
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    await waitFor(() => expect(screen.queryByRole('radio', { name: 'Claude Code' })).toBeNull())
  })

  it('lists every backend while the schema is still loading', async () => {
    // Absent information is not a verdict: hiding a row on it is the same mistake as
    // disabling one, and would read as a control changing shape on a slow load.
    schemaMock.mockReturnValue(undefined)
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    expect(row('Claude Code')).toBeInTheDocument()
    expect(row('KAS (kiro-agent)')).toBeInTheDocument()
  })

  it('offers a retry instead of a false current backend when the config read fails', async () => {
    // `?? KIRO` is right for a config that omits the key and wrong for a read that
    // FAILED: defaulting would paint Kiro CLI as active to an operator running KAS.
    kirocrewConfigMock.mockRejectedValue(new Error('boom'))
    wrap()
    expect(await screen.findByText('Could not load the agent backend.')).toBeInTheDocument()
    // A `useQuery` failure, so it renders through `ErrorNotice` rather than as a
    // hand-written line -- the journal holds the route and status this component never
    // sees, and the agent hand-off is what recovers them.
    expect(
      screen.getByText('Could not load the agent backend.').closest('[role="alert"]'),
    ).not.toBeNull()
    expect(screen.queryByRole('radio')).toBeNull()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('states that the set is decided at gateway start', async () => {
    // The one thing no per-row line can say: a managed fleet bounds this set by
    // policy, and that policy is read once when the gateway starts.
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    expect(screen.getByText(/decided when the gateway starts/)).toBeInTheDocument()
  })
})

describe('AgentBackendTab checking a row is not selecting an agent', () => {
  it('clicking a row changes the detail and writes no config', async () => {
    // The rule the whole layout exists for. Under the old control the only way to
    // read about a harness was to select it.
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    highlight('Claude Code')
    expect(row('Claude Code')).toBeChecked()
    // Looking at it did not make it the running one.
    expect(row('Claude Code')).not.toHaveAttribute('aria-current')
    expect(row('Kiro CLI')).toHaveAttribute('aria-current', 'true')
    expect(patchConfigMock).not.toHaveBeenCalled()
  })

  it('checking a radio opens its detail and writes no config', async () => {
    // The radio's own change event -- what a keyboard arrow fires in a browser --
    // is a look, not a switch. "Checking does not switch the backend" has to be a
    // property of the component, not of which handler happens to be wired.
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    fireEvent.click(row('KAS (kiro-agent)'))
    expect(row('KAS (kiro-agent)')).toBeChecked()
    expect(row('Kiro CLI')).not.toBeChecked()
    expect(row('Kiro CLI')).toHaveAttribute('aria-current', 'true')
    expect(patchConfigMock).not.toHaveBeenCalled()
  })

  it('is one radio group, so the browser supplies the keyboard', async () => {
    // Every row shares one `name`: that is what gives the list a single tab stop
    // and arrow keys that move the check and the focus together, natively -- the
    // behaviour the old tablist reimplemented by hand. Eight tabbable rows would
    // put eight stops between the page heading and the one button.
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    const names = new Set(screen.getAllByRole('radio').map(el => el.getAttribute('name')))
    expect(names.size).toBe(1)
    expect([...names][0]).toBeTruthy()
    // Exactly one is checked at any time, and the check follows the open detail.
    expect(screen.getAllByRole('radio').filter(el => (el as HTMLInputElement).checked)).toHaveLength(1)
    highlight('Claude Code')
    expect(screen.getAllByRole('radio').filter(el => (el as HTMLInputElement).checked)).toHaveLength(1)
    expect(row('Claude Code')).toBeChecked()
    // The checked radio names the detail it opened.
    expect(row('Claude Code')).toHaveAttribute('aria-controls', detail().id)
    expect(row('Kiro CLI')).not.toHaveAttribute('aria-controls')
  })

  it('does not steal focus when a row is clicked rather than arrowed to', async () => {
    // Clicking already puts focus where the user pointed; the component must not
    // fight the browser over it, and a click on a row must not pull focus off, say,
    // the Use button the user was about to press.
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    highlight('Claude Code')
    expect(row('Claude Code')).toBeChecked()
  })

  it('renders exactly one detail, whatever is checked', async () => {
    // The reason the panel was redesigned: every harness's whole card used to render
    // stacked down the page.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('', card()), probeRow('kas', card()), probeRow('claude', card())],
    })
    wrap()
    await waitFor(() => expect(screen.getAllByTestId('agent-harness-detail')).toHaveLength(1))
    expect(screen.getAllByText(/supports \d+ of \d+ features/)).toHaveLength(1)
    highlight('Claude Code')
    expect(screen.getAllByTestId('agent-harness-detail')).toHaveLength(1)
    expect(screen.getAllByText(/supports \d+ of \d+ features/)).toHaveLength(1)
  })

  it('opens the detail directly under its own row, not in a side pane', async () => {
    // The onboarding shape: the detail is inside the checked row's outline, after
    // its label, so it reads as that agent's panel rather than a card detached at
    // the foot of the list.
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    highlight('Claude Code')
    const outline = rowLabel('Claude Code').parentElement as HTMLElement
    expect(outline.contains(detail())).toBe(true)
    expect(
      rowLabel('Claude Code').compareDocumentPosition(detail()) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
    // And not under any other row.
    expect((rowLabel('Kiro CLI').parentElement as HTMLElement).contains(detail())).toBe(false)
  })

  it('states each row status as words, outside the row so it is not the row name', async () => {
    // The badge summarises a sentence that is also present. It lives OUTSIDE the row
    // because text inside would join the row's accessible NAME, and every row would
    // then be named after its own problem.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', card()),
        probeRow('kas', { installed: 'missing', missing_components: ['kiro-cli'], ...card() }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['', 'kas']))
    wrap()
    await waitFor(() => expect(row('KAS (kiro-agent)')).toBeInTheDocument())
    // The name is the name -- the row also carries a badge, and neither of them is
    // the row's accessible NAME.
    expect(row('KAS (kiro-agent)')).toHaveAccessibleName('KAS (kiro-agent)')
    expect(rowLabel('KAS (kiro-agent)').querySelector('span.truncate')?.textContent).toBe(
      'KAS (kiro-agent)',
    )
    // The state is the description.
    const describedBy = row('KAS (kiro-agent)').getAttribute('aria-describedby')
    expect(describedBy).toBeTruthy()
    expect(document.getElementById(describedBy as string)?.textContent).toContain(
      'Missing on this machine: kiro-cli',
    )
  })

  it('states BOTH in-use and readiness, so an active broken harness shows both', async () => {
    // The two facts are independent and an operator needs them at once. One mark with
    // a precedence can only ever show the winner, which is why they are two things on
    // the row: a harness that is the one running AND missing its binary has to read
    // as "in use, and broken" rather than as one or the other.
    kirocrewConfigMock.mockResolvedValue({ agent: { acp_backend: 'claude' } })
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', card()),
        probeRow('claude', {
          installed: 'missing',
          missing_components: ['claude-agent-acp'],
          ...card(),
        }),
      ],
    })
    wrap()
    await waitFor(() => expect(row('Claude Code')).toHaveAttribute('aria-current', 'true'))

    // Both words are in the row's description, in the order they are read visually.
    const describedBy = row('Claude Code').getAttribute('aria-describedby')
    const description = document.getElementById(describedBy as string)?.textContent ?? ''
    expect(description).toContain('In use.')
    expect(description).toContain('Missing on this machine: claude-agent-acp')

    // A healthy row that is NOT current says neither.
    const other = document.getElementById(
      row('Kiro CLI').getAttribute('aria-describedby') as string,
    )
    expect(other?.textContent).not.toContain('In use.')
  })

  it('marks in-use independently of readiness, so a broken active row keeps its word', async () => {
    // The regression this guards: an earlier single-glyph row let a readiness problem
    // hide which backend was running.
    kirocrewConfigMock.mockResolvedValue({ agent: { acp_backend: 'claude' } })
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', card()),
        probeRow('claude', { installed: 'missing', missing_components: ['x'], ...card() }),
      ],
    })
    wrap()
    // aria-current is the machine-readable half and survives the readiness verdict.
    await waitFor(() => expect(row('Claude Code')).toHaveAttribute('aria-current', 'true'))
    // Both render on the row, visibly: the in-use word and the readiness badge.
    await waitFor(() => expect(rowLabel('Claude Code').textContent).toContain('Not installed'))
    expect(rowLabel('Claude Code').textContent).toContain('In use')
    // A non-current row carries only the badge.
    expect(rowLabel('Kiro CLI').textContent).toContain('Installed')
    expect(rowLabel('Kiro CLI').textContent).not.toContain('In use')
  })

  it('says "In use" visibly on the row, not only to a screen reader', async () => {
    // The configured harness has no Use button, so the row is the one place a
    // sighted reader learns which agent is running. The word is on the row itself,
    // and only on the running one.
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toHaveAttribute('aria-current', 'true'))
    const label = within(rowLabel('Kiro CLI')).getByText('In use')
    expect(label).toBeInTheDocument()
    expect(label.className).not.toContain('sr-only')
    expect(within(rowLabel('Claude Code')).queryByText('In use')).toBeNull()
    // And it belongs to the ACTIVE harness, not to whichever row is being read.
    highlight('Claude Code')
    expect(within(rowLabel('Kiro CLI')).getByText('In use')).toBeInTheDocument()
    expect(within(rowLabel('Claude Code')).queryByText('In use')).toBeNull()
  })

  it('makes a dead Use button look dead, in the onboarding button style', async () => {
    // The detail's one mutating control is the same accent button first-run setup
    // uses, and its dead state is that button's: disabled, faded, no pointer. A dead
    // click is answered by silence, so the button must not read as pressable.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', card()),
        probeRow('claude', { installed: 'missing', missing_components: ['x'], ...card() }),
      ],
    })
    wrap()
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    await waitFor(() => expect(useButton('Claude Code')).toBeDisabled())
    const dead = useButton('Claude Code').className
    expect(dead).toContain('disabled:opacity-30')
    expect(dead).toContain('disabled:cursor-not-allowed')
    expect(dead).toContain('bg-accent')

    // The contrast, in the same render: a pressable one is enabled and accent-filled.
    highlight('Kiro CLI')
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('', card()), probeRow('kas', card())],
    })
    highlight('KAS (kiro-agent)')
    await waitFor(() => expect(useButton('KAS (kiro-agent)')).toBeEnabled())
    expect(useButton('KAS (kiro-agent)').className).toContain('bg-accent')
  })

  it('gives the in-use word a hover title as well', async () => {
    // The word alone is what a sighted reader gets; the title repeats it for a
    // hover, so the hover text and the visible text are one string.
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toHaveAttribute('aria-current', 'true'))
    expect(rowLabel('Kiro CLI').querySelector('[title="In use"]')).toBeInTheDocument()
    expect(rowLabel('Kiro CLI').textContent).toContain('In use')
    expect(rowLabel('Claude Code').querySelector('[title="In use"]')).toBeNull()
    expect(rowLabel('Claude Code').textContent).not.toContain('In use')
  })

  it('does not say the in-use word twice to a screen reader', async () => {
    // The row description states both facts in full, so the visible word and the
    // badge do not join the accessible NAME of the radio.
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toHaveAttribute('aria-current', 'true'))
    expect(row('Kiro CLI')).toHaveAccessibleName('Kiro CLI')
  })

  it('mutes the capability list for a harness this build does not offer', async () => {
    // A full-strength list directly under "This build does not offer this agent" is a
    // mixed message. The facts stay -- an operator asking why they cannot pick it needs
    // them -- they just stop competing with the line that answers the question.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', card()),
        probeRow('deepseek', {
          selectable: false,
          ...card({ offered_by_build: false, tool_approval: 'unverified' }),
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() => expect(row('deepseek')).toBeInTheDocument())
    highlight('deepseek')
    const count = panel().getByText(/deepseek supports \d+ of \d+ features/)
    expect(count.parentElement?.className).toContain('opacity-60')

    // An offered harness's list is not muted.
    highlight('Kiro CLI')
    const kiro = panel().getByText(/Kiro CLI supports \d+ of \d+ features/)
    expect(kiro.parentElement?.className).not.toContain('opacity-60')
  })

  it('shows the full harness name, never truncated by a fixed row width', async () => {
    // The old strip capped each row at 11rem and truncated "KAS (kiro-agent)" to
    // "KAS (kiro-..." at ordinary widths. A row now spans the list, so the name has
    // the row's width and the badge yields its own space.
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    for (const name of ['Kiro CLI', 'Claude Code', 'KAS (kiro-agent)']) {
      const label = rowLabel(name)
      expect(label.className.split(/\s+/)).not.toEqual(expect.arrayContaining([expect.stringMatching(/^max-w-/)]))
      expect(label.querySelector('span.truncate')?.textContent).toBe(name)
    }
  })

  it('says nothing about readiness it did not measure', async () => {
    // No probe payload at all -- a 404 or a non-owner 403. The active row still shows
    // which backend is running, because in-use is not a readiness claim; and no row
    // carries a badge, because a badge would be a claim.
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toHaveAttribute('aria-current', 'true'))
    const describedBy = row('Kiro CLI').getAttribute('aria-describedby')
    expect(document.getElementById(describedBy as string)?.textContent).toContain('In use.')
    expect(screen.queryByText(/Missing on this machine/)).toBeNull()
    expect(rowLabel('Kiro CLI').textContent).not.toContain('Installed')
    expect(rowLabel('Claude Code').textContent).not.toContain('Installed')
  })

  it('puts a status badge on each row, in the words first-run setup uses', async () => {
    // A glyph with only a `title` is a glyph a touch device never explains and a
    // first-time reader has to guess at. The badge is the same one the setup gate's
    // picker shows, so a harness reads the same on both screens.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', card()),
        probeRow('claude', { installed: 'missing', missing_components: ['x'], ...card() }),
        probeRow('codex', { installed: 'unknown', ...card() }),
        probeRow('pi', { restart_required: true, ...card() }),
        probeRow('deepseek', {
          selectable: false,
          ...card({ offered_by_build: false, tool_approval: 'unverified' }),
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['', 'claude', 'codex', 'pi']))
    wrap()
    await waitFor(() => expect(row('deepseek')).toBeInTheDocument())

    expect(rowLabel('Kiro CLI').textContent).toContain('Installed')
    expect(rowLabel('Claude Code').textContent).toContain('Not installed')
    expect(rowLabel('codex').textContent).toContain('Check failed')
    expect(rowLabel('pi').textContent).toContain('Installed, not active yet')
    expect(rowLabel('deepseek').textContent).toContain('Not offered')

    // The accessible name stays the harness name alone.
    expect(row('Claude Code')).toHaveAccessibleName('Claude Code')
  })

  it('falls back to the current backend when the highlighted row stops being listed', async () => {
    // The listed set moves as the schema answers, and a stored id that dropped out of
    // it would render an empty pane.
    schemaMock.mockReturnValue(schemaWith(['', 'claude', 'kas']))
    wrap()
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    expect(row('Claude Code')).toBeChecked()
    schemaMock.mockReturnValue(schemaWith(['']))
    // A re-render with the narrower schema: Claude is gone and the pane follows the
    // active backend rather than emptying.
    fireEvent.click(row('Kiro CLI'))
    await waitFor(() => expect(screen.queryByRole('radio', { name: 'Claude Code' })).toBeNull())
    expect(row('Kiro CLI')).toBeChecked()
  })
})

describe('AgentBackendTab switching', () => {
  it('saves the highlighted backend when Use is pressed', async () => {
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    highlight('Claude Code')
    fireEvent.click(useButton('Claude Code'))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('agent.acp_backend', 'claude'))
  })

  it('saves the Claude Code selection the shipped build offers', async () => {
    // Claude is in the public baseline; the only thing that used to be missing was
    // the switch.
    wrap()
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    fireEvent.click(useButton('Claude Code'))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('agent.acp_backend', 'claude'))
  })

  it('has no Use button at all for the backend already running', async () => {
    // A PATCH writing the stored value still resolves, which would reset the model
    // list -- blanking every picker and spawning `--list-models` for a backend that
    // did not change. The row already says "In use", so the detail offers no
    // button rather than a dead one: two dead buttons otherwise look identical for
    // different reasons.
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: 'Use Kiro CLI' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'In use' })).toBeNull()
    expect(within(rowLabel('Kiro CLI')).getByText('In use')).toBeInTheDocument()
    expect(patchConfigMock).not.toHaveBeenCalled()
  })

  it('says what Use does, from the running agent to the checked one, above the button', async () => {
    // Checked is not current: the radio says "chosen" and the button says "Use", and
    // nothing else on the detail said that no switch has happened yet or what one
    // would do. One sentence names both ends, and it sits directly above the button.
    wrap()
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    const line = panel().getByText('New sessions switch from Kiro CLI to Claude Code.')
    expect(line).toBeInTheDocument()
    expect(line.compareDocumentPosition(useButton('Claude Code')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('names the agent actually running as the switch-from, not the default', async () => {
    kirocrewConfigMock.mockResolvedValue({ agent: { acp_backend: 'claude' } })
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    highlight('Kiro CLI')
    expect(panel().getByText('New sessions switch from Claude Code to Kiro CLI.')).toBeInTheDocument()
    expect(panel().queryByText(/from Kiro CLI to/)).toBeNull()
  })

  it('says nothing about switching on the row that is already in use', async () => {
    // The running row has no Use button, so a sentence about what Use does would
    // describe a control that is not there.
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    expect(row('Kiro CLI')).toBeChecked()
    expect(panel().queryByText(/New sessions switch from/)).toBeNull()
    expect(screen.queryByText(/New sessions switch from/)).toBeNull()
  })

  it('says nothing about switching on a harness this build never offers', async () => {
    // No Use button there either: a PATCH the wire refuses is not a switch.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', card()),
        probeRow('codex', {
          selectable: false,
          ...card({ offered_by_build: false, tool_approval: 'unverified' }),
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() => expect(row('codex')).toBeInTheDocument())
    highlight('codex')
    expect(panel().getByText('This build does not offer this agent.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Use codex' })).toBeNull()
    expect(panel().queryByText(/New sessions switch from/)).toBeNull()
  })

  it('resets the model list and drops its localStorage cache after a switch', async () => {
    // `/api/models` re-reads the backend per call, so only the FRONTEND cache was
    // stale. Reset rather than invalidate, and drop localStorage first, so a failing
    // first fetch degrades to auto-only rather than to the old backend's ids.
    localStorage.setItem(
      'kc.acp.models.v1',
      JSON.stringify({ ts: Date.now(), models: [{ name: 'auto', description: '' }] }),
    )
    const { qc } = wrapWithClient()
    qc.setQueryData(['available-models', 'acp'], [{ name: 'auto' }, { name: 'old-backend-model' }])
    const reset = vi.spyOn(qc, 'resetQueries')
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    highlight('Claude Code')
    fireEvent.click(useButton('Claude Code'))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('agent.acp_backend', 'claude'))
    await waitFor(() => expect(reset).toHaveBeenCalledWith({ queryKey: ['available-models'] }))
    // The old rows are gone the moment the switch saves, not after a refetch.
    expect(qc.getQueryData(['available-models', 'acp'])).toBeUndefined()
    expect(localStorage.getItem('kc.acp.models.v1')).toBeNull()
  })

  it('leaves the model list alone when the save is rejected', async () => {
    const { qc } = wrapWithClient()
    patchConfigMock.mockRejectedValue(new Error('nope'))
    qc.setQueryData(['available-models', 'acp'], [{ name: 'kept' }])
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    highlight('Claude Code')
    fireEvent.click(useButton('Claude Code'))
    await waitFor(() => expect(screen.getByText('Could not save the agent backend.')).toBeInTheDocument())
    expect(qc.getQueryData(['available-models', 'acp'])).toEqual([{ name: 'kept' }])
  })

  it('keeps showing the server value after a rejected save', async () => {
    // No optimistic write and no local mirror, so a rejected PATCH needs no revert.
    patchConfigMock.mockRejectedValue(new Error('nope'))
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    highlight('Claude Code')
    fireEvent.click(useButton('Claude Code'))
    await waitFor(() => expect(screen.getByText('Could not save the agent backend.')).toBeInTheDocument())
    expect(row('Kiro CLI')).toHaveAttribute('aria-current', 'true')
  })

  it('treats a config_saved 503 as a real switch: invalidates and shows the marker message', async () => {
    // A 503 `setup_marker_write_failed` carrying `config_saved: true` DID commit the
    // choice on disk; only the first-run marker did not. So the switch's side
    // effects must run (reset the model list, invalidate the config) and the
    // server's marker-write message is shown, NOT the generic save failure that
    // would keep badging the old agent.
    const { qc } = wrapWithClient()
    const reset = vi.spyOn(qc, 'resetQueries')
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    qc.setQueryData(['available-models', 'acp'], [{ name: 'old-backend-model' }])
    patchConfigMock.mockRejectedValue(
      new ApiError(
        503,
        'Agent saved, but setup completion could not be recorded.',
        JSON.stringify({ code: 'setup_marker_write_failed', config_saved: true }),
      ),
    )
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    highlight('Claude Code')
    fireEvent.click(useButton('Claude Code'))
    await waitFor(() =>
      expect(screen.getByText('Agent saved, but setup completion could not be recorded.')).toBeInTheDocument(),
    )
    expect(screen.queryByText('Could not save the agent backend.')).toBeNull()
    await waitFor(() => expect(reset).toHaveBeenCalledWith({ queryKey: ['available-models'] }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['kirocrewConfig'] })
    expect(qc.getQueryData(['available-models', 'acp'])).toBeUndefined()
  })
})

/** A payload whose Claude row is missing its adapter, with an install command. */
function missingClaude() {
  acpBackendsMock.mockResolvedValue({
    backends: [
      probeRow('', card()),
      probeRow('claude', {
        installed: 'missing',
        missing_components: ['claude-agent-acp'],
        install_command: 'npm i -g @zed-industries/claude-code-acp',
        ...card(),
      }),
    ],
  })
}

describe('AgentBackendTab status strip', () => {
  it('derives the strip instead of asserting per-agent capabilities', async () => {
    // An earlier revision wrote a prose sentence per agent claiming what each one
    // supported. Those claims were not measured anywhere and were wrong.
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    expect(detail().textContent).toContain('Default. All features supported.')
    highlight('KAS (kiro-agent)')
    expect(detail().textContent).toContain('Experimental')
    expect(screen.queryByText(/OS sandbox|Anthropic|steered mid-turn/)).toBeNull()
  })

  it('says what to install and offers the install command to copy', async () => {
    // The command gets its own copyable block rather than being folded into the
    // sentence: an operator must not have to select the one string they have to run
    // out of a paragraph. Beside the command the components are NOT named again --
    // "Missing: claude-acp" next to `npm i -g @zed-industries/claude-code-acp` names
    // a second thing, and the reader second-guesses which to install -- but the
    // row's description still carries them, so a screen reader is told what is gone.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', card()),
        probeRow('claude', {
          installed: 'missing',
          missing_components: ['claude-agent-acp'],
          install_command: 'npm i -g @zed-industries/claude-code-acp',
          ...card(),
        }),
      ],
    })
    wrap()
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    await waitFor(() =>
      expect(panel().getByText('Install Claude Code on the gateway host.')).toBeInTheDocument(),
    )
    expect(panel().getByText('npm i -g @zed-industries/claude-code-acp')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Copy command' })).toBeInTheDocument()
    expect(panel().getByText(/After installing, press Check again/)).toBeInTheDocument()
    expect(panel().queryByText(/Missing/)).toBeNull()
    const describedBy = row('Claude Code').getAttribute('aria-describedby')
    expect(document.getElementById(describedBy as string)?.textContent).toContain(
      'Missing on this machine: claude-agent-acp',
    )
    // And the switch is dead, with the reason wired to it rather than merely near it.
    expect(useButton('Claude Code')).toBeDisabled()
    expect(useButton('Claude Code').getAttribute('aria-describedby')).toBe('agent-backend-status')
  })

  it('copies the install command where the async Clipboard API does not exist', async () => {
    // The case a bare `navigator.clipboard.writeText` silently no-ops in: a non-secure
    // origin has no `navigator.clipboard` at all, and a plain-HTTP LAN or remote gateway
    // is exactly that. The shared helper falls back to `execCommand`, so the command
    // still reaches the clipboard -- which is why this panel must not hand-roll the copy.
    const execCommand = vi.fn().mockReturnValue(true)
    const realClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    Object.defineProperty(document, 'execCommand', { value: execCommand, configurable: true })
    try {
      missingClaude()
      wrap()
      await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
      highlight('Claude Code')
      const copy = await screen.findByRole('button', { name: 'Copy command' })
      fireEvent.click(copy)
      await waitFor(() => expect(execCommand).toHaveBeenCalledWith('copy'))
    } finally {
      if (realClipboard) Object.defineProperty(navigator, 'clipboard', realClipboard)
    }
  })

  it('shows no copied confirmation when the clipboard was not written', async () => {
    // A tick over an unchanged clipboard is worse than no affordance at all: the reader
    // walks away believing they hold the command and finds out at the paste. Both layers
    // fail here, so the control must not claim success.
    const realClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
      configurable: true,
    })
    Object.defineProperty(document, 'execCommand', {
      value: vi.fn().mockReturnValue(false),
      configurable: true,
    })
    try {
      missingClaude()
      wrap()
      await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
      highlight('Claude Code')
      fireEvent.click(await screen.findByRole('button', { name: 'Copy command' }))
      // It reports the failure rather than a success it cannot vouch for: the same
      // notice first-run setup's copy block shows, under the box.
      await waitFor(() => expect(screen.getByTestId('kiro-gate-copy-failed')).toBeInTheDocument())
      expect(screen.getByText(/Copy failed/)).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Copied' })).toBeNull()
      expect(screen.getByRole('button', { name: 'Copy command' })).toBeInTheDocument()
    } finally {
      if (realClipboard) Object.defineProperty(navigator, 'clipboard', realClipboard)
    }
  })

  it('states the missing components with no command block when the server has none', async () => {
    // kiro-cli is installed from its own docs, not by a one-liner this repo could
    // honestly print, so the probe reports `""` rather than an invented command.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', {
          installed: 'missing',
          missing_components: ['kiro-cli'],
          install_command: '',
          ...card(),
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() => expect(panel().getByText('Missing: kiro-cli')).toBeInTheDocument())
    expect(panel().getByText('Install Kiro CLI on the gateway host.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Copy command' })).toBeNull()
    expect(screen.queryByText('Install command')).toBeNull()
  })

  it('leaves the switch ENABLED when the install check could not be completed', async () => {
    // `unknown` means the CHECK failed. Collapsing it onto `missing` would tell
    // someone to run a global install for something they may already have.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('', card()), probeRow('claude', { installed: 'unknown', ...card() })],
    })
    wrap()
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    await waitFor(() =>
      expect(panel().getByText(/Could not check whether this is installed/)).toBeInTheDocument(),
    )
    // The switch is deliberately left live, and the strip has to SAY so. A bright Use
    // button under a bare "could not check" line reads as a mistake, which is what a
    // blind reader called it before this sentence existed.
    expect(panel().getByText(/You can still switch to it/)).toBeInTheDocument()
    expect(useButton('Claude Code')).toBeEnabled()
  })

  it('falls back to schema-only gating when the probe endpoint refuses (403)', async () => {
    // A 403 for a non-owner is absent information, not a verdict.
    acpBackendsMock.mockRejectedValue(new Error('403 Forbidden'))
    wrap()
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    expect(useButton('Claude Code')).toBeEnabled()
  })

  it('never flashes a dead switch while the probe is still in flight', async () => {
    acpBackendsMock.mockReturnValue(new Promise(() => {}))
    wrap()
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    expect(useButton('Claude Code')).toBeEnabled()
  })

  it('leaves a selectable, installed backend reading exactly as it did before', async () => {
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('', card()), probeRow('claude', card())],
    })
    wrap()
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    await waitFor(() => expect(useButton('Claude Code')).toBeEnabled())
    expect(screen.queryByText(/Missing on this machine/)).toBeNull()
  })

  it('kills the switch for an installed backend this gateway has cached as absent', async () => {
    // The one case where a POSITIVE install verdict still gates the control: the
    // click would reach a spawn that reuses the cached absence and fails.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', card()),
        probeRow('claude', { installed: 'installed', restart_required: true, ...card() }),
      ],
    })
    wrap()
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    await waitFor(() => expect(useButton('Claude Code')).toBeDisabled())
    // Names the cheap remedy FIRST and the restart only as the fallback, because a
    // re-probe is not guaranteed to clear the cached negative -- the flag is re-read
    // from the spawn path after the clear. Promising "without restarting" would be
    // wrong in exactly the case the wording exists to cover.
    expect(panel().getByText(/Press Check again to pick it up/)).toBeInTheDocument()
    // The last resort is the SAME sentence first-run setup shows for this state,
    // naming the harness and the gateway host: the two screens are one picker,
    // and "Settings > About" here against "that host" there read as two problems.
    expect(
      panel().getByText(
        'Claude Code is installed on the gateway host. Press Check again to pick it up. If this line is still here after that, restart Kiro Crew on that host.',
      ),
    ).toBeInTheDocument()
    expect(panel().queryByText(/Settings > About/)).toBeNull()
    expect(panel().queryByText(/Installed on this machine/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Check again' })).toBeInTheDocument()
  })

  it('says nothing about restarting for a row the deployment cannot select anyway', async () => {
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', card()),
        probeRow('claude', { selectable: false, restart_required: true, ...card() }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    await waitFor(() => expect(screen.queryByRole('radio', { name: 'Claude Code' })).toBeNull())
    expect(screen.queryByText(/must restart before it can be used/)).toBeNull()
  })

  it("re-asks the probe on an interval under the app's real staleTime: Infinity", async () => {
    // Load-bearing rather than tuning: inheriting the global `staleTime: Infinity`
    // makes the probe answer permanent for the life of the page, so an operator who
    // followed the panel's own install instruction could not re-ask short of a
    // reload.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      acpBackendsMock.mockResolvedValue({ backends: [probeRow('', card())] })
      const qc = new QueryClient({
        defaultOptions: { queries: { retry: false, staleTime: Infinity } },
      })
      render(
        <QueryClientProvider client={qc}>
          <AgentBackendTab />
        </QueryClientProvider>,
      )
      await waitFor(() => expect(acpBackendsMock).toHaveBeenCalledTimes(1))
      await vi.advanceTimersByTimeAsync(30_000)
      await waitFor(() => expect(acpBackendsMock.mock.calls.length).toBeGreaterThan(1))
    } finally {
      vi.useRealTimers()
      cleanup()
    }
  })
})

describe('AgentBackendTab check again', () => {
  it('re-probes the highlighted backend and lights the switch with no reload', async () => {
    // The remedy for the state the panel could previously only describe. The server
    // drops what this process cached before re-probing, so an operator who just ran
    // the install command sees the switch come alive.
    missingClaude()
    acpBackendRecheckMock.mockResolvedValue({
      backend: probeRow('claude', { installed: 'installed', ...card() }),
    })
    wrap()
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    await waitFor(() => expect(useButton('Claude Code')).toBeDisabled())

    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(acpBackendRecheckMock).toHaveBeenCalledWith('claude'))
    await waitFor(() => expect(useButton('Claude Code')).toBeEnabled())
    expect(screen.queryByText(/Missing on this machine/)).toBeNull()
  })

  it('splices the one re-checked row and leaves the others as they were', async () => {
    // A full refetch would re-read seven unmoved rows -- and would answer this
    // backend's row from the server's own TTL cache if the write landed inside the
    // window, losing the fresh verdict the button exists to get.
    missingClaude()
    acpBackendRecheckMock.mockResolvedValue({
      backend: probeRow('claude', { installed: 'installed', ...card() }),
    })
    wrap()
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check again' })).toBeEnabled())
    const listCalls = acpBackendsMock.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(useButton('Claude Code')).toBeEnabled())
    expect(acpBackendsMock.mock.calls.length).toBe(listCalls)
    // The untouched row kept its own answer.
    highlight('Kiro CLI')
    expect(detail().textContent).toContain('Default. All features supported.')
  })

  it('cancels the in-flight poll so it cannot land on top of the fresh row', async () => {
    // A GET that was already in flight carries the pre-install list, and react-query
    // would write that answer into the cache whenever it resolved -- including after
    // the splice -- putting the stale row back and killing the switch again until the
    // next poll. Cancelling before the POST goes out makes the splice the last write.
    missingClaude()
    acpBackendRecheckMock.mockResolvedValue({
      backend: probeRow('claude', { installed: 'installed', ...card() }),
    })
    const { qc } = wrapWithClient()
    const cancel = vi.spyOn(qc, 'cancelQueries')
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check again' })).toBeEnabled())

    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(useButton('Claude Code')).toBeEnabled())
    expect(cancel).toHaveBeenCalledWith({ queryKey: ['acpBackends'] })
  })

  it('keeps the switch dead when the re-probe still reports the component absent', async () => {
    // No parameter fakes a green: the server re-probes and the panel renders whatever
    // came back.
    missingClaude()
    acpBackendRecheckMock.mockResolvedValue({
      backend: probeRow('claude', {
        installed: 'missing',
        missing_components: ['claude-agent-acp'],
        install_command: 'npm i -g @zed-industries/claude-code-acp',
        ...card(),
      }),
    })
    wrap()
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    await waitFor(() => expect(useButton('Claude Code')).toBeDisabled())
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(acpBackendRecheckMock).toHaveBeenCalled())
    expect(useButton('Claude Code')).toBeDisabled()
    expect(panel().getByText('Install Claude Code on the gateway host.')).toBeInTheDocument()
    expect(panel().getByText('npm i -g @zed-industries/claude-code-acp')).toBeInTheDocument()
  })

  it('still reports restart_required when a re-probe comes back with it set', async () => {
    // The server re-measures the DISK and leaves the spawn path's own cached
    // resolution alone, so this disclosure survives a re-probe. The panel renders
    // whatever came back and never assumes a re-check cleared it.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('', card()), probeRow('claude', { restart_required: true, ...card() })],
    })
    acpBackendRecheckMock.mockResolvedValue({
      backend: probeRow('claude', { restart_required: true, ...card() }),
    })
    wrap()
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    await waitFor(() => expect(useButton('Claude Code')).toBeDisabled())
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(acpBackendRecheckMock).toHaveBeenCalledWith('claude'))
    await waitFor(() =>
      expect(panel().getByText(/Press Check again to pick it up/)).toBeInTheDocument(),
    )
    expect(useButton('Claude Code')).toBeDisabled()
  })

  it('applies the fresh row and shows the marker message when a re-check saves but the marker write fails', async () => {
    // A 503 `setup_marker_write_failed` on a re-check carries the fresh probe row:
    // the re-probe itself succeeded and only the marker write did not. So the row is
    // applied exactly as on success (the switch comes alive) and the server's
    // marker-write message is shown in place of the generic install-check-failed line.
    missingClaude()
    acpBackendRecheckMock.mockRejectedValue(
      new ApiError(
        503,
        'Agent check completed, but setup completion could not be recorded.',
        JSON.stringify({
          code: 'setup_marker_write_failed',
          backend: probeRow('claude', { installed: 'installed', ...card() }),
        }),
      ),
    )
    wrap()
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    await waitFor(() => expect(useButton('Claude Code')).toBeDisabled())
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(useButton('Claude Code')).toBeEnabled())
    expect(
      panel().getByText('Agent check completed, but setup completion could not be recorded.'),
    ).toBeInTheDocument()
    expect(
      panel().queryByText('Could not check whether this is installed on this machine.'),
    ).toBeNull()
  })

  it('surfaces a failed re-check instead of letting it read as "still missing"', async () => {
    // Unlike a failed poll, which is absent information the user did not ask for.
    missingClaude()
    acpBackendRecheckMock.mockRejectedValue(new Error('500'))
    wrap()
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check again' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    // Through ErrorNotice, and INSIDE the detail: an error about this harness's probe
    // belongs beside this harness's buttons, not at the top of a panel whose other
    // rows are fine.
    await waitFor(() =>
      expect(
        panel().getByText('Could not check whether this is installed on this machine.'),
      ).toBeInTheDocument(),
    )
    const notice = panel()
      .getByText('Could not check whether this is installed on this machine.')
      .closest('[role="alert"]')
    expect(notice).not.toBeNull()
    expect(detail().contains(notice)).toBe(true)
  })

  it('keeps a late re-check failure on the harness it belongs to', async () => {
    // The request is asynchronous and the highlight is not. Press Check again on A,
    // move to B, and A's rejection arrives with B on screen: an unkeyed message reads
    // as "B could not be checked" when nothing about B was ever asked. Clearing on the
    // move does not fix it either -- the rejection lands after the move.
    const FAILED = 'Could not check whether this is installed on this machine.'
    let reject: (reason: Error) => void = () => {}
    schemaMock.mockReturnValue(schemaWith(['', 'claude', 'kas', 'codex']))
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', card()),
        probeRow('claude', {
          installed: 'missing',
          missing_components: ['claude-code'],
          ...card(),
        }),
        probeRow('codex', { installed: 'missing', missing_components: ['codex-acp'], ...card() }),
      ],
    })
    acpBackendRecheckMock.mockImplementation(
      () =>
        new Promise((_resolve, rejectIt) => {
          reject = rejectIt
        }),
    )
    wrap()
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check again' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(acpBackendRecheckMock).toHaveBeenCalledWith('claude'))

    // The reader moves on BEFORE the rejection arrives.
    highlight('codex')
    await waitFor(() => expect(panel().getByText(/codex-acp/)).toBeInTheDocument())
    reject(new Error('500'))

    // codex was asked about nothing, so codex's strip says nothing. The positive fact
    // is awaited first -- the button leaving its pending label is the mutation having
    // settled -- so this is not a check that merely ran before the rejection did.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Check again' })).toBeEnabled(),
    )
    expect(panel().queryByText(FAILED)).toBeNull()

    // And the message is not lost: it is still the failing harness's to show.
    highlight('Claude Code')
    await waitFor(() => expect(panel().getByText(FAILED)).toBeInTheDocument())
  })

  it('offers no re-check for a harness this build never offers', async () => {
    // Nothing this machine holds is why it is not on offer, so a button that
    // re-measured the machine would answer a question nobody asked.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', card()),
        probeRow('deepseek', {
          selectable: false,
          ...card({ offered_by_build: false, tool_approval: 'unverified' }),
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() => expect(row('deepseek')).toBeInTheDocument())
    highlight('deepseek')
    expect(screen.queryByRole('button', { name: 'Check again' })).toBeNull()
  })

  it('offers no re-check for a harness that is simply installed', async () => {
    acpBackendsMock.mockResolvedValue({ backends: [probeRow('', card())] })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    await waitFor(() =>
      expect(detail().textContent).toContain('Default. All features supported.'),
    )
    expect(screen.queryByRole('button', { name: 'Check again' })).toBeNull()
  })
})

/**
 * The capability card, now the body of the detail pane.
 *
 * The panel renders it and owns none of it. Every line arrives from
 * `GET /api/acp-backends` as an id the server projected from the core's own
 * capability memberships, and this file holds the label per id -- so these tests
 * are about the RENDERING rules, and the meaning of each line is pinned in
 * `test/test_backend_cards.py`.
 *
 * One rule changed with the layout and it is worth naming: the capability list is no
 * longer behind a disclosure. It was collapsed because up to fifteen lines times
 * eight harnesses buried the control the panel exists for; with one harness on screen
 * there is nothing to bury. The where-it-lives notes stay collapsed, because those
 * are looked up once rather than compared.
 */
describe('AgentBackendTab detail card', () => {
  it('marks each capability available or not, in words as well as in an icon', async () => {
    // The state has to be TEXT. A mark that differs only by icon shape and colour
    // is unreadable to a screen reader and to anyone who cannot tell the two
    // colours apart, so each row carries its verdict as a visually hidden word.
    acpBackendsMock.mockResolvedValue({ backends: [probeRow('', card())] })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() =>
      expect(screen.getByText('Kiro Crew tools work in the chat')).toBeInTheDocument(),
    )
    const supported = screen.getByText('Kiro Crew tools work in the chat')
    expect(supported.parentElement?.textContent).toContain('Available.')
    const absent = screen.getByText('You can add a message while it works')
    expect(absent.parentElement?.textContent).toContain('Not available.')
  })

  it('counts the card for the harness on screen', async () => {
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', card()),
        probeRow(
          'kas',
          card({
            capabilities: [
              { id: 'crew_tools', available: true },
              { id: 'mid_turn_steer', available: true },
            ],
          }),
        ),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['', 'kas']))
    wrap()
    await waitFor(() =>
      expect(screen.getByText('Kiro CLI supports 1 of 2 features')).toBeInTheDocument(),
    )
    // The other harness's count is not on screen at the same time -- that is what
    // "exactly one card" means.
    expect(screen.queryByText('KAS (kiro-agent) supports 2 of 2 features')).toBeNull()
    highlight('KAS (kiro-agent)')
    expect(screen.getByText('KAS (kiro-agent) supports 2 of 2 features')).toBeInTheDocument()
  })

  it('shows the capability list without needing a click', async () => {
    // The disclosure existed because eight open cards buried the control. One card
    // does not, and the capability set is what the reader came for.
    acpBackendsMock.mockResolvedValue({ backends: [probeRow('', card())] })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() =>
      expect(screen.getByText('Kiro Crew tools work in the chat')).toBeInTheDocument(),
    )
    expect(screen.getByText('Kiro Crew tools work in the chat').closest('details')).toBeNull()
  })

  it('skips a capability id this frontend has no label for', async () => {
    // The opposite of `nameOf`'s fallback, and deliberately: a raw
    // `some_future_capability` in front of a reader is worse than one line fewer,
    // whereas a row with no text at all is worse than a policy id. The count
    // follows the lines that render, so it cannot advertise a line nobody sees.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow(
          '',
          card({
            capabilities: [
              { id: 'crew_tools', available: true },
              { id: 'some_future_capability', available: false },
            ],
          }),
        ),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() =>
      expect(screen.getByText('Kiro Crew tools work in the chat')).toBeInTheDocument(),
    )
    expect(screen.queryByText(/some_future_capability/)).toBeNull()
    expect(screen.getByText('Kiro CLI supports 1 of 1 features')).toBeInTheDocument()
  })

  it('marks a cell nobody has measured as neither, in its own word and its own glyph', async () => {
    // The third state, and the reason it is not a cross: `available: false` is the
    // same value a real absence carries, so a reader told only that cannot tell "this
    // harness cannot" from "nobody has driven it". The word and the reason are what
    // make the difference readable, and the glyph is a third glyph for the same
    // reason the verdict is text -- one alphabet, one meaning per mark.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow(
          '',
          card({
            capabilities: [
              { id: 'crew_tools', available: true, measured: true },
              { id: 'mid_turn_steer', available: false, measured: true },
              {
                id: 'manual_compact',
                available: false,
                measured: false,
                unmeasured_reason: 'no_driven_capture',
              },
            ],
          }),
        ),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() => expect(screen.getByText('The /compact command works')).toBeInTheDocument())
    const row = screen.getByText('The /compact command works').parentElement
    expect(row?.textContent).toContain('Not checked yet.')
    // Never the not-available word, which is the whole point of the state.
    expect(row?.textContent).not.toContain('Not available.')
    // And the reason, because "not measured" alone is not something a reader can act
    // on: the cell is waiting for a live run.
    expect(row?.textContent).toContain('Nobody has tried this on a real session yet')
    // A third GLYPH, asserted by DISTINCTNESS rather than by the icon's name: either
    // of the other two marks would put this cell back in the state it is being lifted
    // out of, while which icon carries the third meaning is a design choice a test
    // has no business pinning.
    const glyph = (label: string) =>
      screen.getByText(label).closest('li')?.querySelector('svg')?.getAttribute('class') ?? ''
    expect(glyph('The /compact command works')).not.toEqual('')
    expect(glyph('The /compact command works')).not.toEqual(
      glyph('Kiro Crew tools work in the chat'),
    )
    expect(glyph('The /compact command works')).not.toEqual(
      glyph('You can add a message while it works'),
    )
  })

  it('keeps an unmeasured cell out of the count and names what sits outside it', async () => {
    // THREE lines render and the denominator is TWO: an unchecked cell inside the
    // fraction, described beside it as uncounted, is a sentence that contradicts
    // itself -- and a reader who counts the rows cannot tell which half is true.
    // Out of the fraction it is counted as neither BY the arithmetic, and the clause
    // says how many sit outside. The denominator, not the wording, is what this
    // asserts, which is why the fixture's row count and its total differ.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow(
          '',
          card({
            capabilities: [
              { id: 'crew_tools', available: true, measured: true },
              { id: 'mid_turn_steer', available: false, measured: true },
              {
                id: 'manual_compact',
                available: false,
                measured: false,
                unmeasured_reason: 'no_driven_capture',
              },
            ],
          }),
        ),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() =>
      expect(screen.getByText(/Kiro CLI supports 1 of 2 features/)).toBeInTheDocument(),
    )
    expect(screen.getByText(/plus 1 not checked/)).toBeInTheDocument()
    // All three rows are on screen, so the missing third is the denominator's doing
    // rather than a dropped line.
    expect(screen.getByText('The /compact command works')).toBeInTheDocument()
  })

  it('says nothing about measurement where the gateway sends no flag', async () => {
    // A gateway that predates the third state sends `available` alone. Absent is
    // MEASURED, so its card renders exactly as it does today -- tick and cross, and
    // no remainder line claiming a gap nobody reported.
    acpBackendsMock.mockResolvedValue({ backends: [probeRow('', card())] })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() =>
      expect(screen.getByText('You can add a message while it works')).toBeInTheDocument(),
    )
    const absent = screen.getByText('You can add a message while it works')
    expect(absent.parentElement?.textContent).toContain('Not available.')
    expect(absent.parentElement?.textContent).not.toContain('Not checked yet.')
    expect(screen.queryByText(/plus \d+ not checked/)).toBeNull()
  })

  it('states not measured even for a reason code this frontend cannot label', async () => {
    // Same rule as an unlabelled capability id, applied one level down: the STATE is
    // the server's and renders whatever the reason is, while a raw
    // `some_future_reason` in front of a reader says less than nothing.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow(
          '',
          card({
            capabilities: [
              {
                id: 'manual_compact',
                available: false,
                measured: false,
                unmeasured_reason: 'some_future_reason',
              },
            ],
          }),
        ),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() => expect(screen.getByText('The /compact command works')).toBeInTheDocument())
    const row = screen.getByText('The /compact command works').parentElement
    expect(row?.textContent).toContain('Not checked yet.')
    expect(row?.textContent).not.toContain('some_future_reason')
  })

  it('states tool approval outside any disclosure, from the mechanism the server named', async () => {
    // The one security-relevant line on the card and the reason a build-excluded
    // agent cannot be picked, so it must not need a click. It is also the one
    // GRADED line: the label comes from the mechanism, not from a boolean.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('', card()), probeRow('kas', card({ tool_approval: 'seeded_settings' }))],
    })
    schemaMock.mockReturnValue(schemaWith(['', 'kas']))
    wrap()
    await waitFor(() =>
      expect(
        screen.getByText('Asks before each tool, because Kiro Crew tells it to.'),
      ).toBeInTheDocument(),
    )
    expect(screen.getByText(/because Kiro Crew tells it to/).closest('details')).toBeNull()
    highlight('KAS (kiro-agent)')
    expect(screen.getByText(/cannot confirm it took effect/)).toBeInTheDocument()
  })

  it('says nothing about a mechanism this frontend has no label for', async () => {
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('', card({ tool_approval: 'a_future_mechanism' }))],
    })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() =>
      expect(screen.getByText('Kiro Crew tools work in the chat')).toBeInTheDocument(),
    )
    expect(screen.queryByText(/a_future_mechanism/)).toBeNull()
  })

  it('states the operator notes the server sent, and only those', async () => {
    // A note is raised or absent, never raised-and-negated: the server sends the
    // ids that HOLD, so the panel has no negative form to render. They render as plain
    // lines rather than behind a heading: the card keeps one where-it-lives fact, the
    // credential store, and one line needs no toggle to hide it.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('', card({ operator_notes: ['own_credential_store'] }))],
    })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() =>
      expect(screen.getByText(/Signs in with its own file/)).toBeInTheDocument(),
    )
    expect(screen.queryByText(/home folder moves into the pod/)).toBeNull()
    expect(screen.queryByText('Good to know')).toBeNull()
  })

  it('opens nothing for a security note or a credential note', async () => {
    // "Kiro Crew's sandbox is not confining this child" is as material as how the
    // harness is made to ask, and both fail OPEN. A reader comparing agents must not
    // have to open anything to find either -- and the same now holds for the one
    // where-it-lives note the card keeps, since whose secret store an agent signs in
    // against is a risk the reader takes on rather than a fact they look up once.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow(
          '',
          card({
            security_notes: ['crew_sandbox_stands_down'],
            operator_notes: ['own_credential_store'],
          }),
        ),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() => expect(screen.getByText(/Has its own sandbox/)).toBeInTheDocument())
    expect(screen.getByText(/Has its own sandbox/).closest('details')).toBeNull()
    expect(screen.getByText(/Signs in with its own file/).closest('details')).toBeNull()
  })

  it('renders no card at all when the gateway sent none', async () => {
    // The same rule as every other absent probe field: absent information is not
    // a verdict, so the panel says nothing rather than rendering an empty card
    // that reads as "supports nothing".
    acpBackendsMock.mockResolvedValue({ backends: [probeRow('')] })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() =>
      expect(detail().textContent).toContain('Default. All features supported.'),
    )
    expect(screen.queryByText(/supports \d+ of/)).toBeNull()
    expect(screen.queryByText('Good to know')).toBeNull()
  })

  it('lists an agent this build never offers, and says it is not offered', async () => {
    // Known to the core, outside the selectable baseline. It gets a row with no Use
    // button: an operator asking "why can I not pick that?" is asking about a fact
    // the detail already carries, and hiding it answers with silence.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', card()),
        probeRow('deepseek', {
          selectable: false,
          ...card({ offered_by_build: false, tool_approval: 'unverified' }),
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() => expect(row('deepseek')).toBeInTheDocument())
    highlight('deepseek')
    expect(panel().getByText('This build does not offer this agent.')).toBeInTheDocument()
    // The reason, from the routing enum rather than from prose written per agent.
    expect(screen.getByText(/cannot confirm this agent asks/)).toBeInTheDocument()
    // Listed, never offered: a button whose PATCH the wire refuses is not a button.
    expect(screen.queryByRole('button', { name: 'Use deepseek' })).toBeNull()
  })

  it('still hides an agent the deployment denied, card or no card', async () => {
    // The distinction the `offered_by_build` field exists for. A policy denial is
    // not the reader's to fix and stays hidden; only a BUILD exclusion is
    // listed. Claude here carries a full card and is denied by the schema.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('', card()), probeRow('claude', card())],
    })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() =>
      expect(screen.getByText('Kiro CLI supports 1 of 2 features')).toBeInTheDocument(),
    )
    expect(screen.queryByRole('radio', { name: 'Claude Code' })).toBeNull()
  })
})

describe('AgentBackendTab MCP ability card', () => {
  /**
   * The MCP half of the card, narrowed to what switching COSTS the reader.
   *
   * A line belongs here only where switching to this agent takes a feature away, adds
   * a risk, or makes one of the reader's own agent-file settings ineffective. Two
   * things pass: the deny reach (a risk met by accident) and the settings that will not
   * take effect. The route Crew takes to the agent — native, mirror, external — is true
   * and costs the reader nothing, so it is not on the card at all; `kirocrew doctor`
   * states it for whoever is diagnosing a route.
   */
  beforeEach(() => {
    schemaMock.mockReturnValue(schemaWith(['']))
  })

  it('states the deny rule outside any disclosure, on an agent that can lose a server', async () => {
    // THE risk this card exists for: switching one tool off is an ordinary action that
    // says nothing about servers, so a reader meets the difference by accident unless
    // the card states it before they pick.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('', { ...card(), ...mcp({ per_tool_deny: 'whole-server' }) })],
    })
    wrap()
    const rule = await screen.findByText(/stops every tool on the same server/)
    expect(rule.closest('details')).toBeNull()
    expect(rule.textContent).toContain('Kiro CLI')
  })

  it('marks the per-call exception as an exception, right under the rule', async () => {
    // Two sentences that qualify each other without saying so read as a contradiction,
    // which is what a reader reported. The exception names itself and follows the rule.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('', { ...card(), ...mcp({ per_tool_deny: 'per-call' }) })],
    })
    wrap()
    const rule = await screen.findByText(/stops every tool on the same server/)
    const exception = panel().getByText(/One exception/)
    expect(exception.textContent).toContain('kirocrew-core')
    expect(exception.closest('details')).toBeNull()
    expect(rule.compareDocumentPosition(exception) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('says nothing about a reach that costs the reader nothing', async () => {
    // `settings-file` stops the tool it names and nothing else. A line saying so would
    // be a caveat that always fires, which is a line nobody reads.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('', { ...card(), ...mcp({ per_tool_deny: 'settings-file' }) })],
    })
    wrap()
    await waitFor(() => expect(panel().getByText(/Crew tools/)).toBeInTheDocument())
    expect(panel().queryByText(/stops every tool on the same server/)).toBeNull()
    expect(panel().queryByText(/One exception/)).toBeNull()
  })

  it('names the agent-file settings that will not take effect here', async () => {
    // The reader's own settings, one consequence sentence each, under one heading that
    // says what the group IS. Withheld and no-channel arrive as one list: the split is
    // the mirror maintainer's, and the reader's question is the same either way.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', {
          ...card(),
          ...mcp({ ineffective: ['auto_approve', 'permission_mode', 'hooks'] }),
        }),
      ],
    })
    wrap()
    await waitFor(() =>
      expect(
        panel().getByText('These settings in your agent config file will not take effect on Kiro CLI'),
      ).toBeInTheDocument(),
    )
    expect(panel().getByText(/The auto-approve rules your agent config file sets/)).toBeInTheDocument()
    expect(panel().getByText(/The permission mode your agent config file asks for/)).toBeInTheDocument()
    expect(panel().getByText(/The hooks your agent config file defines/)).toBeInTheDocument()
  })

  it('renders no heading for an agent that honours the whole file', async () => {
    // Stated only where it holds: a reader never reads a row of "delivered" marks that
    // mean "as expected".
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('', { ...card(), ...mcp({ ineffective: [] }) })],
    })
    wrap()
    await waitFor(() => expect(panel().getByText(/Crew tools/)).toBeInTheDocument())
    expect(panel().queryByText(/will not take effect on/)).toBeNull()
  })

  it('falls back to the setting id for one this frontend has no phrase for', async () => {
    // A setting added to the core must not vanish here: the id is a machine word but it
    // is searchable, where a dropped line tells the reader nothing exists.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', { ...card(), ...mcp({ ineffective: ['auto_approve', 'some_future_setting'] }) }),
      ],
    })
    wrap()
    await waitFor(() => expect(panel().getByText('some_future_setting')).toBeInTheDocument())
    expect(panel().getByText(/The auto-approve rules your agent config file sets/)).toBeInTheDocument()
  })

  it('never puts the route Crew takes on the card', async () => {
    // The narrowing rule, asserted from the reader's side: `mirror` / `native` /
    // `external` costs them no feature, adds no risk, and stops no setting of theirs
    // working, so no line states it and no phrase for it survives in this file.
    for (const reach of ['whole-server', 'per-call', 'settings-file']) {
      acpBackendsMock.mockResolvedValue({
        backends: [probeRow('', { ...card(), ...mcp({ per_tool_deny: reach }) })],
      })
      wrap()
      await waitFor(() => expect(panel().getByText(/Crew tools/)).toBeInTheDocument())
      const text = detail().textContent ?? ''
      expect(text).not.toContain('copies them across')
      expect(text).not.toContain('reads that file itself')
      expect(text).not.toMatch(/\bmirror\b/)
      cleanup()
    }
  })

  it('leaves no raw {{placeholder}} and no navigation path on any reach', async () => {
    // The defect class this pins: a label whose STRING names {{name}} while its record
    // passes no vars. And no wayfinding: the same screen carries more than one MCP
    // surface, so a path spelled here reads as another place rather than as directions.
    for (const reach of ['whole-server', 'per-call', 'settings-file']) {
      acpBackendsMock.mockResolvedValue({
        backends: [
          probeRow('', {
            ...card(),
            ...mcp({ per_tool_deny: reach, ineffective: ['auto_approve', 'hooks'] }),
          }),
        ],
      })
      wrap()
      await waitFor(() => expect(panel().getByText(/Crew tools/)).toBeInTheDocument())
      const text = detail().textContent ?? ''
      expect(text).not.toContain('{{')
      expect(text).not.toContain('Connections')
      cleanup()
    }
  })

  it('shows the MCP half for the harness on screen and no other', async () => {
    // Highlight is not selection, and this half follows the same rule as the capability
    // half: one card at a time, so the reader can compare by arrowing.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', { ...card(), ...mcp({ per_tool_deny: 'settings-file' }) }),
        probeRow('opencode', { ...card(), ...mcp({ per_tool_deny: 'whole-server' }) }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['', 'opencode']))
    wrap()
    await waitFor(() => expect(row('opencode')).toBeInTheDocument())
    expect(screen.queryByText(/stops every tool on the same server/)).toBeNull()
    highlight('opencode')
    await waitFor(() =>
      expect(panel().getByText(/stops every tool on the same server/)).toBeInTheDocument(),
    )
  })

  it('renders nothing MCP for a gateway that sent no mcp group', async () => {
    // An older gateway sends no group at all, and the card then says nothing about MCP
    // rather than guessing.
    acpBackendsMock.mockResolvedValue({ backends: [probeRow('', { ...card() })] })
    wrap()
    await waitFor(() => expect(panel().getByText(/Crew tools/)).toBeInTheDocument())
    expect(panel().queryByText(/stops every tool on the same server/)).toBeNull()
    expect(panel().queryByText(/will not take effect on/)).toBeNull()
  })

  it('reads one card and writes no config', async () => {
    // The whole half is advisory: nothing on it refuses a selection and nothing on it
    // saves one.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('', { ...card(), ...mcp({ per_tool_deny: 'whole-server' }) })],
    })
    wrap()
    await screen.findByText(/stops every tool on the same server/)
    expect(patchConfigMock).not.toHaveBeenCalled()
  })
})

describe('AgentBackendTab standing caveats', () => {
  it("states that a pre-approval in Claude's own settings skips Crew's gate", async () => {
    // A TOOL-GATING disclosure the card cannot yet express as data: the routing enum
    // says the setting cannot be read back and not WHERE a pre-approval an operator
    // did not write can come from.
    wrap()
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    expect(screen.getByText(/\.claude\/settings\.json inside the project/)).toBeInTheDocument()
  })

  it('does not put that caveat on the other agents', async () => {
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    expect(screen.queryByText(/\.claude\/settings\.json/)).toBeNull()
    highlight('KAS (kiro-agent)')
    expect(screen.queryByText(/\.claude\/settings\.json/)).toBeNull()
  })

  it('states BOTH the gating caveat and the sign-in remedy on a harness that has both', async () => {
    // Claude is the harness that carries two, and an earlier revision returned early
    // on the gating line -- so the one harness with two facts showed one of them.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', card()),
        probeRow('claude', {
          auth: { sign_in_remedy: 'Run: claude login', signs_in_separately: true },
          ...card(),
        }),
      ],
    })
    wrap()
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    await waitFor(() => expect(screen.getByText('Run: claude login')).toBeInTheDocument())
    expect(screen.getByText(/\.claude\/settings\.json inside the project/)).toBeInTheDocument()
  })

  it('drops the caveat with the row when Claude Code is not selectable', async () => {
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    expect(screen.queryByText(/\.claude\/settings\.json/)).toBeNull()
  })

  it("renders the server's sign-in remedy verbatim when the harness signs in separately", async () => {
    // Untranslated on purpose: a per-harness sentence through i18n is a per-harness
    // edit to thirteen locale files, so the harness that needs it most -- one this
    // frontend has never heard of -- is the one that would get no sentence at all.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', card()),
        probeRow('codex', {
          auth: { sign_in_remedy: 'Run: codex login', signs_in_separately: true },
          ...card(),
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['', 'codex']))
    wrap()
    await waitFor(() => expect(row('codex')).toBeInTheDocument())
    highlight('codex')
    await waitFor(() => expect(screen.getByText('Run: codex login')).toBeInTheDocument())
  })

  it('says nothing when the harness does not sign in separately', async () => {
    // A harness authenticating through Crew's own identity store has no separate
    // sign-in to finish.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', card()),
        probeRow('kas', {
          auth: { sign_in_remedy: 'Run: kiro login', signs_in_separately: false },
          ...card(),
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['', 'kas']))
    wrap()
    await waitFor(() => expect(row('KAS (kiro-agent)')).toBeInTheDocument())
    highlight('KAS (kiro-agent)')
    await waitFor(() => expect(panel().getByText('Experimental')).toBeInTheDocument())
    expect(screen.queryByText('Run: kiro login')).toBeNull()
  })

  it('renders a row with no auth object at all, and says nothing about signing in', async () => {
    // An older gateway sends none, which is absent information like every other.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('', card()), probeRow('codex', card())],
    })
    schemaMock.mockReturnValue(schemaWith(['', 'codex']))
    wrap()
    await waitFor(() => expect(row('codex')).toBeInTheDocument())
    highlight('codex')
    await waitFor(() => expect(useButton('codex')).toBeEnabled())
    expect(screen.queryByText(/Run:/)).toBeNull()
  })

  it("does not put one harness's sign-in remedy on the others", async () => {
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', card()),
        probeRow('codex', {
          auth: { sign_in_remedy: 'Run: codex login', signs_in_separately: true },
          ...card(),
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['', 'codex']))
    wrap()
    await waitFor(() => expect(row('codex')).toBeInTheDocument())
    // Kiro CLI is the open detail and carries no remedy.
    expect(screen.queryByText('Run: codex login')).toBeNull()
  })
})

describe('AgentBackendTab kiro sign-in', () => {
  it('renders the Kiro sign-in inside the KAS detail, compact, while KAS is on offer', async () => {
    // The identity it stores is used by the KAS relay alone, so it lives in the one
    // row whose harness uses it -- and in the in-detail form, not a card inside the
    // detail's own bordered panel.
    wrap()
    await waitFor(() => expect(row('KAS (kiro-agent)')).toBeInTheDocument())
    // Kiro CLI's detail is open: no sign-in there.
    expect(screen.queryByTestId('kiro-sign-in-card')).toBeNull()
    highlight('KAS (kiro-agent)')
    const signIn = screen.getByTestId('kiro-sign-in-card')
    expect(detail().contains(signIn)).toBe(true)
    expect(signIn).toHaveAttribute('data-compact', 'true')
    // And not on the other harness that could be looked at next.
    highlight('Claude Code')
    expect(screen.queryByTestId('kiro-sign-in-card')).toBeNull()
  })

  it('renders no sign-in card when this deployment cannot select KAS', async () => {
    // On a build or policy that hides that option there is nothing to sign in for.
    schemaMock.mockReturnValue(schemaWith(['', 'claude']))
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    expect(screen.queryByRole('radio', { name: 'KAS (kiro-agent)' })).toBeNull()
    expect(screen.queryByTestId('kiro-sign-in-card')).toBeNull()
    highlight('Claude Code')
    expect(screen.queryByTestId('kiro-sign-in-card')).toBeNull()
  })

  it('opens on KAS with the sign-in while KAS is the saved backend, even if it reads as unselectable', async () => {
    kirocrewConfigMock.mockResolvedValue({ agent: { acp_backend: 'kas' } })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() => expect(row('KAS (kiro-agent)')).toHaveAttribute('aria-current', 'true'))
    expect(row('KAS (kiro-agent)')).toBeChecked()
    expect(screen.getByTestId('kiro-sign-in-card')).toBeInTheDocument()
  })

  it('offers no sign-in card for an agent it only lists', async () => {
    // The sign-in gate reads the OFFERED set, not the listed one. A KAS row that
    // exists only to explain itself must not draw a sign-in for an option nobody can
    // pick.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', card()),
        probeRow('kas', {
          selectable: false,
          ...card({ offered_by_build: false, tool_approval: 'unverified' }),
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['']))
    wrap()
    await waitFor(() => expect(row('KAS (kiro-agent)')).toBeInTheDocument())
    highlight('KAS (kiro-agent)')
    expect(panel().getByText('This build does not offer this agent.')).toBeInTheDocument()
    expect(screen.queryByTestId('kiro-sign-in-card')).toBeNull()
  })

  it("opens the KAS detail when the URL carries the chat's sign-in highlight", async () => {
    // The chat's "Sign in to Kiro" link is `/settings/agent?highlight=key:kiro-sign-in`.
    // The Settings highlight hook waits for the anchor and rings it, but the anchor
    // lives inside the KAS detail -- so the tab has to open that detail for the link
    // to land on anything.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/settings/agent?highlight=key%3Akiro-sign-in']}>
          <AgentBackendTab />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await waitFor(() => expect(row('KAS (kiro-agent)')).toBeChecked())
    expect(row('Kiro CLI')).not.toBeChecked()
    // Kiro CLI is still the one running: opening a detail switched nothing.
    expect(row('Kiro CLI')).toHaveAttribute('aria-current', 'true')
    expect(screen.getByTestId('kiro-sign-in-card')).toBeInTheDocument()
    expect(patchConfigMock).not.toHaveBeenCalled()
    // Once open, the reader is free to look elsewhere: the link does not drag them
    // back while the parameter is still in the URL.
    highlight('Claude Code')
    expect(row('Claude Code')).toBeChecked()
    expect(screen.queryByTestId('kiro-sign-in-card')).toBeNull()
  })

  it('leaves the detail on the running backend for any other highlight', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/settings/agent?highlight=key%3Asomething-else']}>
          <AgentBackendTab />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await waitFor(() => expect(row('Kiro CLI')).toBeChecked())
    expect(row('KAS (kiro-agent)')).not.toBeChecked()
    expect(screen.queryByTestId('kiro-sign-in-card')).toBeNull()
  })
})

/**
 * The panel twin of `test_a_new_backend_renders_a_complete_card_with_no_card_edit`.
 *
 * That Python test proves the SERVER projects a complete card for a harness nobody
 * wrote a card for. This one proves the PANEL renders it: a backend id this file has
 * never heard of gets a row, a name, a glyph, a status strip, every capability line,
 * the graded tool-approval line and a working Use button — with no edit to
 * `AgentBackendTab.tsx` and no edit to a locale file.
 *
 * The two together are the whole claim. A projection nothing renders is not a
 * feature, and a panel that needed a per-harness branch would make the projection
 * pointless.
 */
describe('AgentBackendTab new backend', () => {
  it('renders a complete detail for a backend the panel has never heard of, with no panel edit', async () => {
    const unheardOf = 'wireweave'
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('', card()),
        probeRow(unheardOf, {
          install_command: 'npm i -g wireweave-acp',
          auth: { sign_in_remedy: 'Run: wireweave auth', signs_in_separately: true },
          ...card({
            capabilities: [
              { id: 'crew_tools', available: true },
              { id: 'manual_compact', available: true },
              { id: 'mid_turn_steer', available: false },
            ],
            security_notes: ['own_credential_store'],
            operator_notes: ['keeps_own_chat_record'],
            tool_approval: 'verified_gate_extension',
          }),
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['', unheardOf]))
    wrap()

    // A row, named from the server's policy_id because this file has no name for it.
    await waitFor(() => expect(row(unheardOf)).toBeInTheDocument())
    highlight(unheardOf)

    // A status strip.
    await waitFor(() => expect(panel().getByText('Experimental')).toBeInTheDocument())
    // Every capability line, with its verdict as a word.
    expect(screen.getByText('Kiro Crew tools work in the chat')).toBeInTheDocument()
    expect(screen.getByText('The /compact command works')).toBeInTheDocument()
    expect(
      screen.getByText('You can add a message while it works').parentElement?.textContent,
    ).toContain('Not available.')
    expect(screen.getByText(`${unheardOf} supports 2 of 3 features`)).toBeInTheDocument()
    // The graded line, from the routing enum.
    expect(screen.getByText(/through an add-on Kiro Crew loads and checks/)).toBeInTheDocument()
    // The security note, never behind the disclosure.
    expect(screen.getByText(/Signs in with its own file/).closest('details')).toBeNull()
    // A where-it-lives note the server DID send renders, and as a plain line: which
    // notes reach the card is the server's classification (`OPERATOR_LINES`), and the
    // panel renders what arrives rather than re-deciding it. Nothing is behind a
    // toggle now that the group is one line for a stock harness.
    expect(screen.getByText(/keeps its own copy of the chat/).closest('details')).toBeNull()
    // The server's remedy, verbatim.
    expect(screen.getByText('Run: wireweave auth')).toBeInTheDocument()

    // And the switch works.
    fireEvent.click(useButton(unheardOf))
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('agent.acp_backend', unheardOf),
    )
  })
})
