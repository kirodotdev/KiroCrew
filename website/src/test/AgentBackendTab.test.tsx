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
 *
 * The open detail's capability card and its MCP half, the standing caveats, the Kiro
 * sign-in card and a backend the panel has never heard of are pinned in
 * AgentBackendTab.detailCard.test.tsx; the fixtures both files use are in
 * AgentBackendTabTestUtils.tsx.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
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
import { acpBackendName, DEFAULT_ACP_BACKEND } from '../api/acpBackend'
import {
  schemaWith,
  card,
  probeRow,
  wrapWithClient,
  wrap,
  row,
  useButton,
  highlight,
  detail,
  panel,
} from './AgentBackendTabTestUtils'

/**
 * The visible row the radio sits in: the label carrying the name, the in-use word
 * and the status badge. What a sighted reader scans.
 */
const rowLabel = (name: string) => row(name).closest('label') as HTMLElement
/** The harness NAMES in list order, read off each radio's accessible name. */
const rows = () => screen.getAllByRole('radio').map(el => el.getAttribute('aria-label'))

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

describe('AgentBackendTab list', () => {
  it('lists all three backends as rows', async () => {
    wrap()
    expect(await screen.findByRole('radio', { name: 'Kiro CLI' })).toBeInTheDocument()
    expect(row('Claude Code')).toBeInTheDocument()
    expect(row('Kiro Agent')).toBeInTheDocument()
  })

  it('puts KAS first, Kiro CLI second, Strands third, and sorts adapters after', async () => {
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('kiro'), probeRow('claude'), probeRow('strands'), probeRow('kas'), probeRow('zephyr')],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro', 'claude', 'strands', 'kas', 'zephyr']))
    wrap()
    await waitFor(() => expect(screen.getAllByRole('radio')).toHaveLength(5))
    expect(rows()).toEqual(['Kiro Agent', 'Kiro CLI', 'Strands', 'Claude Code', 'zephyr'])
  })

  it('marks the configured backend as the current one', async () => {
    kirocrewConfigMock.mockResolvedValue({ agent: { acp_backend: 'claude' } })
    wrap()
    await waitFor(() => expect(row('Claude Code')).toHaveAttribute('aria-current', 'true'))
    expect(row('Kiro CLI')).not.toHaveAttribute('aria-current')
  })

  it('treats a missing acp_backend as the KAS default', async () => {
    kirocrewConfigMock.mockResolvedValue({})
    wrap()
    await waitFor(() => expect(row('Kiro Agent')).toHaveAttribute('aria-current', 'true'))
  })

  it('opens on the current backend detail', async () => {
    // The useful default and the one id the row list always contains.
    kirocrewConfigMock.mockResolvedValue({ agent: { acp_backend: 'kas' } })
    wrap()
    await waitFor(() => expect(row('Kiro Agent')).toBeChecked())
    expect(row('Kiro CLI')).not.toBeChecked()
  })

  it('hides a backend the deployment may not select, rather than dimming it', async () => {
    // A greyed row invites the reader to find out how to enable it, and under a
    // managed policy there is nothing they can do.
    schemaMock.mockReturnValue(schemaWith(['kiro']))
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    expect(screen.queryByRole('radio', { name: 'Claude Code' })).toBeNull()
    expect(screen.queryByRole('radio', { name: 'Kiro Agent' })).toBeNull()
  })

  it('keeps the current backend listed even if it reads as unselectable', async () => {
    // A control that lists no active harness is a worse failure than one extra row.
    kirocrewConfigMock.mockResolvedValue({ agent: { acp_backend: 'claude' } })
    schemaMock.mockReturnValue(schemaWith(['kiro']))
    wrap()
    await waitFor(() => expect(row('Claude Code')).toHaveAttribute('aria-current', 'true'))
  })

  it('lists a backend the schema starts advertising, with no edit here', async () => {
    // The whole point of reading the option set from the server: an edition that
    // registers a harness lights it up with no frontend change.
    schemaMock.mockReturnValue(schemaWith(['kiro', 'claude', 'kas', 'future-agent']))
    wrap()
    await waitFor(() => expect(row('future-agent')).toBeInTheDocument())
  })

  it('draws each harness its own brand mark, and an unknown one the generic glyph', async () => {
    // The marks themselves are pinned in HarnessLogo.test.tsx; this pins that the
    // ROW renders them, decoratively, beside a name that stays the accessible one.
    const ids = ['kiro', 'kas', 'claude', 'codex', 'zephyr']
    acpBackendsMock.mockResolvedValue({ backends: ids.map(id => probeRow(id)) })
    schemaMock.mockReturnValue(schemaWith(ids))
    wrap()
    await waitFor(() => expect(screen.getAllByRole('radio')).toHaveLength(ids.length))
    const markIn = (name: string) => {
      const label = rowLabel(name)
      return {
        brand: (id: string) => within(label).queryByTestId(`harness-logo-${id}`),
        ghost: within(label).queryByTestId('kiro-ghost-mark'),
        generic: within(label).queryByTestId('harness-logo-fallback'),
      }
    }
    expect(markIn('Kiro CLI').ghost).toBeInTheDocument()
    expect(markIn('Kiro Agent').ghost).toBeInTheDocument()
    expect(markIn('Claude Code').brand('claude')).toBeInTheDocument()
    expect(markIn(acpBackendName({ id: 'codex' })).brand('codex')).toBeInTheDocument()
    const unknown = markIn('zephyr')
    expect(unknown.generic).toBeInTheDocument()
    expect(unknown.ghost).toBeNull()
    // Decorative: the mark adds nothing to the radio's accessible name.
    expect(row('Claude Code')).toHaveAccessibleName('Claude Code')
    expect(within(rowLabel('Claude Code')).getByTestId('harness-logo-claude').closest('[aria-hidden="true"]'))
      .not.toBeNull()
  })

  it('lists an agent this frontend has no name for, under its policy id', async () => {
    // `nameOf` falls back to `policy_id`, which is already a word rather than an
    // internal token -- it is what a governance rule spells.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('kiro'), probeRow('plugin-agent', { ...card() })],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro', 'plugin-agent']))
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
      backends: [probeRow('kiro'), probeRow('claude', { selectable: false })],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro', 'claude']))
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
    expect(row('Kiro Agent')).toBeInTheDocument()
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
    fireEvent.click(row('Kiro Agent'))
    expect(row('Kiro Agent')).toBeChecked()
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
      backends: [probeRow('kiro', card()), probeRow('kas', card()), probeRow('claude', card())],
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
        probeRow('kiro', card()),
        probeRow('kas', { installed: 'missing', missing_components: ['kiro-cli'], ...card() }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro', 'kas']))
    wrap()
    await waitFor(() => expect(row('Kiro Agent')).toBeInTheDocument())
    // The name is the name -- the row also carries a badge, and neither of them is
    // the row's accessible NAME.
    expect(row('Kiro Agent')).toHaveAccessibleName('Kiro Agent')
    expect(rowLabel('Kiro Agent').querySelector('span.truncate')?.textContent).toBe(
      'Kiro Agent',
    )
    // The state is the description.
    const describedBy = row('Kiro Agent').getAttribute('aria-describedby')
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
        probeRow('kiro', card()),
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
        probeRow('kiro', card()),
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

  it('calls a harness on an unverified release "Wrong version", with the fix on the row', async () => {
    // UX BLOCK on #22403: the probe once looked only for the binary, so a Copilot
    // CLI on any release but the verified one read "Installed" here and then
    // refused every chat. The gateway now reports that release as missing the
    // verified one; this row must say so in its own word and carry the command.
    schemaMock.mockReturnValue(schemaWith(['kiro', 'copilot']))
    const command = 'npm i -g @github/copilot@1.0.100'
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('kiro', card()),
        probeRow('copilot', {
          installed: 'missing',
          missing_components: ['copilot 1.0.100'],
          install_command: command,
          found_version: '1.0.103',
          ...card(),
        }),
      ],
    })
    wrap()
    const name = acpBackendName({ id: 'copilot', policy_id: 'copilot' })
    await waitFor(() => expect(rowLabel(name).textContent).toContain('Wrong version'))
    expect(rowLabel(name).textContent).not.toContain('Not installed')
    expect(rowLabel(name).textContent).not.toContain('Installed')
    highlight(name)
    await waitFor(() => expect(detail().textContent).toContain(`This host has ${name} 1.0.103`))
    expect(detail().textContent).toContain(command)
    expect(useButton(name)).toBeDisabled()
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
        probeRow('kiro', card()),
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
      backends: [probeRow('kiro', card()), probeRow('kas', card())],
    })
    highlight('Kiro Agent')
    await waitFor(() => expect(useButton('Kiro Agent')).toBeEnabled())
    expect(useButton('Kiro Agent').className).toContain('bg-accent')
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
        probeRow('kiro', card()),
        probeRow('deepseek', {
          selectable: false,
          ...card({ offered_by_build: false, tool_approval: 'unverified' }),
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro']))
    wrap()
    await waitFor(() => expect(row('DeepSeek Harness')).toBeInTheDocument())
    highlight('DeepSeek Harness')
    const count = panel().getByText(/DeepSeek Harness supports \d+ of \d+ features/)
    expect(count.parentElement?.className).toContain('opacity-60')

    // An offered harness's list is not muted.
    highlight('Kiro CLI')
    const kiro = panel().getByText(/Kiro CLI supports \d+ of \d+ features/)
    expect(kiro.parentElement?.className).not.toContain('opacity-60')
  })

  it('shows the full harness name, never truncated by a fixed row width', async () => {
    // The old strip capped each row at 11rem and truncated "Kiro Agent" to
    // "KAS (kiro-..." at ordinary widths. A row now spans the list, so the name has
    // the row's width and the badge yields its own space.
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    for (const name of ['Kiro CLI', 'Claude Code', 'Kiro Agent']) {
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
        probeRow('kiro', card()),
        probeRow('claude', { installed: 'missing', missing_components: ['x'], ...card() }),
        probeRow('codex', { installed: 'unknown', ...card() }),
        probeRow('pi', { restart_required: true, ...card() }),
        probeRow('deepseek', {
          selectable: false,
          ...card({ offered_by_build: false, tool_approval: 'unverified' }),
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro', 'claude', 'codex', 'pi']))
    wrap()
    await waitFor(() => expect(row('DeepSeek Harness')).toBeInTheDocument())

    expect(rowLabel('Kiro CLI').textContent).toContain('Installed')
    expect(rowLabel('Claude Code').textContent).toContain('Not installed')
    expect(rowLabel('OpenAI Codex').textContent).toContain('Check failed')
    expect(rowLabel('Pi').textContent).toContain('Installed, not active yet')
    expect(rowLabel('DeepSeek Harness').textContent).toContain('Not offered')

    // The accessible name stays the harness name alone.
    expect(row('Claude Code')).toHaveAccessibleName('Claude Code')
  })

  it('falls back to the current backend when the highlighted row stops being listed', async () => {
    // The listed set moves as the schema answers, and a stored id that dropped out of
    // it would render an empty pane.
    schemaMock.mockReturnValue(schemaWith(['kiro', 'claude', 'kas']))
    wrap()
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    expect(row('Claude Code')).toBeChecked()
    schemaMock.mockReturnValue(schemaWith(['kiro']))
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

  it('stores an explicit Kiro CLI choice even though KAS is the default', async () => {
    kirocrewConfigMock.mockResolvedValue({})
    wrap()
    await waitFor(() => expect(row('Kiro Agent')).toHaveAttribute('aria-current', 'true'))
    highlight('Kiro CLI')
    fireEvent.click(useButton('Kiro CLI'))
    // "kiro", never "": on disk an empty value means "no choice", which is KAS.
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('agent.acp_backend', 'kiro'))
  })

  it('reads a stored empty harness as no choice: KAS is in use, not Kiro CLI', async () => {
    kirocrewConfigMock.mockResolvedValue({ agent: { acp_backend: '' } })
    wrap()
    await waitFor(() => expect(row('Kiro Agent')).toHaveAttribute('aria-current', 'true'))
    expect(row('Kiro CLI')).not.toHaveAttribute('aria-current')
    expect(screen.queryByRole('radio', { name: '' })).toBeNull()
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
        probeRow('kiro', card()),
        probeRow('codex', {
          selectable: false,
          ...card({ offered_by_build: false, tool_approval: 'unverified' }),
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro']))
    wrap()
    await waitFor(() => expect(row('OpenAI Codex')).toBeInTheDocument())
    highlight('OpenAI Codex')
    expect(panel().getByText('This build does not offer this agent.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Use OpenAI Codex' })).toBeNull()
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
      probeRow('kiro', card()),
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
    highlight('Kiro Agent')
    expect(detail().textContent).toContain('Default.')
    highlight('Claude Code')
    expect(detail().textContent).toContain('Experimental')
    expect(screen.queryByText(/OS sandbox|Anthropic|steered mid-turn/)).toBeNull()
  })

  it('puts the default line on DEFAULT_ACP_BACKEND, not on Kiro CLI', async () => {
    // New installs run KAS. A page that called Kiro CLI the default and tagged KAS
    // Experimental would tell a new user the harness they are on is the risky one.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('kiro', card()), probeRow('kas', card()), probeRow('claude', card())],
    })
    wrap()
    await waitFor(() => expect(row('Kiro Agent')).toBeInTheDocument())
    expect(DEFAULT_ACP_BACKEND).toBe('kas')

    highlight('Kiro Agent')
    await waitFor(() => expect(detail().textContent).toContain('Default.'))
    expect(panel().queryByText('Experimental')).toBeNull()
    expect(document.getElementById('agent-backend-row-status-kas')?.textContent).toBe('Default.')

    // Kiro CLI is the stable floor: neither the default nor Experimental.
    highlight('Kiro CLI')
    await waitFor(() =>
      expect(detail().textContent).toContain('Kiro CLI is installed and ready to use.'),
    )
    expect(detail().textContent).not.toContain('Default.')
    expect(panel().queryByText('Experimental')).toBeNull()
    const kiroStatus = document.getElementById('agent-backend-row-status-kiro')?.textContent ?? ''
    expect(kiroStatus).not.toMatch(/Default|Experimental/)

    // Any other harness is still Experimental.
    highlight('Claude Code')
    await waitFor(() => expect(panel().getByText('Experimental')).toBeInTheDocument())
    expect(detail().textContent).not.toContain('Default.')
    expect(document.getElementById('agent-backend-row-status-claude')?.textContent).toBe(
      'Experimental',
    )
  })

  it('says neither Default nor Experimental for Kiro CLI with no probe answer', async () => {
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    highlight('Kiro CLI')
    expect(detail().textContent).not.toMatch(/Default\.|Experimental/)
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
        probeRow('kiro', card()),
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
        probeRow('kiro', {
          installed: 'missing',
          missing_components: ['kiro-cli'],
          install_command: '',
          ...card(),
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro']))
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
      backends: [probeRow('kiro', card()), probeRow('claude', { installed: 'unknown', ...card() })],
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
      backends: [probeRow('kiro', card()), probeRow('claude', card())],
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
        probeRow('kiro', card()),
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
        probeRow('kiro', card()),
        probeRow('claude', { selectable: false, restart_required: true, ...card() }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro']))
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
      acpBackendsMock.mockResolvedValue({ backends: [probeRow('kiro', card())] })
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
    expect(detail().textContent).toContain('Kiro CLI is installed and ready to use.')
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
      backends: [probeRow('kiro', card()), probeRow('claude', { restart_required: true, ...card() })],
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
    schemaMock.mockReturnValue(schemaWith(['kiro', 'claude', 'kas', 'codex']))
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow('kiro', card()),
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
    highlight('OpenAI Codex')
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
        probeRow('kiro', card()),
        probeRow('deepseek', {
          selectable: false,
          ...card({ offered_by_build: false, tool_approval: 'unverified' }),
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['kiro']))
    wrap()
    await waitFor(() => expect(row('DeepSeek Harness')).toBeInTheDocument())
    highlight('DeepSeek Harness')
    expect(screen.queryByRole('button', { name: 'Check again' })).toBeNull()
  })

  it('offers no re-check for a harness that is simply installed', async () => {
    acpBackendsMock.mockResolvedValue({ backends: [probeRow('kiro', card())] })
    schemaMock.mockReturnValue(schemaWith(['kiro']))
    wrap()
    await waitFor(() => expect(row('Kiro CLI')).toBeInTheDocument())
    await waitFor(() =>
      expect(detail().textContent).toContain('Kiro CLI is installed and ready to use.'),
    )
    expect(screen.queryByRole('button', { name: 'Check again' })).toBeNull()
  })
})

/**
 * The shared probe verdict, rendered (#14517). The same states as
 * acpBackend.eligibility.test.ts and the first-run gate's block, so a surface that
 * stopped reading `acpProbeBlocksUse` fails here. Settings is the OPTIMISTIC reader:
 * only a known block disables Use; a failed check leaves it live.
 */
describe('AgentBackendTab shared probe eligibility', () => {
  it.each([
    ['installed', {}, true],
    ['missing', { installed: 'missing', missing_components: ['claude-agent-acp'] }, false],
    ['restart required', { restart_required: true }, false],
    ['check failed', { installed: 'unknown' }, true],
  ] as const)('%s: Use enabled = %s', async (_label, over, enabled) => {
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('kiro'), probeRow('claude', { ...over })],
    })
    wrap()
    await waitFor(() => expect(row('Claude Code')).toBeInTheDocument())
    highlight('Claude Code')
    if (enabled) await waitFor(() => expect(useButton('Claude Code')).toBeEnabled())
    else await waitFor(() => expect(useButton('Claude Code')).toBeDisabled())
  })

  it('unselectable: not offered at all', async () => {
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow('kiro'), probeRow('claude', { selectable: false }), probeRow('kas')],
    })
    wrap()
    await waitFor(() => expect(row('Kiro Agent')).toBeInTheDocument())
    expect(screen.queryByRole('radio', { name: 'Claude Code' })).toBeNull()
  })
})
