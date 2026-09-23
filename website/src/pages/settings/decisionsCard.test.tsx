/**
 * The Decisions (Jev) card in Settings > Developer > Feature Previews.
 *
 * What is under test is the one thing this card does differently from every
 * other preview in that section: its switch is the KEYSTONE
 * `decisions_consent.json`, read and written through `/api/decisions/consent`,
 * not a per-device localStorage flag and not a `config.json` value. So the cases
 * are the states a backend-backed switch can be in — read pending, read failed,
 * gateway too old (404), consent off, consent on, write failed — and the claim
 * in each is the same one: the card never offers a write it cannot make, and
 * never stays silent about why. The sampling share still comes from
 * `config.json`, so the two reads are stubbed separately.
 *
 * A third read decides whether the card exists at all: `GET /api/dashboard/config`
 * reports `decisions_enabled`, the `capabilities.decisions` governance answer, and
 * a fleet that pinned the seam off gets no card — see the last block.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { api, type DecisionsConsentData } from '../../api/client'
import { PREVIEW_FLAG_PREFIX } from '../../utils/previewFlags'
import { FeaturePreviewsSection } from './FeaturePreviewsSection'

/** Rendered through the whole section, because that is where the card ships. */
function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter><FeaturePreviewsSection /></MemoryRouter>
    </QueryClientProvider>,
  )
}

/** The card's own switch. Named in full so "Decisions" cannot match another row. */
const decisionsSwitch = () => screen.getByRole('switch', { name: 'Decisions (Jev)' })

/** An older gateway has no consent route: the client rejects with a 404. */
const notFound = () => Object.assign(new Error('Not Found'), { status: 404 })

const ENDPOINT = 'https://api.typesafe.ai/v1/systemone'

/**
 * The point rows the gateway projects, in the order its registry lists them.
 *
 * Spelled out here rather than derived, because what the card must do with them is
 * exactly to draw whatever arrives: a test that built these from the same table the
 * card reads would assert nothing about the projection.
 */
const pointsOf = (
  enabled: boolean,
  toolArgs = false,
  compaction = false,
  memoryText = false,
  judgeProvider = 'auto',
) => {
  const granted: Record<string, boolean> = {
    tool_args: toolArgs,
    compaction,
    memory_text: memoryText,
  }
  const status = (scope: string | null) =>
    !enabled ? 'off' : scope && !granted[scope] ? 'needs_scope' : 'active'
  return [
    { id: 'skills.select', needs_scope: null, status: status(null) },
    { id: 'tool.risk', needs_scope: 'tool_args', status: status('tool_args') },
    { id: 'message.steer', needs_scope: null, status: status(null) },
    { id: 'model.route', needs_scope: null, status: status(null) },
    { id: 'compaction.keep', needs_scope: 'compaction', status: status('compaction') },
    { id: 'memory.recall', needs_scope: 'memory_text', status: status('memory_text') },
    // The judge, whose row the gateway resolves from the LANE as well as the keystone:
    // its small-model lane needs neither the endpoint consent nor a scope, so that lane
    // reports active with the switch off. `auto` reaches it too while this build
    // registers no scope for the point -- the gate resolves `auto` against Jev being
    // ARMED, and an unregistered scope is not armed.
    {
      id: 'nudge.wake',
      needs_scope: null,
      lane: judgeProvider === 'jev' ? 'jev' : 'llm',
      status: judgeProvider === 'jev' ? (enabled ? 'needs_scope' : 'off') : 'active',
    },
  ]
}

/** A consent payload as the gateway returns it, bound to the default endpoint. */
const consentOf = (enabled: boolean, overrides: Partial<DecisionsConsentData> = {}): DecisionsConsentData => ({
  enabled,
  endpoint: enabled ? ENDPOINT : '',
  configured_endpoint: ENDPOINT,
  permits: enabled,
  // The tool-argument egress scope. FALSE by default here on purpose: that is what
  // a keystone recorded before the scope existed reads as, and it is the state the
  // overwhelming majority of consented installs are in.
  tool_args: false,
  // The whole-transcript egress scope, false by default for the same reason: it is
  // what a keystone recorded before the scope existed reads as, and no narrower yes
  // grants it.
  compaction: false,
  // And the recalled-memory scope, false on the same terms.
  memory_text: false,
  points: pointsOf(
    enabled,
    overrides.tool_args === true,
    overrides.compaction === true,
    overrides.memory_text === true,
  ),
  ...overrides,
})

/** The row for one point, by the plain-words name the reader sees. */
const pointRow = (name: string) =>
  screen.getByRole('tab', {
    name: new RegExp(name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i'),
  })

/** Open a point's own panel. Highlighting is not selection: nothing else moves. */
const openPoint = (name: string) => pointRow(name).click()

/** The point that carries each scope, for a test that needs to reach its switch. */
const SCOPE_POINT: Record<string, string> = {
  tool_args: 'Risky tool-call notes',
  compaction: 'Which tool calls a compaction would keep (measured only)',
  memory_text: 'Which recalled memories reach the prompt',
}

/** The accessible name of each scope's consent switch, as the card labels it. */
const SCOPE_SWITCH: Record<string, string> = {
  tool_args: 'Also send tool-call arguments so Jev can flag risky calls',
  compaction: 'Also send the conversation and tool-call inputs so Jev can score compaction',
  memory_text:
    'Also send snippets of recalled memories so Jev can drop the ones that do not help',
}

/**
 * Open the panel that owns a scope and hand back its switch.
 *
 * Every scope switch lives on its own point's panel, and one panel renders at a
 * time, so a test cannot reach two scopes at once and must say which point it means.
 * That is the shape itself, not an accident of it: the switch is the OK for THAT
 * point, and drawing it beside the main switch is what made it ambiguous.
 */
async function openScope(scope: 'tool_args' | 'compaction' | 'memory_text') {
  await waitFor(() => {
    expect(pointRow(SCOPE_POINT[scope])).toBeInTheDocument()
  })
  openPoint(SCOPE_POINT[scope])
  return await waitFor(() => screen.getByRole('switch', { name: SCOPE_SWITCH[scope] }))
}

/**
 * Open a scope's own panel and report whether its switch is there.
 *
 * The absence half of `openScope`, and it has to open the panel for the answer to
 * mean anything: one panel renders at a time, so "the switch is not on screen" is
 * true of every point that is not selected, whatever the card would draw for THIS
 * one. A test asserting absence without opening the panel passes on a card that
 * offers the switch with consent withheld.
 */
async function scopeSwitchOnPanel(scope: 'tool_args' | 'compaction' | 'memory_text') {
  await waitFor(() => {
    expect(pointRow(SCOPE_POINT[scope])).toBeInTheDocument()
  })
  openPoint(SCOPE_POINT[scope])
  await waitFor(() => {
    expect(screen.getByText(/logged as/i)).toBeInTheDocument()
  })
  return screen.queryByRole('switch', { name: SCOPE_SWITCH[scope] })
}

/** The tool-argument consent switch, on `tool.risk`'s own panel. */
const toolArgsSwitch = () => screen.getByRole('switch', { name: SCOPE_SWITCH.tool_args })

/** The whole-transcript consent switch, on `compaction.keep`'s own panel. */
const compactionSwitch = () => screen.getByRole('switch', { name: SCOPE_SWITCH.compaction })

/** The recalled-memory consent switch, on `memory.recall`'s own panel. */
const memoryTextSwitch = () => screen.getByRole('switch', { name: SCOPE_SWITCH.memory_text })

/**
 * Stub all three reads: the governance answer that decides whether the card is
 * drawn, the keystone (or a rejection), and the config's bucket.
 *
 * `decisions_enabled: true` is the ungoverned default every case below assumes —
 * a case that wants the withdrawn state passes `governed: false` and gets no card.
 */
function stubGateway(
  consent: DecisionsConsentData | (Partial<DecisionsConsentData> & { enabled: boolean }) | Error,
  config: unknown = { decisions: { bucket: 100 } },
  dashboard: unknown = { decisions_enabled: true },
) {
  vi.spyOn(api, 'dashboardConfig').mockResolvedValue(dashboard as never)
  vi.spyOn(api, 'kirocrewConfig').mockResolvedValue(config as never)
  // Names only, which is all the vault ever hands back: the card shows set or unset
  // and never holds the credential. Stubbed in every case so no test reaches the
  // network for a field most of them do not open.
  vi.spyOn(api, 'secretsList').mockResolvedValue({ names: [] } as never)
  if (consent instanceof Error) {
    vi.spyOn(api, 'getDecisionsConsent').mockRejectedValue(consent)
  } else {
    // The short form forwards its OTHER fields too, not just the switch: a case
    // that stubs a recorded ceiling or a projected row list must not have it
    // silently replaced by the default the switch alone implies.
    const full =
      'permits' in consent
        ? (consent as DecisionsConsentData)
        : consentOf(consent.enabled, consent)
    vi.spyOn(api, 'getDecisionsConsent').mockResolvedValue(full)
  }
}

describe('Decisions (Jev) preview card', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('offers no write while the keystone has not been read', async () => {
    // A never-resolving read: the switch has no basis for the state it would
    // show, so it must not be clickable in the meantime. Awaited rather than
    // asserted on the first frame, because the card is not drawn until the
    // governance read says it may be.
    vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ decisions_enabled: true } as never)
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({} as never)
    vi.spyOn(api, 'getDecisionsConsent').mockReturnValue(new Promise(() => {}) as never)
    renderSection()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).toBe('true')
    })
  })

  it('disables itself and names the gateway when the consent route is missing', async () => {
    // The frontend ships before the backend whenever a user updates one half
    // first: an older gateway answers the consent GET with 404.
    stubGateway(notFound(), { telemetry: {} })
    const save = vi.spyOn(api, 'saveDecisionsConsent').mockResolvedValue(consentOf(true))
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/older than this feature/i)).toBeInTheDocument()
    })
    expect(decisionsSwitch().getAttribute('aria-disabled')).toBe('true')
    expect(decisionsSwitch().getAttribute('aria-checked')).toBe('false')
    decisionsSwitch().click()
    expect(save).not.toHaveBeenCalled()
  })

  it('never reads consent out of config.json, whatever it carries', async () => {
    // A shadow-era gateway has a `decisions` section with `preview: true`, and a
    // hand-edited one might carry `enabled: true`. Neither is consent: the file is
    // agent-writable, which is the whole reason the switch is a keystone. With no
    // consent route, both render as an old gateway.
    stubGateway(notFound(), {
      decisions: { preview: true, enabled: true, points: { 'skills.select': { arm: 'live' } } },
    })
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/older than this feature/i)).toBeInTheDocument()
    })
    expect(decisionsSwitch().getAttribute('aria-checked')).toBe('false')
  })

  it('says the read failed rather than blaming the gateway version', async () => {
    // Two different facts, two different fixes: an old gateway needs an update,
    // a failed read needs a retry. Neither may be reported as the other.
    stubGateway(new Error('offline'))
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/could not read the settings/i)).toBeInTheDocument()
    })
    expect(screen.queryByText(/older than this feature/i)).toBeNull()
    expect(decisionsSwitch().getAttribute('aria-disabled')).toBe('true')
  })

  it('reflects the keystone and writes it through the consent route when flipped', async () => {
    stubGateway({ enabled: false })
    const save = vi.spyOn(api, 'saveDecisionsConsent').mockResolvedValue(consentOf(true))
    const patch = vi.spyOn(api, 'patchConfig').mockResolvedValue({} as never)
    renderSection()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    })
    decisionsSwitch().click()
    await waitFor(() => {
      // The reviewed address rides along, so the gateway binds consent to it.
      expect(save).toHaveBeenCalledWith(true, ENDPOINT)
    })
    // Never the config route: a `decisions.enabled` PATCH is refused by the
    // backend and, were it accepted, would be the agent-writable switch this
    // design removes.
    expect(patch).not.toHaveBeenCalled()
  })

  it('shows consent as on, and offers withdrawing it', async () => {
    stubGateway({ enabled: true })
    const save = vi.spyOn(api, 'saveDecisionsConsent').mockResolvedValue(consentOf(false))
    renderSection()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-checked')).toBe('true')
    })
    decisionsSwitch().click()
    await waitFor(() => {
      expect(save).toHaveBeenCalledWith(false, ENDPOINT)
    })
  })

  it('stays closed to input until the write is reflected in a fresh read', async () => {
    // The window this closes: react-query holds a mutation pending only while
    // `onSettled` has an unresolved promise outstanding. Started-but-not-returned,
    // the switch came back to life the instant the PUT resolved and still showed
    // the pre-flip value — so the flip read as having failed, and a second click
    // wrote it again. The second read is held open here to sit inside that window.
    let releaseRefetch: (value: unknown) => void = () => {}
    let reads = 0
    vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ decisions_enabled: true } as never)
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ decisions: { bucket: 100 } } as never)
    vi.spyOn(api, 'getDecisionsConsent').mockImplementation((() => {
      reads += 1
      if (reads === 1) return Promise.resolve(consentOf(false))
      return new Promise(resolve => { releaseRefetch = resolve })
    }) as never)
    vi.spyOn(api, 'saveDecisionsConsent').mockResolvedValue(consentOf(true))
    // The vault index too: every read the card draws from is in its error gate now, so
    // an unstubbed one freezes the switch this test is about -- which is the gate
    // working, not a defect in it.
    vi.spyOn(api, 'secretsList').mockResolvedValue({ names: [] } as never)

    renderSection()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    })
    decisionsSwitch().click()
    await waitFor(() => {
      expect(api.saveDecisionsConsent).toHaveBeenCalledWith(true, ENDPOINT)
    })
    // The PUT has resolved and the refetch has not. The switch still shows the
    // stored value, so it must not accept another click against it.
    expect(decisionsSwitch().getAttribute('aria-checked')).toBe('false')
    expect(decisionsSwitch().getAttribute('aria-disabled')).toBe('true')

    releaseRefetch(consentOf(true))
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-checked')).toBe('true')
    })
    expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
  })

  it('reports a refused write instead of leaving the switch looking flipped', async () => {
    stubGateway({ enabled: false })
    vi.spyOn(api, 'saveDecisionsConsent').mockRejectedValue(new Error('dashboard owner required'))
    renderSection()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    })
    decisionsSwitch().click()
    await waitFor(() => {
      expect(screen.getByText(/could not save this setting/i)).toBeInTheDocument()
    })
    // The switch shows the keystone's value, not the click's, so a refused write
    // cannot leave the card claiming the preview is on.
    expect(decisionsSwitch().getAttribute('aria-checked')).toBe('false')
  })

  it('states that the message text leaves the machine, whatever else it says', async () => {
    // The egress sentence is the consent this card asks for. It renders in every
    // state — including the disabled ones — because a reader who cannot flip the
    // switch yet is still deciding whether they ever will.
    stubGateway(notFound(), { telemetry: {} })
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/leave this machine.*sent over the internet/i)).toBeInTheDocument()
    })
  })

  it('names Jev as the recipient and the fallback as the shipped rule', async () => {
    // The two things the shadow-only copy did not have to say, now that the
    // answer is acted on: where the data goes, and what happens when the answer
    // does not arrive.
    stubGateway({ enabled: true })
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/sent over the internet to Jev/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/falls back to the same rule/i)).toBeInTheDocument()
  })

  it('lists one row per point the GATEWAY projects, never a list of its own', async () => {
    stubGateway({ enabled: true }, { decisions: { bucket: 100 } })
    renderSection()
    await waitFor(() => {
      expect(pointRow('Automatic skill choice')).toBeInTheDocument()
    })
    // Every row the payload carried, in the registry's order. The card holds a label
    // per id and no array of ids, so this set is the gateway's answer.
    expect(screen.getAllByRole('tab').map(el => el.textContent)).toEqual([
      expect.stringContaining('Automatic skill choice'),
      expect.stringContaining('Risky tool-call notes'),
      expect.stringContaining('Mid-turn message handling'),
      expect.stringContaining("Model for the turn's difficulty"),
      expect.stringContaining('Which tool calls a compaction would keep'),
      expect.stringContaining('Which recalled memories reach the prompt'),
      expect.stringContaining('Quiet check-ins: wake or skip'),
    ])
    expect(screen.getByText(/what Jev decides while this is on/i)).toBeInTheDocument()
  })

  it('draws a row for a point this build has no label for, under its own identifier', async () => {
    // The property the server-side projection exists for: a build that ships another
    // point must reach a reader with no edit on this side.
    stubGateway(
      consentOf(true, {
        points: [{ id: 'invented.point', needs_scope: null, status: 'active' }],
      }),
    )
    renderSection()
    await waitFor(() => {
      expect(pointRow('invented.point')).toBeInTheDocument()
    })
  })

  it('carries the status as the EFFECTIVE answer, with its own word for a missing scope', async () => {
    stubGateway({ enabled: true })
    renderSection()
    await waitFor(() => {
      expect(pointRow('Automatic skill choice')).toBeInTheDocument()
    })
    // Running: the row is marked current, which is a different fact from being the
    // row you are looking at.
    expect(pointRow('Automatic skill choice').getAttribute('aria-current')).toBe('true')
    // Consent stands but this point's egress category was never granted. NOT "off":
    // the fix is a switch on its own panel, and "off" would send the reader back to
    // the main switch they already turned on.
    expect(pointRow('Risky tool-call notes').textContent).toContain('Needs your OK')
    expect(pointRow('Risky tool-call notes').getAttribute('aria-current')).toBeNull()
  })

  it('says every point but the judge is off while consent is off, and lists them all', async () => {
    stubGateway({ enabled: false })
    renderSection()
    await waitFor(() => {
      expect(pointRow('Automatic skill choice')).toBeInTheDocument()
    })
    // A point a reader cannot use still gets a row: a row is free and it is the only
    // way to read about what consenting would turn on.
    expect(screen.getAllByRole('tab')).toHaveLength(pointsOf(false).length)
    for (const row of screen.getAllByRole('tab')) {
      // The judge is the ONE exception, and it is the reason this lane exists: with no
      // consent recorded the default provider resolves to the small model, which sends
      // nothing to the endpoint this switch governs, so that point does run here. A
      // row claiming otherwise would tell a keyless owner their judge is off while it
      // is answering.
      if (row.textContent?.includes('Quiet check-ins')) {
        expect(row.getAttribute('aria-current')).toBe('true')
        expect(row.textContent).toContain('Judged by the small model')
        continue
      }
      expect(row.getAttribute('aria-current')).toBeNull()
      expect(row.textContent).toContain('Off')
    }
  })

  it('opens the highlighted point\'s own panel, and highlighting is never selection', async () => {
    const save = vi.spyOn(api, 'saveDecisionsConsent')
    const scope = vi.spyOn(api, 'saveDecisionsScope')
    stubGateway({ enabled: true })
    renderSection()
    await waitFor(() => {
      expect(pointRow('Automatic skill choice')).toBeInTheDocument()
    })
    // The first row's panel, unasked: one panel is always rendered.
    expect(screen.getByRole('tabpanel').textContent).toContain('which one of your skills')
    expect(screen.getByRole('tabpanel').textContent).toContain('skills.select')
    openPoint('Mid-turn message handling')
    await waitFor(() => {
      expect(screen.getByRole('tabpanel').textContent).toContain('steers that turn')
    })
    // Exactly one panel, so the wall of prose cannot come back.
    expect(screen.getAllByRole('tabpanel')).toHaveLength(1)
    // And opening a row wrote nothing: a row is not a control.
    expect(save).not.toHaveBeenCalled()
    expect(scope).not.toHaveBeenCalled()
  })

  it('moves the highlight with the arrow keys, Home and End, and selects with none of them', async () => {
    // The list is ONE tab stop, so the arrows are the only way through it for a reader
    // who is not using a pointer. Enter and Space are named and do nothing beyond
    // highlighting, which is what makes "opening a row writes nothing" a property of
    // the keyboard path too, not only the click path.
    const save = vi.spyOn(api, 'saveDecisionsConsent')
    const scope = vi.spyOn(api, 'saveDecisionsScope')
    stubGateway({ enabled: true })
    renderSection()
    const first = await waitFor(() => pointRow('Automatic skill choice'))
    const panel = () => screen.getByRole('tabpanel').textContent ?? ''
    // The panel rides a chunk boundary, so its first paint is awaited like any other.
    await waitFor(() => expect(panel()).toContain('skills.select'))

    fireEvent.keyDown(first, { key: 'ArrowDown' })
    await waitFor(() => expect(panel()).toContain('tool.risk'))
    fireEvent.keyDown(pointRow('Risky tool-call notes'), { key: 'ArrowUp' })
    await waitFor(() => expect(panel()).toContain('skills.select'))

    // End and Home are the ends of the PROJECTED list, whatever the gateway sent.
    fireEvent.keyDown(pointRow('Automatic skill choice'), { key: 'End' })
    await waitFor(() => expect(panel()).toContain('nudge.wake'))
    fireEvent.keyDown(pointRow('Quiet check-ins: wake or skip'), {
      key: 'Home',
    })
    await waitFor(() => expect(panel()).toContain('skills.select'))

    // ArrowRight/ArrowLeft are the same move: a horizontal list is what some platforms
    // read this shape as, and a reader should not have to know which.
    fireEvent.keyDown(pointRow('Automatic skill choice'), { key: 'ArrowRight' })
    await waitFor(() => expect(panel()).toContain('tool.risk'))
    fireEvent.keyDown(pointRow('Risky tool-call notes'), { key: 'ArrowLeft' })
    await waitFor(() => expect(panel()).toContain('skills.select'))

    // Named on purpose and inert on purpose, and an unhandled key is not swallowed.
    fireEvent.keyDown(pointRow('Automatic skill choice'), { key: 'Enter' })
    fireEvent.keyDown(pointRow('Automatic skill choice'), { key: ' ' })
    fireEvent.keyDown(pointRow('Automatic skill choice'), { key: 'a' })
    expect(panel()).toContain('skills.select')
    expect(screen.getAllByRole('tabpanel')).toHaveLength(1)
    // Not one keystroke was a write: moving through the list is reading, not consenting.
    expect(save).not.toHaveBeenCalled()
    expect(scope).not.toHaveBeenCalled()
  })

  it('sends a typed credential to the vault under its own name, once', async () => {
    // The vault hands no value back, so what this has to prove is that the draft LEAVES:
    // the name it is stored under and the value verbatim. Save is held until there is
    // something to send, so a stray click cannot write an empty credential over a key.
    stubGateway(consentOf(true), { decisions: { bucket: 100 } })
    const save = vi.spyOn(api, 'secretsSave').mockResolvedValue({} as never)
    renderSection()
    await waitFor(() => {
      expect(screen.getByText('Shared settings', { exact: true })).toBeInTheDocument()
    })
    fireEvent.click(screen.getByText('Shared settings', { exact: true }))
    const field = await waitFor(() => screen.getByLabelText('Jev API key'))
    expect(screen.getByRole('button', { name: 'Save key' })).toBeDisabled()
    fireEvent.change(field, { target: { value: 'sk-live-not-a-real-key' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save key' }))
    await waitFor(() => {
      expect(save).toHaveBeenCalledWith('TYPESAFE_API_KEY', 'sk-live-not-a-real-key')
    })
    expect(save).toHaveBeenCalledTimes(1)
  })

  it('points at a setting it deliberately does not offer, by its config path', async () => {
    // How many skills a turn may inject is not a Jev setting at all — it decides
    // whether `skills.select` is ever reached — so the panel names where it lives
    // instead of pretending the card is the whole story.
    stubGateway({ enabled: true })
    renderSection()
    await waitFor(() => {
      expect(screen.getByRole('tabpanel').textContent).toContain('skills.max_triggered')
    })
    expect(screen.getByRole('tabpanel').textContent).toContain('~/.kiro/crew/config.json')
  })

  it('offers model.route its three tier pickers, defaulting to inherit', async () => {
    stubGateway({ enabled: true }, { decisions: { bucket: 100, model_route: { simple: '', medium: '', complex: '' } } })
    renderSection()
    await waitFor(() => {
      expect(pointRow("Model for the turn's difficulty")).toBeInTheDocument()
    })
    openPoint("Model for the turn's difficulty")
    await waitFor(() => {
      expect(screen.getByText('Model for a simple turn')).toBeInTheDocument()
    })
    expect(screen.getByText('Model for a medium turn')).toBeInTheDocument()
    expect(screen.getByText('Model for a hard turn')).toBeInTheDocument()
    // INHERIT is what an unpinned tier reads as, and no model id is named for the
    // reader: an id their account is not offered would fail on the first prompt.
    expect(screen.getByRole('tabpanel').textContent).toContain("Keep the session's own model")
  })

  it('names the lane on the judge row rather than the generic active word', async () => {
    // Under the list's "while this is on" heading, a row reading the generic active
    // word with the switch OFF is a contradiction, and it implies the endpoint is
    // being used right now. The small-model lane sends nothing there, so the chip has
    // to say which judge answers.
    stubGateway(
      { enabled: false, points: pointsOf(false, false, false, false, 'llm') },
      { decisions: { bucket: 100, nudge_wake: { provider: 'llm', llm_model: '' } } },
    )
    renderSection()
    await waitFor(() => {
      expect(pointRow('Quiet check-ins: wake or skip')).toBeInTheDocument()
    })
    expect(screen.getByText('Judged by the small model')).toBeInTheDocument()
  })

  it('keeps the generic active word when Jev is the lane that would answer', async () => {
    // The contradiction exists only for the small-model lane. With the switch on and
    // the address in force, Jev is what decides and the heading is accurate, so the row
    // must not claim a small-model judge.
    stubGateway(
      { enabled: true, permits: true, points: pointsOf(true, true, true, true, 'jev') },
      { decisions: { bucket: 100, nudge_wake: { provider: 'jev', llm_model: '' } } },
    )
    renderSection()
    await waitFor(() => {
      expect(pointRow('Quiet check-ins: wake or skip')).toBeInTheDocument()
    })
    expect(screen.queryByText('Judged by the small model')).not.toBeInTheDocument()
  })

  it('offers nudge.wake its provider and model pickers, reachable with consent off', async () => {
    // Consent OFF and the small model chosen: the state the lane exists for. Every
    // other point's controls are withheld here, so this also pins that the judge's two
    // are deliberately not gated on the switch.
    stubGateway(
      { enabled: false, points: pointsOf(false, false, false, false, 'llm') },
      { decisions: { bucket: 100, nudge_wake: { provider: 'llm', llm_model: '' } } },
    )
    renderSection()
    await waitFor(() => {
      expect(pointRow('Quiet check-ins: wake or skip')).toBeInTheDocument()
    })
    openPoint('Quiet check-ins: wake or skip')
    await waitFor(() => {
      expect(screen.getByText('Which judge answers')).toBeInTheDocument()
    })
    expect(screen.getByText('Model for the small-model judge')).toBeInTheDocument()
    const panelText = screen.getByRole('tabpanel').textContent ?? ''
    // The chosen provider reads as the small model, and the model reads as INHERIT --
    // no model id is named for the reader, for the same reason a tier names none.
    expect(panelText).toContain('The small model \u2014 stays with your current provider')
    // The judge's INHERIT names the JUDGE agent's model, not the session's: an empty
    // `llm_model` resolves the judge agent's own, a different model from the one an
    // unpinned tier keeps.
    expect(panelText).toContain("Keep the judge agent's own model")
    // The card's own frame speaks for the Jev endpoint: its heading says "while this is
    // on" and its intro says nothing is sent while it is off. Both are beside the point
    // on this lane, so the panel states which lane answers and where the evidence goes.
    expect(panelText).toContain('switch above does not govern it')
  })

  it('leaves the lane note out when Jev is the lane that would answer', async () => {
    // The note answers a question only the small-model lane raises. On the Jev lane the
    // card's frame IS the whole story, and a second line there would contradict nothing
    // and explain nothing.
    stubGateway(
      { enabled: true, points: pointsOf(true, false, false, false, 'jev') },
      { decisions: { bucket: 100, nudge_wake: { provider: 'jev', llm_model: '' } } },
    )
    renderSection()
    await waitFor(() => {
      expect(pointRow('Quiet check-ins: wake or skip')).toBeInTheDocument()
    })
    openPoint('Quiet check-ins: wake or skip')
    await waitFor(() => {
      expect(screen.getByText('Which judge answers')).toBeInTheDocument()
    })
    expect(screen.getByRole('tabpanel').textContent ?? '').not.toContain(
      'switch above does not govern it',
    )
  })

  it('fades the egress note only when the gateway cannot run this at all', async () => {
    stubGateway(notFound(), { decisions: { preview: true } })
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/older than this feature/i)).toBeInTheDocument()
    })
    // Nothing can leave the machine on this gateway, so the warning fades with
    // the rest of the card instead of shouting beside a switch that cannot move.
    // Same opacity as the disabled row: a muted colour alone still reads darker
    // than a row at 40%, which is the fade "just missing".
    expect(screen.getByText(/leave this machine/i).className).toContain('opacity-40')
    // And the notice says WHERE to act, not just that something must be updated.
    expect(screen.getByText(/Settings › Releases/)).toBeInTheDocument()
  })

  it('offers the sampling share as a slider, and says what the current one means', async () => {
    stubGateway({ enabled: true }, { decisions: { bucket: 25 } })
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/25% of your sessions/i)).toBeInTheDocument()
    })
    // A control rather than a sentence telling the reader to hand-edit config.json,
    // which is what this card used to say. The share can only ever NARROW what
    // consent already allows, which is why it stays a config value.
    const slider = document.querySelector('input[type="range"]') as HTMLInputElement
    expect(slider).not.toBeNull()
    expect(slider.value).toBe('25')
    expect(slider.min).toBe('0')
    expect(slider.max).toBe('100')
    // Zero is a state worth printing too: on, and deciding for nobody.
    cleanup()
    vi.restoreAllMocks()
    stubGateway({ enabled: true }, { decisions: { bucket: 0 } })
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/0% of your sessions/i)).toBeInTheDocument()
    })
  })

  it('writes a moved slider to the config path, never to the keystone', async () => {
    const patch = vi.spyOn(api, 'patchConfig').mockResolvedValue({} as never)
    const save = vi.spyOn(api, 'saveDecisionsConsent')
    stubGateway({ enabled: true }, { decisions: { bucket: 100 } })
    renderSection()
    await waitFor(() => {
      expect(document.querySelector('input[type="range"]')).not.toBeNull()
    })
    const slider = document.querySelector('input[type="range"]') as HTMLInputElement
    // Through `fireEvent.change`, not a raw dispatch: React tracks the previous value
    // on the node itself, so setting `.value` by hand and dispatching leaves the
    // tracker in step and the handler never runs.
    fireEvent.change(slider, { target: { value: '40' } })
    fireEvent.pointerUp(slider)
    await waitFor(() => {
      expect(patch).toHaveBeenCalledWith('decisions.bucket', 40)
    })
    // The keystone is untouched: narrowing a share is not a consent decision.
    expect(save).not.toHaveBeenCalled()
  })

  it('writes the share ONCE, when the drag ends, not on every tick', async () => {
    // A write per tick is a config write per pixel, and the thumb reads the SAVED
    // share while they are in flight — so it snaps back to where the drag started
    // until each refetch lands. The draft holds the position; release commits it.
    const patch = vi.spyOn(api, 'patchConfig').mockResolvedValue({} as never)
    stubGateway({ enabled: true }, { decisions: { bucket: 100 } })
    renderSection()
    await waitFor(() => {
      expect(document.querySelector('input[type="range"]')).not.toBeNull()
    })
    const slider = document.querySelector('input[type="range"]') as HTMLInputElement
    for (const value of ['90', '80', '70', '60']) {
      fireEvent.change(slider, { target: { value } })
    }
    // Mid-drag: the thumb follows the reader and nothing has been saved.
    expect(patch).not.toHaveBeenCalled()
    expect(slider.value).toBe('60')
    fireEvent.pointerUp(slider)
    await waitFor(() => {
      expect(patch).toHaveBeenCalledWith('decisions.bucket', 60)
    })
    expect(patch).toHaveBeenCalledTimes(1)
  })

  it('writes nothing when a drag ends where it started', async () => {
    const patch = vi.spyOn(api, 'patchConfig').mockResolvedValue({} as never)
    stubGateway({ enabled: true }, { decisions: { bucket: 100 } })
    renderSection()
    await waitFor(() => {
      expect(document.querySelector('input[type="range"]')).not.toBeNull()
    })
    const slider = document.querySelector('input[type="range"]') as HTMLInputElement
    fireEvent.change(slider, { target: { value: '40' } })
    fireEvent.change(slider, { target: { value: '100' } })
    fireEvent.pointerUp(slider)
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(patch).not.toHaveBeenCalled()
  })

  it('does not accept a ceiling while consent is off, because the write throws it away', async () => {
    // The keystone writer stores this ceiling as 0 whenever the switch is off, so the
    // PUT would answer 200 and the typed number would be gone on the next read -- a
    // control that takes a value and discards it. Nothing is being sent yet, so there
    // is no conversation for a ceiling to bound.
    const budget = vi.spyOn(api, 'saveDecisionsHistoryBudget')
    stubGateway(consentOf(false), { decisions: { bucket: 100 } })
    renderSection()
    await waitFor(() => {
      expect(screen.getByText('Shared settings', { exact: true })).toBeInTheDocument()
    })
    fireEvent.click(screen.getByText('Shared settings', { exact: true }))
    const box = await waitFor(() =>
      screen.getByLabelText(/Earlier conversation one decision may carry/i)
    )
    expect(box).toBeDisabled()
    // Disabled AND explained: a held control with no reason beside it is a dead one.
    expect(screen.getByText(/takes effect once the main Jev switch is on/i)).toBeInTheDocument()
    fireEvent.change(box, { target: { value: '6000' } })
    fireEvent.blur(box)
    expect(budget).not.toHaveBeenCalled()
  })

  describe('the prior-conversation ceiling', () => {
    const budgetBox = () =>
      screen.getByLabelText(/Earlier conversation one decision may carry/i) as HTMLInputElement

    const openShared = async () => {
      renderSection()
      await waitFor(() => {
        expect(screen.getByText('Shared settings', { exact: true })).toBeInTheDocument()
      })
      fireEvent.click(screen.getByText('Shared settings', { exact: true }))
      await waitFor(() => {
        expect(budgetBox()).toBeInTheDocument()
      })
    }

    it('writes the KEYSTONE ceiling, never the config path', async () => {
      // The config value is what the seam asks for and an agent may raise it; the
      // ceiling that clamps it is on the keystone, behind the owner-only route.
      const ceiling = vi
        .spyOn(api, 'saveDecisionsHistoryBudget')
        .mockResolvedValue({} as never)
      const patch = vi.spyOn(api, 'patchConfig')
      stubGateway({ enabled: true, history_budget_chars: 0 })
      await openShared()
      fireEvent.change(budgetBox(), { target: { value: '4000' } })
      fireEvent.blur(budgetBox())
      await waitFor(() => {
        expect(ceiling).toHaveBeenCalledWith(4000)
      })
      expect(patch).not.toHaveBeenCalled()
    })

    it('refuses a value it cannot read WHOLE, rather than saving a prefix of it', async () => {
      // `parseInt` reads a prefix, so "6,000" would save 6 — a number nobody typed,
      // on the value that decides how much conversation leaves the machine. The
      // saved ceiling stays, which is the smaller number and the consented one.
      const ceiling = vi
        .spyOn(api, 'saveDecisionsHistoryBudget')
        .mockResolvedValue({} as never)
      stubGateway({ enabled: true, history_budget_chars: 0 })
      await openShared()
      for (const typed of ['6,000', '6 000', '4e3', '12.5', '-1', 'lots', ' ']) {
        fireEvent.change(budgetBox(), { target: { value: typed } })
        fireEvent.blur(budgetBox())
      }
      await new Promise(resolve => setTimeout(resolve, 20))
      expect(ceiling).not.toHaveBeenCalled()
    })

    it('takes an explicit 0, which is how prior turns are handed back', async () => {
      const ceiling = vi
        .spyOn(api, 'saveDecisionsHistoryBudget')
        .mockResolvedValue({} as never)
      stubGateway({ enabled: true, history_budget_chars: 4000 })
      await openShared()
      expect(budgetBox().value).toBe('4000')
      fireEvent.change(budgetBox(), { target: { value: '0' } })
      fireEvent.blur(budgetBox())
      await waitFor(() => {
        expect(ceiling).toHaveBeenCalledWith(0)
      })
    })
  })

  describe('the credential never reads a failed read as "no key"', () => {
    const openShared = async () => {
      renderSection()
      await waitFor(() => {
        expect(screen.getByText('Shared settings', { exact: true })).toBeInTheDocument()
      })
      fireEvent.click(screen.getByText('Shared settings', { exact: true }))
    }

    it('says the read failed instead of rendering a set key as unset', async () => {
      // The defect this closes: the vault index is one fetch, `apiKeySet` is derived
      // from it, and a transient failure resolved to false. The card then said "not
      // set" about a key that exists and offered Save under it -- an invitation to
      // overwrite a credential the reader had just been told was absent.
      vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ decisions_enabled: true } as never)
      vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ decisions: { bucket: 100 } } as never)
      vi.spyOn(api, 'getDecisionsConsent').mockResolvedValue(consentOf(true))
      vi.spyOn(api, 'secretsList').mockRejectedValue(new Error('gateway busy'))
      const save = vi.spyOn(api, 'secretsSave')

      await openShared()
      await waitFor(() => {
        expect(screen.getByText(/could not read the settings/i)).toBeInTheDocument()
      })
      // And no credential write is on offer while the index is unknown.
      expect(screen.queryByRole('button', { name: 'Save key' })).toBeNull()
      expect(save).not.toHaveBeenCalled()
    })

    it('offers Save only once the index is actually known', async () => {
      // The other half: a SUCCESSFUL read saying the vault is empty is a real answer,
      // and it is the first-run state the setup control exists for.
      stubGateway(consentOf(true), { decisions: { bucket: 100 } })
      await openShared()
      await waitFor(() => {
        expect(screen.getByRole('button', { name: 'Save key' })).toBeInTheDocument()
      })
    })
  })

  it('still lets consent be WITHDRAWN when an auxiliary read fails', async () => {
    // The hole this closes: the vault index and the advertised model list were folded
    // into one `readFailed`, which froze every control including the main switch. So a
    // secrets fetch that failed while consent was ON left the seam sending with no way
    // for its owner to turn it off -- the vault being unreachable is not a reason to
    // keep conversation leaving the machine. Neither read is needed to write `false`.
    vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ decisions_enabled: true } as never)
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ decisions: { bucket: 100 } } as never)
    vi.spyOn(api, 'getDecisionsConsent').mockResolvedValue(consentOf(true))
    vi.spyOn(api, 'secretsList').mockRejectedValue(new Error('vault unreachable'))
    vi.spyOn(api, 'models').mockRejectedValue(new Error('no model list'))
    const save = vi.spyOn(api, 'saveDecisionsConsent').mockResolvedValue(consentOf(false) as never)

    renderSection()
    await waitFor(() => {
      expect(decisionsSwitch()).toHaveAttribute('aria-checked', 'true')
    })
    // The failure is still REPORTED -- this is not a fix that hides it.
    expect(screen.getByText(/could not read the settings/i)).toBeInTheDocument()
    // And the one control that stops egress is still live.
    expect(decisionsSwitch()).not.toBeDisabled()
    fireEvent.click(decisionsSwitch())
    await waitFor(() => {
      expect(save).toHaveBeenCalled()
    })
    expect(save.mock.calls[0][0]).toBe(false)
  })

  it('says Replace key on the button itself, not only in a tooltip', async () => {
    // An icon alone names neither the verb nor what it acts on, and beside a stored
    // credential "replace the stored key" and "discard what I just typed" differ by
    // whether work is lost. The wording is the button's TEXT, and then it is the
    // button's only accessible name: a second one over a visible one is a duplicate.
    vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ decisions_enabled: true } as never)
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ decisions: { bucket: 100 } } as never)
    vi.spyOn(api, 'getDecisionsConsent').mockResolvedValue(consentOf(true))
    vi.spyOn(api, 'secretsList').mockResolvedValue({ names: ['TYPESAFE_API_KEY'] } as never)
    renderSection()
    await waitFor(() => {
      expect(screen.getByText('Shared settings', { exact: true })).toBeInTheDocument()
    })
    fireEvent.click(screen.getByText('Shared settings', { exact: true }))
    const replace = await waitFor(() => screen.getByRole('button', { name: 'Replace key' }))
    expect(replace).toHaveTextContent('Replace key')
    expect(replace).not.toHaveAttribute('aria-label')
    expect(replace).not.toHaveAttribute('title')
    // And the shared wording is gone from this card: "Replace" alone is what the
    // reader could not act on.
    expect(screen.queryByRole('button', { name: 'Replace' })).toBeNull()
  })

  describe('removing the credential asks before it writes', () => {
    const stubSetKey = (held?: Promise<unknown>) => {
      vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ decisions_enabled: true } as never)
      vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ decisions: { bucket: 100 } } as never)
      vi.spyOn(api, 'getDecisionsConsent').mockResolvedValue(consentOf(true))
      vi.spyOn(api, 'secretsList').mockResolvedValue({ names: ['TYPESAFE_API_KEY'] } as never)
      const del = vi.spyOn(api, 'secretsDelete')
      if (held) del.mockReturnValue(held as never)
      else del.mockResolvedValue({} as never)
      return del
    }

    const openShared = async () => {
      renderSection()
      await waitFor(() => {
        expect(screen.getByText('Shared settings', { exact: true })).toBeInTheDocument()
      })
      fireEvent.click(screen.getByText('Shared settings', { exact: true }))
      return await waitFor(() => screen.getByRole('button', { name: 'Remove key' }))
    }

    it('writes nothing until the removal is confirmed', async () => {
      // The defect this closes: the trash used to fire the DELETE immediately and then
      // render the field's DEFERRED-removal strip, which carries an Undo — an Undo for
      // something already done, in exactly the window a reader who reconsiders reaches
      // for it. The vault never hands a value back, so nothing could be recovered.
      //
      // Every pre-arm assertion comes FIRST and the armed state LAST, because arming
      // replaces the controls the earlier ones look for.
      const del = stubSetKey()
      const remove = await openShared()
      expect(screen.queryByText(/cannot be undone/i)).toBeNull()
      expect(screen.queryByRole('button', { name: 'Delete key' })).toBeNull()

      fireEvent.click(remove)

      await waitFor(() => {
        expect(screen.getByText(/cannot be undone/i)).toBeInTheDocument()
      })
      expect(del).not.toHaveBeenCalled()
      // No Undo affordance for a write that has not happened — and the confirm carries
      // its own word, so it can never be mistaken for the trash that opened it.
      expect(screen.queryByRole('button', { name: /undo/i })).toBeNull()
      expect(screen.getByRole('button', { name: 'Delete key' })).toBeInTheDocument()
    })

    it('keeps the key when the reader backs out', async () => {
      const del = stubSetKey()
      const remove = await openShared()
      fireEvent.click(remove)
      const keep = await waitFor(() => screen.getByRole('button', { name: 'Keep it' }))
      fireEvent.click(keep)
      await waitFor(() => {
        expect(screen.queryByText(/cannot be undone/i)).toBeNull()
      })
      expect(del).not.toHaveBeenCalled()
    })

    it('deletes only from the confirm control', async () => {
      const del = stubSetKey()
      const remove = await openShared()
      fireEvent.click(remove)
      const confirm = await waitFor(() => screen.getByRole('button', { name: 'Delete key' }))
      fireEvent.click(confirm)
      await waitFor(() => {
        expect(del).toHaveBeenCalledTimes(1)
      })
    })

    it('offers no Save while the question is open or the DELETE is in flight', async () => {
      // A DELETE and a POST outstanding together let whichever the server finishes last
      // decide, so the save path stays shut for the whole removal.
      let release: () => void = () => {}
      const held = new Promise<unknown>(resolve => {
        release = () => resolve({})
      })
      stubSetKey(held)
      const remove = await openShared()
      fireEvent.click(remove)
      await waitFor(() => {
        expect(screen.getByText(/cannot be undone/i)).toBeInTheDocument()
      })
      expect(screen.queryByRole('button', { name: 'Save key' })).toBeNull()
      fireEvent.click(screen.getByRole('button', { name: 'Delete key' }))
      await waitFor(() => {
        expect(screen.queryByRole('button', { name: 'Save key' })).toBeNull()
      })
      release()
    })
  })

  it('offers the credential as a vault entry, set or unset and never a value', async () => {
    stubGateway({ enabled: true })
    renderSection()
    await waitFor(() => {
      expect(screen.getByText('Jev API key')).toBeInTheDocument()
    })
    // The name the field writes is the ONE entry `provider.api_key` honours, which is
    // what keeps an agent-writable config safe to leave naming it.
    const body = document.body.textContent ?? ''
    expect(body).toContain('never in config.json')
  })

  it('carries no point row at all against a gateway without the consent route', async () => {
    // An older gateway has no point wired, so a row reading "off" would describe
    // a check that does not exist rather than one that is switched off.
    stubGateway(notFound(), { telemetry: {} })
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/older than this feature/i)).toBeInTheDocument()
    })
    expect(screen.queryAllByRole('tab')).toEqual([])
    expect(screen.queryByText(/what Jev decides while this is on/i)).toBeNull()
  })

  it('names the address consent is given for', async () => {
    // Consent is to an ADDRESS, not to "sending": the reader must see where.
    stubGateway(consentOf(false, { configured_endpoint: 'https://proxy.example/v1/systemone' }))
    renderSection()
    await waitFor(() => {
      // `exact: true`: a bare string matches a SUBSTRING, so an element reading
      // `…/systemone.evil.test/x` would satisfy an assertion meant to prove the card
      // shows the address config names. Same comparison defect as `includes` on a URL.
      expect(
        screen.getByText('https://proxy.example/v1/systemone', { exact: true }),
      ).toBeInTheDocument()
    })
    expect(screen.getByText(/^Sent to$/)).toBeInTheDocument()
  })

  it('says nothing is sent when the config moved the address out from under consent', async () => {
    // The redirected-config state: the switch reads on, the gate refuses, and
    // the card must not let those two disagree silently.
    stubGateway(consentOf(true, {
      endpoint: ENDPOINT,
      configured_endpoint: 'https://attacker.example/v1/systemone',
      permits: false,
    }))
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/nothing is being sent/i)).toBeInTheDocument()
    })
    expect(decisionsSwitch().getAttribute('aria-checked')).toBe('true')
    expect(screen.getByText(/Turn this off and on again/i)).toBeInTheDocument()
    // Styled as a warning, not a paragraph: the switch is on and the truth is
    // "nothing is sent", so the state must read as wrong before it is read.
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent(/nothing is being sent/i)
    expect(alert.className).toContain('text-warn')
  })

  it('shows no moved-address notice while consent is off or matches', async () => {
    stubGateway(consentOf(true))
    renderSection()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-checked')).toBe('true')
    })
    expect(screen.queryByText(/nothing is being sent/i)).toBeNull()
  })

  it('leaves the four localStorage previews alone', async () => {
    // The section mixes two kinds of switch now. Flipping the backend one must
    // not write a preview flag — a stray one would turn an unrelated unreleased
    // page on for this device.
    stubGateway({ enabled: false })
    vi.spyOn(api, 'saveDecisionsConsent').mockResolvedValue(consentOf(true))
    renderSection()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    })
    decisionsSwitch().click()
    await waitFor(() => {
      expect(api.saveDecisionsConsent).toHaveBeenCalled()
    })
    expect(Object.keys(localStorage).filter(k => k.startsWith(PREVIEW_FLAG_PREFIX))).toEqual([])
  })

    describe('the tool-argument consent switch', () => {
    it('is absent while the main switch is off, because nothing is sent at all then', async () => {
      // A second egress control under an off switch would describe a state that
      // cannot happen, and would invite a grant nothing could act on.
      stubGateway({ enabled: false })
      renderSection()
      await waitFor(() => {
        expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
      })
      openPoint('Risky tool-call notes')
      expect(screen.queryByRole('switch', { name: /tool-call arguments/ })).toBeNull()
    })

    it('lives on its own point\'s panel, not beside the main switch', async () => {
      // Which is the whole point of the redesign: what belongs to one point is one
      // level down, so the top of the card stays scannable.
      stubGateway({ enabled: true })
      renderSection()
      await waitFor(() => {
        expect(pointRow('Automatic skill choice')).toBeInTheDocument()
      })
      expect(screen.queryByRole('switch', { name: /tool-call arguments/ })).toBeNull()
      openPoint('Risky tool-call notes')
      await waitFor(() => {
        expect(toolArgsSwitch()).toBeInTheDocument()
      })
    })

    it('appears unchecked once consent is on, for a keystone that never recorded it', async () => {
      // The state every install consented before this scope existed is in: sending
      // is allowed, tool arguments are not, and the card must draw exactly that
      // rather than inferring the scope from the main switch.
      stubGateway({ enabled: true })
      renderSection()
      await waitFor(() => {
        expect(pointRow('Risky tool-call notes')).toBeInTheDocument()
      })
      openPoint('Risky tool-call notes')
      await waitFor(() => {
        expect(toolArgsSwitch()).toBeInTheDocument()
      })
      expect(toolArgsSwitch().getAttribute('aria-checked')).toBe('false')
    })

    it('reflects a recorded scope', async () => {
      stubGateway(consentOf(true, { tool_args: true }))
      renderSection()
      await waitFor(() => {
        expect(pointRow('Risky tool-call notes')).toBeInTheDocument()
      })
      openPoint('Risky tool-call notes')
      await waitFor(() => {
        expect(toolArgsSwitch().getAttribute('aria-checked')).toBe('true')
      })
    })

    it('grants the scope WITHOUT restating consent to an address', async () => {
      stubGateway({ enabled: true })
      const scope = vi.spyOn(api, 'saveDecisionsScope').mockResolvedValue(
        consentOf(true, { tool_args: true }),
      )
      const save = vi.spyOn(api, 'saveDecisionsConsent')
      renderSection()
      await waitFor(() => {
        expect(pointRow('Risky tool-call notes')).toBeInTheDocument()
      })
      openPoint('Risky tool-call notes')
      await waitFor(() => {
        expect(toolArgsSwitch()).toBeInTheDocument()
      })
      toolArgsSwitch().click()
      await waitFor(() => {
        // The scope alone. `enabled` is omitted and no endpoint is echoed: echoing an
        // endpoint is a REVIEW of an address, and a scope switch is not one. The
        // gateway preserves the recorded switch and refuses the write outright unless
        // consent already stands for the address config names.
        expect(scope).toHaveBeenCalledWith('tool_args', true)
      })
      expect(save).not.toHaveBeenCalled()
    })

    it('revokes it with an explicit false rather than by omission', async () => {
      // Omission PRESERVES the recorded scope on this route, so a revoke has to send
      // the boolean. A card that omitted it would leave the scope granted.
      stubGateway(consentOf(true, { tool_args: true }))
      const scope = vi.spyOn(api, 'saveDecisionsScope').mockResolvedValue(consentOf(true))
      renderSection()
      await waitFor(() => {
        expect(pointRow('Risky tool-call notes')).toBeInTheDocument()
      })
      openPoint('Risky tool-call notes')
      await waitFor(() => {
        expect(toolArgsSwitch().getAttribute('aria-checked')).toBe('true')
      })
      toolArgsSwitch().click()
      await waitFor(() => {
        expect(scope).toHaveBeenCalledWith('tool_args', false)
      })
    })

    it('leaves the scope unmentioned when the MAIN switch is flipped', async () => {
      // The main switch says nothing about tool arguments, and the route preserves a
      // recorded scope for an absent field. So an ordinary flip must send two
      // arguments, not three: a third would make the main switch able to grant or
      // erase an egress scope the owner did not touch.
      stubGateway({ enabled: false })
      const save = vi.spyOn(api, 'saveDecisionsConsent').mockResolvedValue(consentOf(true))
      renderSection()
      await waitFor(() => {
        expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
      })
      decisionsSwitch().click()
      await waitFor(() => {
        expect(save).toHaveBeenCalledWith(true, ENDPOINT)
      })
    })

    it('states what the extra data is, and that it changes no permission', async () => {
      stubGateway({ enabled: true })
      renderSection()
      await waitFor(() => {
        expect(pointRow('Risky tool-call notes')).toBeInTheDocument()
      })
      openPoint('Risky tool-call notes')
      await waitFor(() => {
        expect(toolArgsSwitch()).toBeInTheDocument()
      })
      // Read off the whole rendered section: the description is a sibling of the
      // switch inside SettingsToggle, and walking a fixed number of parents pins a
      // DOM shape this test has no business asserting.
      const body = document.body.textContent ?? ''
      expect(body).toContain('name and arguments of each call')
      expect(body).toContain('Passwords and keys are replaced')
      expect(body).toContain('changes nothing about which tool calls are allowed')
    })
  })

  describe('capabilities.decisions governance gate', () => {
    // The card is the door to a PAID external egress, so a managed fleet can
    // withdraw the whole feature: `GET /api/dashboard/config` reports the ceiling's
    // answer as `decisions_enabled` and the card is drawn only on a literal `true`.
    // Fail closed on absence — an older gateway and a read still in flight both
    // look the same from here, and neither is permission.

    it('draws no card when governance withdrew the seam', async () => {
      stubGateway({ enabled: false }, { decisions: { bucket: 100 } }, { decisions_enabled: false })
      renderSection()
      // A sibling card proves the section rendered, so an absent switch is the
      // gate and not a failed render.
      await waitFor(() => {
        expect(screen.getByRole('switch', { name: /Crew Members/i })).toBeInTheDocument()
      })
      expect(screen.queryByRole('switch', { name: 'Decisions (Jev)' })).toBeNull()
      // Not merely hidden: nothing about the feature is on screen to act on.
      expect(screen.queryByText(/sent over the internet to Jev/i)).toBeNull()
    })

    it('draws no card when the field is absent, and never guesses from the keystone', async () => {
      // An older gateway has no `decisions_enabled` at all. A keystone that
      // already says `true` must not stand in for the fleet's permission.
      stubGateway({ enabled: true }, { decisions: { bucket: 100 } }, {})
      renderSection()
      await waitFor(() => {
        expect(screen.getByRole('switch', { name: /Crew Members/i })).toBeInTheDocument()
      })
      expect(screen.queryByRole('switch', { name: 'Decisions (Jev)' })).toBeNull()
    })

    it('explains a failed ceiling read instead of removing the feature', async () => {
      // The read FAILING is not a withdrawal: nothing has been denied, the dashboard
      // just does not know, and the user can retry. Hiding the card there would turn
      // a transport failure into a feature that silently does not exist
      // (AUTOSDE `errors-use-error-notice`). It has to fade and say so, exactly like
      // the card's other two reads already do.
      stubGateway({ enabled: false })
      vi.spyOn(api, 'dashboardConfig').mockRejectedValue(new Error('offline'))
      renderSection()
      await waitFor(() => {
        expect(screen.getByText(/could not read the settings/i)).toBeInTheDocument()
      })
      // The card is PRESENT — that is the whole point — and offers no write against a
      // ceiling it could not read.
      expect(decisionsSwitch()).toBeInTheDocument()
      expect(decisionsSwitch().getAttribute('aria-disabled')).toBe('true')
    })

    it('keeps hiding the card while the ceiling read is still in flight', async () => {
      // The control for the case above: a read that has not LANDED is not a read that
      // FAILED, and an unanswered ceiling is no basis for offering an egress switch.
      // Without this, "fail closed on absence" and "explain a failure" collapse into
      // each other and the first case would pass on a card that is simply always drawn.
      stubGateway({ enabled: false })
      vi.spyOn(api, 'dashboardConfig').mockReturnValue(new Promise(() => {}) as never)
      renderSection()
      await waitFor(() => {
        expect(screen.getByRole('switch', { name: /Crew Members/i })).toBeInTheDocument()
      })
      expect(screen.queryByRole('switch', { name: 'Decisions (Jev)' })).toBeNull()
      expect(screen.queryByText(/could not read the settings/i)).toBeNull()
    })

    it('draws the card when the ceiling permits it', async () => {
      // The control: the same stubs, one field flipped, and the card is back —
      // so the two cases above measure the gate rather than a broken render.
      stubGateway({ enabled: false }, { decisions: { bucket: 100 } }, { decisions_enabled: true })
      renderSection()
      await waitFor(() => {
        expect(decisionsSwitch()).toBeInTheDocument()
      })
      expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    })
  })
})

describe('the whole-transcript consent scope', () => {
  it('is not drawn on its own panel while the main switch is off', async () => {
    // With consent withheld nothing is sent at all, so a second egress control would
    // describe a state that cannot happen -- and the gateway refuses the write anyway.
    //
    // The panel is OPENED before asserting absence. Without that, the assertion is
    // true of any point that is not the selected one, so it would pass on a card that
    // offered the switch with consent withheld -- which is the defect it names.
    stubGateway({ enabled: false })
    renderSection()
    expect(await scopeSwitchOnPanel('compaction')).toBeNull()
    // The row itself is still there: a point a reader cannot use is what the card
    // exists to explain.
    expect(pointRow(SCOPE_POINT.compaction)).toBeInTheDocument()
  })

  it('is not drawn on its own panel while the address moved out from under consent', async () => {
    // The switch is recorded-on here and still must not be offered: nothing is being
    // sent, so granting a wider category would be a control over an egress that is not
    // happening, and the route answers 409 for it.
    stubGateway(
      consentOf(true, {
        endpoint: ENDPOINT,
        configured_endpoint: 'https://proxy.example/v1/systemone',
        permits: false,
        compaction: true,
      }),
    )
    renderSection()
    expect(await scopeSwitchOnPanel('compaction')).toBeNull()
  })

  it('draws OFF for a consent recorded before the scope existed', async () => {
    // The whole point of a third leaf: an owner who consented to sending, and even to
    // tool arguments, has not consented to a whole transcript. Asserted one panel at a
    // time, because each switch is the OK for its own point.
    stubGateway(consentOf(true, { tool_args: true }))
    renderSection()
    expect((await openScope('tool_args')).getAttribute('aria-checked')).toBe('true')
    expect((await openScope('compaction')).getAttribute('aria-checked')).toBe('false')
  })

  it('draws ON when the keystone recorded it', async () => {
    stubGateway(consentOf(true, { compaction: true }))
    renderSection()
    expect((await openScope('compaction')).getAttribute('aria-checked')).toBe('true')
  })

  it('grants the scope through the consent route, naming only its own field', async () => {
    stubGateway({ enabled: true })
    const scope = vi
      .spyOn(api, 'saveDecisionsScope')
      .mockResolvedValue(consentOf(true, { compaction: true }))
    const save = vi.spyOn(api, 'saveDecisionsConsent')
    renderSection()
    const sw = await openScope('compaction')
    sw.click()
    await waitFor(() => {
      // ONE field, and no endpoint echo: every other scope is omitted, which is what
      // preserves it, and echoing an address is a review this switch is not.
      expect(scope).toHaveBeenCalledWith('compaction', true)
    })
    // And the switch that means consent itself is untouched.
    expect(save).not.toHaveBeenCalled()
  })

  it('revokes it with an explicit false rather than by omission', async () => {
    stubGateway(consentOf(true, { compaction: true }))
    const scope = vi.spyOn(api, 'saveDecisionsScope').mockResolvedValue(consentOf(true))
    renderSection()
    const sw = await openScope('compaction')
    expect(sw.getAttribute('aria-checked')).toBe('true')
    sw.click()
    await waitFor(() => {
      // An explicit false: omission PRESERVES a scope, so a revoke has to be written.
      expect(scope).toHaveBeenCalledWith('compaction', false)
    })
  })

  it('names the point on its own panel, as the decision log spells it', async () => {
    stubGateway(consentOf(true, { compaction: true }))
    renderSection()
    await openScope('compaction')
    // The identifier a reader greps the decision log for, on the panel for the point
    // it belongs to.
    expect(screen.getByTitle('compaction.keep')).toBeInTheDocument()
    cleanup()
    // And a gateway that does not ship the point names it nowhere: the card holds a
    // label per id and no list of its own, so the row and the identifier both go.
    stubGateway(
      consentOf(true, {
        points: pointsOf(true).filter(row => row.id !== 'compaction.keep'),
      }),
    )
    renderSection()
    await waitFor(() => {
      expect(screen.getByTitle('skills.select')).toBeInTheDocument()
    })
    expect(screen.queryByTitle('compaction.keep')).toBeNull()
    expect(
      screen.queryByRole('tab', { name: /Which tool calls a compaction would keep/i }),
    ).toBeNull()
  })

  it('says the compaction itself is unchanged', async () => {
    // A switch that read as "better compaction" would be a promise this build does
    // not keep: nothing is applied, and the answer is a line on a notice.
    stubGateway({ enabled: true })
    renderSection()
    await openScope('compaction')
    const rendered = document.body.textContent ?? ''
    expect(rendered).toContain('It is a measurement')
    expect(rendered).toContain('Tool OUTPUT is never sent')
    expect(rendered).toContain('Passwords and keys are replaced')
  })
})

describe('the whole-transcript scope reports its own in-flight and failed states', () => {
  /**
   * Both are about THIS PR's switch. A scope PUT carries `enabled: true` — the scope is
   * only meaningful while the seam is on — so a scope flip followed immediately by a
   * main switch OFF is two concurrent writes to one keystone, and the scope one
   * landing second restores consent the owner just revoked. And a refused scope write is
   * otherwise silent: the switch snaps back on the refetch with nothing saying why.
   *
   * The FREEZE is asserted for both scopes, because it is one hazard rather than one per
   * scope and the main switch's condition is this row's own. The tool-argument switch's
   * error surface is still not asserted here: that switch shipped before this one, and
   * #12492 is fixing the server side for every scope at once.
   */
  const heldSave = () => {
    let release: (v: DecisionsConsentData) => void = () => {}
    const promise = new Promise<DecisionsConsentData>(resolve => { release = resolve })
    return { promise, release: () => release(consentOf(true)) }
  }

  it('freezes the main switch while the scope write is in flight', async () => {
    stubGateway(consentOf(true))
    const held = heldSave()
    vi.spyOn(api, 'saveDecisionsScope').mockReturnValue(held.promise)
    renderSection()
    const sw = await openScope('compaction')
    expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    sw.click()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).toBe('true')
    })
    held.release()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    })
  })

  it('freezes the main switch while the TOOL-ARGUMENT scope write is in flight too', async () => {
    // Same hazard, other scope: a condition that named only one of the two covered part
    // of its own subject.
    stubGateway(consentOf(true))
    const held = heldSave()
    vi.spyOn(api, 'saveDecisionsScope').mockReturnValue(held.promise)
    renderSection()
    const sw = await openScope('tool_args')
    expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    sw.click()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).toBe('true')
    })
    held.release()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    })
  })

  it('says so when the scope write is refused, rather than reverting in silence', async () => {
    stubGateway(consentOf(true))
    vi.spyOn(api, 'saveDecisionsScope').mockRejectedValue(new Error('dashboard owner required'))
    renderSection()
    const sw = await openScope('compaction')
    sw.click()
    await waitFor(() => {
      expect(screen.getByTestId('decisions-save-error')).toBeInTheDocument()
    })
    // The stored value is what the switch shows: the refused write did not take.
    expect(sw.getAttribute('aria-checked')).toBe('false')
  })

  it('draws no error while the write is merely in flight', async () => {
    stubGateway(consentOf(true))
    const held = heldSave()
    vi.spyOn(api, 'saveDecisionsScope').mockReturnValue(held.promise)
    renderSection()
    const sw = await openScope('compaction')
    sw.click()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).toBe('true')
    })
    expect(screen.queryByTestId('decisions-save-error')).toBeNull()
    held.release()
  })
})

describe('the recalled-memory scope switch', () => {
  it('is not offered while the main switch is off', async () => {
    // Off, nothing is sent at all, so a second egress control would describe a
    // state that cannot happen.
    stubGateway({ enabled: false })
    renderSection()
    await waitFor(() => {
      expect(decisionsSwitch()).toBeInTheDocument()
    })
    expect(screen.queryByRole('switch', { name: /snippets of recalled memories/i })).toBeNull()
  })

  it('appears unchecked once consent is on, for a keystone that never recorded it', async () => {
    // The state every install consented before this scope existed is in: sending is
    // allowed, recalled-memory text is not, and the card must draw exactly that
    // rather than inferring the scope from the main switch.
    stubGateway({ enabled: true })
    renderSection()
    await openScope('memory_text')
    expect(memoryTextSwitch().getAttribute('aria-checked')).toBe('false')
  })

  it('reflects a recorded scope', async () => {
    stubGateway(consentOf(true, { memory_text: true }))
    renderSection()
    await openScope('memory_text')
    await waitFor(() => {
      expect(memoryTextSwitch().getAttribute('aria-checked')).toBe('true')
    })
  })

  it('is not granted by the tool-argument scope', async () => {
    // Two independent decisions. A card that read one switch off the other would
    // show a consent the owner never gave.
    stubGateway(consentOf(true, { tool_args: true }))
    renderSection()
    await openScope('tool_args')
    await waitFor(() => {
      expect(toolArgsSwitch().getAttribute('aria-checked')).toBe('true')
    })
    // Each scope's switch is on its OWN point's panel, so the second answer is read
    // where it lives rather than beside the first.
    await openScope('memory_text')
    expect(memoryTextSwitch().getAttribute('aria-checked')).toBe('false')
  })

  it('grants the scope through the same consent route, naming only itself', async () => {
    stubGateway({ enabled: true })
    const scopeSave = vi.spyOn(api, 'saveDecisionsScope').mockResolvedValue(
      consentOf(true, { memory_text: true }),
    )
    renderSection()
    await openScope('memory_text')
    memoryTextSwitch().click()
    await waitFor(() => {
      // ONLY its own scope: naming the other would let this click grant or erase a
      // scope the owner did not touch, which the route's omission rule exists for.
      expect(scopeSave).toHaveBeenCalledWith('memory_text', true)
    })
  })

  it('revokes it with an explicit false rather than by omission', async () => {
    stubGateway(consentOf(true, { memory_text: true }))
    const scopeSave = vi.spyOn(api, 'saveDecisionsScope').mockResolvedValue(consentOf(true))
    renderSection()
    await openScope('memory_text')
    await waitFor(() => {
      expect(memoryTextSwitch().getAttribute('aria-checked')).toBe('true')
    })
    memoryTextSwitch().click()
    await waitFor(() => {
      expect(scopeSave).toHaveBeenCalledWith('memory_text', false)
    })
  })

  it('states what the extra data is, and what the decision can do with it', async () => {
    stubGateway({ enabled: true })
    renderSection()
    await openScope('memory_text')
    const body = document.body.textContent ?? ''
    expect(body).toContain('first 200 characters')
    expect(body).toContain('It can only remove them')
    expect(body).toContain('Passwords and keys are replaced')
  })

  it('names recalled-memory snippets in the egress note, above either switch', async () => {
    // The note is what a reader consents to, and it is drawn whether or not the
    // scope switches are. Naming only messages and skills would understate it.
    stubGateway({ enabled: false })
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/leave this machine/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/snippets of the memories recalled/i)).toBeInTheDocument()
  })
})

describe('a scope write and the main switch cannot interleave', () => {
  // The hole this closes: flip a scope, immediately turn the seam OFF, and the two
  // PUTs race. The disable lands first, the scope write lands second carrying
  // `enabled: true`, and consent is re-committed by a click that was about a scope —
  // the owner turned egress off and it came back on.

  it('says NOTHING about enabled on a scope write', async () => {
    // Not even the value the card holds. That value comes from a read, and a concurrent
    // revoking PUT makes it stale inside the window — writing it back would re-commit a
    // consent the owner had just withdrawn, from a click that was about a scope. Saying
    // nothing is the only shape that cannot, and the gateway preserves the recorded flag.
    stubGateway({ enabled: true })
    const scopeSave = vi.spyOn(api, 'saveDecisionsScope').mockResolvedValue(
      consentOf(true, { memory_text: true }),
    )
    const consentSave = vi.spyOn(api, 'saveDecisionsConsent')
    renderSection()
    await openScope('memory_text')
    memoryTextSwitch().click()
    await waitFor(() => {
      expect(scopeSave).toHaveBeenCalledWith('memory_text', true)
    })
    // The scope path must not reach the consent writer at all: that is the function
    // whose body carries `enabled`.
    expect(consentSave).not.toHaveBeenCalled()
  })

  it('freezes the MAIN switch while a scope write is in flight', async () => {
    // The other half: the interleave cannot be started, not merely made harmless.
    stubGateway({ enabled: true })
    let release = () => {}
    vi.spyOn(api, 'saveDecisionsScope').mockImplementation(
      () => new Promise(resolve => { release = () => resolve(consentOf(true, { memory_text: true })) }),
    )
    renderSection()
    await openScope('memory_text')
    memoryTextSwitch().click()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).toBe('true')
    })
    release()
  })

  it('refuses the NEXT scope write while one scope write is in flight', async () => {
    // Two outstanding writes against one keystone resolve in an order the clicks did
    // not choose, and the route records the whole file under one lock. The invariant is
    // about the WRITE, not about two switches being on screen together: this card draws
    // one point's panel at a time, so the second scope is reached by opening its own
    // panel while the first write is still outstanding, and its switch must refuse.
    stubGateway(consentOf(true, { tool_args: true }))
    let release = () => {}
    vi.spyOn(api, 'saveDecisionsScope').mockImplementation(
      () => new Promise(resolve => { release = () => resolve(consentOf(true, { tool_args: true })) }),
    )
    renderSection()
    await openScope('memory_text')
    memoryTextSwitch().click()
    const other = await openScope('tool_args')
    await waitFor(() => {
      expect(other.getAttribute('aria-disabled')).toBe('true')
    })
    release()
  })

  it('frees every switch again once the scope write settles', async () => {
    stubGateway({ enabled: true })
    vi.spyOn(api, 'saveDecisionsScope').mockResolvedValue(consentOf(true, { memory_text: true }))
    renderSection()
    await openScope('memory_text')
    memoryTextSwitch().click()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    })
  })
})

describe('a refused scope write says so', () => {
  // The switch snaps back to the recorded value on the refetch, which on its own looks
  // like the click never landed. Each scope gets its OWN notice: two failures rendering
  // in one place would leave a reader unable to tell which write was refused.

  it('surfaces a refused tool-argument write, naming that switch', async () => {
    stubGateway({ enabled: true })
    vi.spyOn(api, 'saveDecisionsScope').mockRejectedValue(new Error('dashboard owner required'))
    renderSection()
    await openScope('tool_args')
    toolArgsSwitch().click()
    await waitFor(() => {
      expect(screen.getByTestId('decisions-save-error')).toBeInTheDocument()
    })
    expect(screen.getByTestId('decisions-save-error').textContent).toContain('Also send tool-call arguments')
  })

  it('surfaces a refused recalled-memory write, naming that switch', async () => {
    stubGateway({ enabled: true })
    vi.spyOn(api, 'saveDecisionsScope').mockRejectedValue(new Error('dashboard owner required'))
    renderSection()
    await openScope('memory_text')
    memoryTextSwitch().click()
    await waitFor(() => {
      expect(screen.getByTestId('decisions-save-error')).toBeInTheDocument()
    })
    expect(screen.getByTestId('decisions-save-error').textContent).toContain('Also send snippets of recalled memories')
  })

  it('draws neither notice while both writes are healthy', async () => {
    stubGateway({ enabled: true })
    renderSection()
    await openScope('memory_text')
    expect(screen.queryByTestId('decisions-save-error')).toBeNull()
  })

  it('lists memory.recall among what Jev decides, once its scope is granted', async () => {
    // The list answers "what does Jev decide". It named the skill choice and the
    // compaction measurement and omitted the one point that changes what a TOOL
    // RETURNS, which understates the feature exactly where the reader is deciding
    // whether to grant it.
    stubGateway(consentOf(true, { memory_text: true }))
    renderSection()
    await openScope('memory_text')
    const text = document.body.textContent ?? ''
    expect(text).toContain('Which recalled memories reach the prompt')
    expect(text).toContain('memory.recall')
  })

  it('reports memory.recall as needing your OK while its scope is not granted', async () => {
    // Without the scope the point is inert, and the card must not tell a reader the
    // decision happens. In a list that names every point the GATEWAY projects, that is
    // the row's STATUS rather than its absence: hiding the row would also hide the
    // switch that grants it, leaving a reader no way to reach the thing they are being
    // asked about.
    stubGateway(consentOf(true, { memory_text: false }))
    renderSection()
    const row = await waitFor(() => pointRow('Which recalled memories reach the prompt'))
    expect(row.textContent).toContain('Needs your OK')
    expect(row.textContent).not.toContain('Switched on')
  })

  it('counts the note\'s promised categories against the points that declare a scope', async () => {
    // The note said "Two further categories" while three scopes existed, because the
    // widest one landed between the two the copy knew about. Asserted as a RELATIONSHIP
    // against the DATA, not against switches on screen: one point's panel renders at a
    // time, so counting rendered switches would pin a card shape instead of the
    // invariant. The number in the sentence and the number of points that declare a
    // scope have to move together, which holds whatever the card looks like.
    stubGateway({ enabled: true })
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/leave this machine/i)).toBeInTheDocument()
    })
    const scoped = pointsOf(true).filter(row => row.needs_scope !== null)
    const COUNT_WORD: Record<number, string> = {
      1: 'One further category',
      2: 'Two further categories',
      3: 'Three further categories',
      4: 'Four further categories',
    }
    const note = screen.getByText(/leave this machine/i).textContent ?? ''
    expect(note).toContain(COUNT_WORD[scoped.length])
    // And one clause per category, so the count is not satisfied by a bare number.
    expect(note).toContain('the name and arguments of your tool calls')
    expect(note).toContain('the conversation and tool-call inputs')
    expect(note).toContain('short snippets of the memories recalled')
  })

  it('says WHICH switch could not be saved', async () => {
    // "Could not save this switch" over three switches names none of them, and the
    // widest scope's notice does not even sit beside its own switch. Each notice
    // interpolates the label of the switch it belongs to.
    stubGateway({ enabled: true })
    vi.spyOn(api, 'saveDecisionsScope').mockRejectedValue(new Error('dashboard owner required'))
    renderSection()
    await openScope('memory_text')
    memoryTextSwitch().click()
    await waitFor(() => {
      expect(screen.getByTestId('decisions-save-error')).toBeInTheDocument()
    })
    const notice = screen.getByTestId('decisions-save-error').textContent ?? ''
    expect(notice).toContain('Also send snippets of recalled memories')
    expect(notice).not.toContain('this switch')
  })

  it('names the compaction switch on its own displaced notice', async () => {
    // This is the notice the naming exists for: it renders at the foot of the card,
    // away from the switch it reports on, so without the label a reader cannot tell
    // which of the three writes was refused.
    stubGateway({ enabled: true })
    vi.spyOn(api, 'saveDecisionsScope').mockRejectedValue(new Error('dashboard owner required'))
    renderSection()
    await openScope('compaction')
    compactionSwitch().click()
    await waitFor(() => {
      expect(screen.getByTestId('decisions-save-error')).toBeInTheDocument()
    })
    const notice = screen.getByTestId('decisions-save-error').textContent ?? ''
    expect(notice).toContain('Also send the conversation and tool-call inputs')
    expect(notice).not.toContain('this switch')
  })

  it('names only the scope that failed', async () => {
    // One rejected write must not light the other scope's notice.
    stubGateway({ enabled: true })
    vi.spyOn(api, 'saveDecisionsScope').mockRejectedValue(new Error('nope'))
    renderSection()
    await openScope('memory_text')
    memoryTextSwitch().click()
    await waitFor(() => {
      expect(screen.getByTestId('decisions-save-error')).toBeInTheDocument()
    })
    // One notice, and it names the scope that failed and not the one beside it.
    expect(screen.getByTestId('decisions-save-error').textContent).toContain('Also send snippets of recalled memories')
    expect(screen.getByTestId('decisions-save-error').textContent).not.toContain('Also send tool-call arguments')
  })
})
