/**
 * AgentBackendTab: what the open detail shows for a harness.
 *
 * The capability card and its MCP half, the standing caveats, the Kiro sign-in card
 * in the KAS detail, and a backend id the panel has never heard of. The list, the
 * switch, the status strip and Check again are pinned in AgentBackendTab.test.tsx.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import React from 'react'

const {
  patchConfigMock,
  kirocrewConfigMock,
  schemaMock,
  acpBackendsMock,
  acpBackendRecheckMock,
} = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() => Promise.resolve({ agent: { acp_backend: 'kiro' } })),
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
// renders unconditionally, so its absence in a test can only be the tab's gate.
vi.mock('../pages/developer/KiroSignInCard', () => ({
  KiroSignInCard: ({ compact }: { compact?: boolean }) => (
    <div data-testid="kiro-sign-in-card" data-compact={compact ? 'true' : 'false'} />
  ),
}))

// Same sentinel treatment for the engine card under it: its behaviour is pinned in
// KasBundleCard.test.tsx, and the real card would need the kas-bundle API.
vi.mock('../pages/developer/KasBundleCard', () => ({
  KasBundleCard: () => <div data-testid="kas-bundle-card" />,
}))

import { AgentBackendTab } from '../pages/developer/AgentBackendTab'
import {
  schemaWith,
  card,
  probeRow,
  wrap,
  row,
  useButton,
  highlight,
  detail,
  panel,
} from './AgentBackendTabTestUtils'

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
      // (`registry.PerToolDeny.costs_whole_server`), so the fixture ships one too and
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

beforeEach(() => {
  localStorage.clear()
  patchConfigMock.mockClear()
  patchConfigMock.mockResolvedValue({})
  kirocrewConfigMock.mockClear()
  kirocrewConfigMock.mockResolvedValue({ agent: { acp_backend: 'kiro' } })
  acpBackendRecheckMock.mockClear()
  // The shipped core: every known agent is selectable. Claude Code is in the public
  // baseline because acp/client.py owns its whole spawn path and the adapter it needs
  // is a public npm package -- the only thing that used to be missing was the switch.
  schemaMock.mockReturnValue(schemaWith(['kiro', 'claude', 'kas']))
  // Default to NO probe information — a 404 from a gateway that predates the
  // endpoint. Every test that does not opt in therefore pins the pre-probe
  // behaviour: schema-only gating, nothing dead or annotated by the probe.
  acpBackendsMock.mockClear()
  acpBackendsMock.mockRejectedValue(new Error('404 Not Found'))
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
    acpBackendsMock.mockResolvedValue({ backends: [probeRow('kiro', card())] })
    schemaMock.mockReturnValue(schemaWith(['kiro']))
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
        probeRow('kiro', card()),
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
    schemaMock.mockReturnValue(schemaWith(['kiro', 'kas']))
    wrap()
    await waitFor(() =>
      expect(screen.getByText('Kiro CLI supports 1 of 2 features')).toBeInTheDocument(),
    )
    // The other harness's count is not on screen at the same time -- that is what
    // "exactly one card" means.
    expect(screen.queryByText('Kiro Agent supports 2 of 2 features')).toBeNull()
    highlight('Kiro Agent')
    expect(screen.getByText('Kiro Agent supports 2 of 2 features')).toBeInTheDocument()
  })

  it('shows the capability list without needing a click', async () => {
    // The disclosure existed because eight open cards buried the control. One card
    // does not, and the capability set is what the reader came for.
    acpBackendsMock.mockResolvedValue({ backends: [probeRow('kiro', card())] })
    schemaMock.mockReturnValue(schemaWith(['kiro']))
    wrap()
    await waitFor(() =>
      expect(screen.getByText('Kiro Crew tools work in the chat')).toBeInTheDocument(),
    )
    expect(screen.getByText('Kiro Crew tools work in the chat').closest('details')).toBeNull()
  })

  it('folds the detail lines behind a disclosure and keeps them out of the count', async () => {
    // The card counts the curated core only. A detail line is a real difference,
    // so it is shown -- one click away, and never in "supports N of M".
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow(
          'kiro',
          card({
            capabilities: [{ id: 'crew_tools', available: true }],
            details: [
              { id: 'member_saved_agent', available: true },
              { id: 'markdown_agents', available: false },
            ],
          }),
        ),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro']))
    wrap()
    await waitFor(() =>
      expect(screen.getByText('Kiro CLI supports 1 of 1 features')).toBeInTheDocument(),
    )
    const saved = screen.getByText('Mates load their saved agent')
    const folded = saved.closest('details')
    expect(folded).not.toBeNull()
    expect(folded?.open).toBe(false)
    expect(folded?.querySelector('summary')?.textContent).toBe(
      'Other differences (not counted)',
    )
    expect(saved.parentElement?.textContent).toContain('Available.')
    const markdown = screen.getByText('Loads custom agents you define in a markdown file')
    expect(markdown.parentElement?.textContent).toContain('Not available.')
    expect(screen.getByText('Kiro Crew tools work in the chat').closest('details')).toBeNull()
  })

  it('renders no disclosure when the gateway sends no detail lines', async () => {
    acpBackendsMock.mockResolvedValue({ backends: [probeRow('kiro', card())] })
    schemaMock.mockReturnValue(schemaWith(['kiro']))
    wrap()
    await waitFor(() =>
      expect(screen.getByText('Kiro Crew tools work in the chat')).toBeInTheDocument(),
    )
    expect(screen.queryByText('Other differences (not counted)')).toBeNull()
  })

  it('skips a capability id this frontend has no label for', async () => {
    // The opposite of `nameOf`'s fallback, and deliberately: a raw
    // `some_future_capability` in front of a reader is worse than one line fewer,
    // whereas a row with no text at all is worse than a policy id. The count
    // follows the lines that render, so it cannot advertise a line nobody sees.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow(
          'kiro',
          card({
            capabilities: [
              { id: 'crew_tools', available: true },
              { id: 'some_future_capability', available: false },
            ],
          }),
        ),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro']))
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
          'kiro',
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
    schemaMock.mockReturnValue(schemaWith(['kiro']))
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
          'kiro',
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
    schemaMock.mockReturnValue(schemaWith(['kiro']))
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
    acpBackendsMock.mockResolvedValue({ backends: [probeRow('kiro', card())] })
    schemaMock.mockReturnValue(schemaWith(['kiro']))
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
          'kiro',
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
    schemaMock.mockReturnValue(schemaWith(['kiro']))
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
      backends: [probeRow('kiro', card()), probeRow('kas', card({ tool_approval: 'seeded_settings' }))],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro', 'kas']))
    wrap()
    await waitFor(() =>
      expect(
        screen.getByText('Asks before each tool, because Kiro Crew tells it to.'),
      ).toBeInTheDocument(),
    )
    expect(screen.getByText(/because Kiro Crew tells it to/).closest('details')).toBeNull()
    highlight('Kiro Agent')
    expect(screen.getByText(/cannot confirm it took effect/)).toBeInTheDocument()
  })

  it('says nothing about a mechanism this frontend has no label for', async () => {
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('kiro', card({ tool_approval: 'a_future_mechanism' }))],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro']))
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
      backends: [probeRow('kiro', card({ operator_notes: ['own_credential_store'] }))],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro']))
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
          'kiro',
          card({
            security_notes: ['crew_sandbox_stands_down'],
            operator_notes: ['own_credential_store'],
          }),
        ),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro']))
    wrap()
    await waitFor(() => expect(screen.getByText(/Has its own sandbox/)).toBeInTheDocument())
    expect(screen.getByText(/Has its own sandbox/).closest('details')).toBeNull()
    expect(screen.getByText(/Signs in with its own file/).closest('details')).toBeNull()
  })

  it('renders no card at all when the gateway sent none', async () => {
    // The same rule as every other absent probe field: absent information is not
    // a verdict, so the panel says nothing rather than rendering an empty card
    // that reads as "supports nothing".
    acpBackendsMock.mockResolvedValue({ backends: [probeRow('kiro')] })
    schemaMock.mockReturnValue(schemaWith(['kiro']))
    wrap()
    await waitFor(() =>
      expect(detail().textContent).toContain('Kiro CLI is installed and ready to use.'),
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
        probeRow('kiro', card()),
        probeRow('deepseek', {
          selectable: false,
          ...card({ offered_by_build: false, tool_approval: 'unverified' }),
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro']))
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
      backends: [probeRow('kiro', card()), probeRow('claude', card())],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro']))
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
    schemaMock.mockReturnValue(schemaWith(['kiro']))
  })

  it('states the deny rule outside any disclosure, on an agent that can lose a server', async () => {
    // THE risk this card exists for: switching one tool off is an ordinary action that
    // says nothing about servers, so a reader meets the difference by accident unless
    // the card states it before they pick.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('kiro', { ...card(), ...mcp({ per_tool_deny: 'whole-server' }) })],
    })
    wrap()
    const rule = await screen.findByText(/stops every tool on the same server/)
    expect(rule.closest('details')).toBeNull()
    expect(rule.textContent).toContain('Kiro CLI')
  })

  it('links the deny rule to the MCP Servers list, where the reader acts on it', async () => {
    // #21303: the rule names a cost on whole servers; the link lands on the list of
    // them, which the Connections page opens from its `?view=` param.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('kiro', { ...card(), ...mcp({ per_tool_deny: 'whole-server' }) })],
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/settings/agent']}><AgentBackendTab /></MemoryRouter>
      </QueryClientProvider>,
    )
    const rule = await screen.findByText(/stops every tool on the same server/)
    const link = within(rule).getByRole('link', { name: 'Open the MCP Servers list in Connections' })
    expect(link).toHaveAttribute('href', '/capabilities?tab=mcp&view=mcp-servers')
  })

  it('marks the per-call exception as an exception, right under the rule', async () => {
    // Two sentences that qualify each other without saying so read as a contradiction,
    // which is what a reader reported. The exception names itself and follows the rule.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('kiro', { ...card(), ...mcp({ per_tool_deny: 'per-call' }) })],
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
      backends: [probeRow('kiro', { ...card(), ...mcp({ per_tool_deny: 'settings-file' }) })],
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
        probeRow('kiro', {
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
      backends: [probeRow('kiro', { ...card(), ...mcp({ ineffective: [] }) })],
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
        probeRow('kiro', { ...card(), ...mcp({ ineffective: ['auto_approve', 'some_future_setting'] }) }),
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
        backends: [probeRow('kiro', { ...card(), ...mcp({ per_tool_deny: reach }) })],
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
          probeRow('kiro', {
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
        probeRow('kiro', { ...card(), ...mcp({ per_tool_deny: 'settings-file' }) }),
        probeRow('opencode', { ...card(), ...mcp({ per_tool_deny: 'whole-server' }) }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro', 'opencode']))
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
    acpBackendsMock.mockResolvedValue({ backends: [probeRow('kiro', { ...card() })] })
    wrap()
    await waitFor(() => expect(panel().getByText(/Crew tools/)).toBeInTheDocument())
    expect(panel().queryByText(/stops every tool on the same server/)).toBeNull()
    expect(panel().queryByText(/will not take effect on/)).toBeNull()
  })

  it('reads one card and writes no config', async () => {
    // The whole half is advisory: nothing on it refuses a selection and nothing on it
    // saves one.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('kiro', { ...card(), ...mcp({ per_tool_deny: 'whole-server' }) })],
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
    highlight('Kiro Agent')
    expect(screen.queryByText(/\.claude\/settings\.json/)).toBeNull()
  })

  it('states BOTH the gating caveat and the sign-in remedy on a harness that has both', async () => {
    // Claude is the harness that carries two, and an earlier revision returned early
    // on the gating line -- so the one harness with two facts showed one of them.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('kiro', card()),
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
    schemaMock.mockReturnValue(schemaWith(['kiro']))
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
        probeRow('kiro', card()),
        probeRow('codex', {
          auth: { sign_in_remedy: 'Run: codex login', signs_in_separately: true },
          ...card(),
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro', 'codex']))
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
        probeRow('kiro', card()),
        probeRow('kas', {
          auth: { sign_in_remedy: 'Run: kiro login', signs_in_separately: false },
          ...card(),
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro', 'kas']))
    wrap()
    await waitFor(() => expect(row('Kiro Agent')).toBeInTheDocument())
    highlight('Kiro Agent')
    await waitFor(() => expect(detail().textContent).toContain('Default.'))
    expect(screen.queryByText('Run: kiro login')).toBeNull()
  })

  it('renders a row with no auth object at all, and says nothing about signing in', async () => {
    // An older gateway sends none, which is absent information like every other.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('kiro', card()), probeRow('codex', card())],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro', 'codex']))
    wrap()
    await waitFor(() => expect(row('codex')).toBeInTheDocument())
    highlight('codex')
    await waitFor(() => expect(useButton('codex')).toBeEnabled())
    expect(screen.queryByText(/Run:/)).toBeNull()
  })

  it("does not put one harness's sign-in remedy on the others", async () => {
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('kiro', card()),
        probeRow('codex', {
          auth: { sign_in_remedy: 'Run: codex login', signs_in_separately: true },
          ...card(),
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro', 'codex']))
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
    await waitFor(() => expect(row('Kiro Agent')).toBeInTheDocument())
    // Kiro CLI's detail is open: no sign-in there.
    expect(screen.queryByTestId('kiro-sign-in-card')).toBeNull()
    highlight('Kiro Agent')
    const signIn = screen.getByTestId('kiro-sign-in-card')
    expect(detail().contains(signIn)).toBe(true)
    expect(signIn).toHaveAttribute('data-compact', 'true')
    // And not on the other harness that could be looked at next.
    highlight('Claude Code')
    expect(screen.queryByTestId('kiro-sign-in-card')).toBeNull()
  })

  it('keeps the KAS engine card off the harness row; it lives in Feature Previews', async () => {
    // The route is in shadow testing, so its switch is not in plain view of every
    // KAS user: the KAS detail shows the sign-in and no engine card.
    wrap()
    await waitFor(() => expect(row('Kiro Agent')).toBeInTheDocument())
    highlight('Kiro Agent')
    expect(screen.getByTestId('kiro-sign-in-card')).toBeInTheDocument()
    expect(screen.queryByTestId('kas-bundle-card')).toBeNull()
  })

  it('renders no sign-in card when this deployment cannot select KAS', async () => {
    // On a build or policy that hides that option there is nothing to sign in for.
    schemaMock.mockReturnValue(schemaWith(['kiro', 'claude']))
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    expect(screen.queryByRole('radio', { name: 'Kiro Agent' })).toBeNull()
    expect(screen.queryByTestId('kiro-sign-in-card')).toBeNull()
    expect(screen.queryByTestId('kas-bundle-card')).toBeNull()
    highlight('Claude Code')
    expect(screen.queryByTestId('kiro-sign-in-card')).toBeNull()
  })

  it('opens on KAS with the sign-in while KAS is the saved backend, even if it reads as unselectable', async () => {
    kirocrewConfigMock.mockResolvedValue({ agent: { acp_backend: 'kas' } })
    schemaMock.mockReturnValue(schemaWith(['kiro']))
    wrap()
    await waitFor(() => expect(row('Kiro Agent')).toHaveAttribute('aria-current', 'true'))
    expect(row('Kiro Agent')).toBeChecked()
    expect(screen.getByTestId('kiro-sign-in-card')).toBeInTheDocument()
  })

  it('offers no sign-in card for an agent it only lists', async () => {
    // The sign-in gate reads the OFFERED set, not the listed one. A KAS row that
    // exists only to explain itself must not draw a sign-in for an option nobody can
    // pick.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('kiro', card()),
        probeRow('kas', {
          selectable: false,
          ...card({ offered_by_build: false, tool_approval: 'unverified' }),
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro']))
    wrap()
    await waitFor(() => expect(row('Kiro Agent')).toBeInTheDocument())
    highlight('Kiro Agent')
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
    await waitFor(() => expect(row('Kiro Agent')).toBeChecked())
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
    expect(row('Kiro Agent')).not.toBeChecked()
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
        probeRow('kiro', card()),
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
    schemaMock.mockReturnValue(schemaWith(['kiro', unheardOf]))
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
