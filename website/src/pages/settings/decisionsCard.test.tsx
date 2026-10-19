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
 *
 * The scope switches past the tool-argument one, and scope writes in flight, refused or
 * beside the main switch, are in decisionsCard.scopes.test.tsx; the fixtures both files
 * use are in src/test/decisionsCardTestUtils.tsx.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, cleanup, fireEvent, waitFor } from '@testing-library/react'

import { api } from '../../api/client'
import { PREVIEW_FLAG_PREFIX } from '../../utils/previewFlags'
import {
  renderSection,
  decisionsSwitch,
  ENDPOINT,
  pointsOf,
  consentOf,
  pointRow,
  openPoint,
  toolArgsSwitch,
  stubGateway,
} from '../../test/decisionsCardTestUtils'

/** An older gateway has no consent route: the client rejects with a 404. */
const notFound = () => Object.assign(new Error('Not Found'), { status: 404 })

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
      expect(screen.getByText(/could not save this change/i)).toBeInTheDocument()
    })
    // The same notice serves the tier pickers, the history number box and the key
    // field, none of which is a switch, so its words name no control.
    expect(screen.getByTestId('decisions-save-error').textContent ?? '').not.toMatch(/switch/i)
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
      expect(screen.getByText(/sent over the internet to Jev/i)).toBeInTheDocument()
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
    expect(screen.getByText(/the built-in rule decides instead/i)).toBeInTheDocument()
  })

  it('holds the switch and says why while no decision model is chosen', async () => {
    stubGateway({ enabled: false })
    vi.spyOn(api, 'system').mockResolvedValue({ mem_total_gb: 16 } as never)
    vi.spyOn(api, 'getDecisionsProvider').mockResolvedValue({
      presets: [], active: 'none', loopback: false, configured_endpoint: 'none',
    } as never)
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/No decision model is chosen\. Pick one below/)).toBeInTheDocument()
    })
    expect(screen.queryByText(/sent over the internet to Jev/i)).toBeNull()
    expect(screen.queryByText(/^Sent to/)).toBeNull()
    expect(decisionsSwitch().getAttribute('aria-disabled')).toBe('true')
  })

  it('keeps a standing consent revocable after switching to no model', async () => {
    stubGateway({ enabled: true })
    vi.spyOn(api, 'system').mockResolvedValue({ mem_total_gb: 16 } as never)
    vi.spyOn(api, 'getDecisionsProvider').mockResolvedValue({
      presets: [], active: 'none', loopback: false, configured_endpoint: 'none',
    } as never)
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/No decision model is chosen\. Pick one below/)).toBeInTheDocument()
    })
    expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
  })

  it('says the text stays on this machine while a local preset answers', async () => {
    // Any address on this machine -- a preset or a hand-written loopback address --
    // makes "over the internet" false. Only a remote address keeps that sentence.
    stubGateway({ enabled: true })
    const preset = {
      id: 'laya', name: 'Laya', model: 'english', default_port: 8104, jev_relative_pct: 67,
      hard_relative_pct: 47, peak_ram_gb: 6, recommended_total_ram_gb: 24, p50_secs: 0.17,
      p95_secs: 0.51, timeout_ms: 2000, download_bytes: 846207419,
    }
    const provider = (active: string, loopback = true) => ({
      presets: [preset], active, loopback,
      configured_endpoint: loopback ? 'http://127.0.0.1:8104/v1/systemone' : 'https://proxy.example/v1/systemone',
    })
    vi.spyOn(api, 'system').mockResolvedValue({ mem_total_gb: 16 } as never)
    const read = vi.spyOn(api, 'getDecisionsProvider').mockResolvedValue(provider('laya'))
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/sent to the model server at the address below, on this machine/i)).toBeInTheDocument()
    })
    expect(screen.queryByText(/sent over the internet to Jev/i)).toBeNull()
    expect(screen.queryByText(/no Jev API key is sent to it/i)).toBeNull()

    cleanup()
    read.mockResolvedValue(provider('custom'))
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/sent to the model server at the address below, on this machine/i)).toBeInTheDocument()
    })
    expect(screen.queryByText(/sent over the internet to Jev/i)).toBeNull()
    // A hand-written loopback address gets no key; the card says so, not only the log.
    expect(screen.getByText(/no Jev API key is sent to it/i)).toBeInTheDocument()

    cleanup()
    read.mockResolvedValue(provider('custom', false))
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/sent over the internet to Jev/i)).toBeInTheDocument()
    })
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

  it('links to the Releases page when the gateway lists no points', async () => {
    // An older gateway projects no rows. The notice tells the reader to update, so
    // the place it names is a link there, not plain text.
    stubGateway(consentOf(true, { points: [] }))
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/does not list what Jev decides yet/i)).toBeInTheDocument()
    })
    const link = screen.getByRole('link', { name: 'Settings › Releases' })
    expect(link.getAttribute('href')).toBe('/settings/releases')
  })

  it('says where Auto (Jev) is chosen for the model.route point', async () => {
    stubGateway({ enabled: true })
    renderSection()
    await waitFor(() => {
      expect(pointRow("Model for the turn's difficulty")).toBeInTheDocument()
    })
    openPoint("Model for the turn's difficulty")
    // The panel sits behind the lazy `DecisionsPointPanel` boundary, so its chunk
    // may still be loading on a cold run: give it an explicit deadline.
    await waitFor(
      () => {
        expect(screen.getByRole('tabpanel').textContent ?? '').toContain(
          "whose own model picker shows Auto (Jev)",
        )
      },
      { timeout: 5000 },
    )
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
    expect(screen.getByText(/sent over the internet to Jev/i).className).toContain('opacity-40')
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
    // Picked by text: the section also draws the KAS engine card, whose restart pill
    // reports its own unreadable schema as an alert in this unstubbed render.
    const alert = screen.getAllByRole('alert').find(el => /nothing is being sent/i.test(el.textContent ?? ''))
    expect(alert).toBeDefined()
    expect(alert).toHaveTextContent(/nothing is being sent/i)
    expect(alert?.className).toContain('text-warn')
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
        expect(screen.getByRole('switch', { name: /Webhooks/i })).toBeInTheDocument()
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
        expect(screen.getByRole('switch', { name: /Webhooks/i })).toBeInTheDocument()
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
        expect(screen.getByRole('switch', { name: /Webhooks/i })).toBeInTheDocument()
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
