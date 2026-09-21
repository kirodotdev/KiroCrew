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
import { render, screen, cleanup, waitFor } from '@testing-library/react'
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
  ...overrides,
})

/** The tool-argument consent switch. Only drawn while the main switch is on. */
const toolArgsSwitch = () =>
  screen.getByRole('switch', { name: 'Also send tool-call arguments so Jev can flag risky calls' })

/** The recalled-memory consent switch. Only drawn while the main switch is on. */
const memoryTextSwitch = () =>
  screen.getByRole('switch', { name: /snippets of recalled memories/i })

/** The whole-transcript consent switch. Only drawn while the main switch is on. */
const compactionSwitch = () =>
  screen.getByRole('switch', {
    name: 'Also send the conversation and tool-call inputs so Jev can score compaction',
  })

/**
 * Stub all three reads: the governance answer that decides whether the card is
 * drawn, the keystone (or a rejection), and the config's bucket.
 *
 * `decisions_enabled: true` is the ungoverned default every case below assumes —
 * a case that wants the withdrawn state passes `governed: false` and gets no card.
 */
function stubGateway(
  consent: DecisionsConsentData | { enabled: boolean } | Error,
  config: unknown = { decisions: { bucket: 100 } },
  dashboard: unknown = { decisions_enabled: true },
) {
  vi.spyOn(api, 'dashboardConfig').mockResolvedValue(dashboard as never)
  vi.spyOn(api, 'kirocrewConfig').mockResolvedValue(config as never)
  if (consent instanceof Error) {
    vi.spyOn(api, 'getDecisionsConsent').mockRejectedValue(consent)
  } else {
    const full = 'permits' in consent ? consent : consentOf(consent.enabled)
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

  it('names the one live point in plain words beside its identifier, with no state of its own', async () => {
    stubGateway({ enabled: true }, { decisions: { bucket: 100 } })
    renderSection()
    await waitFor(() => {
      expect(screen.getByText('skills.select')).toBeInTheDocument()
    })
    // Read off the ROW: the gloss a reader understands, then the identifier they
    // grep the log for, introduced as such — and nothing else. The switch is the
    // card's one on/off, so an "Enabled"/"Off" word here would read as a second
    // control.
    expect(screen.getByText('skills.select').parentElement?.parentElement?.textContent)
      .toBe('Automatic skill choicelogged as skills.select')
    expect(screen.getByText(/what Jev decides while this is on/i)).toBeInTheDocument()
    // Nothing consumes the answers of any other point, so no other row exists.
    expect(screen.queryByText('skills.dedupe')).toBeNull()
    expect(screen.queryByText('cron.novelty')).toBeNull()
    // 100 is the shipped default, and it is said out loud: "a sample" would
    // understate what a default enable sends.
    expect(screen.getByText(/Jev answers for all of your sessions/i)).toBeInTheDocument()
    expect(screen.getByText(/set decisions\.bucket in config\.json/i)).toBeInTheDocument()
  })

  it('keeps the row and the share while the switch is off, phrased for the on state', async () => {
    stubGateway({ enabled: false }, { decisions: { bucket: 25 } })
    renderSection()
    await waitFor(() => {
      expect(screen.getByText('skills.select')).toBeInTheDocument()
    })
    // Same row either way: it says what turning the switch on does, not whether
    // it is on. The header already scopes it to "while this is on".
    expect(screen.getByText('skills.select').parentElement?.parentElement?.textContent)
      .toBe('Automatic skill choicelogged as skills.select')
    // The share is what the reader is consenting to, so it is shown BEFORE the
    // switch goes on -- as a statement about the on state, not a claim that
    // anything is being sampled now.
    expect(screen.getByText(/While this is on, Jev answers for 25% of your sessions/i))
      .toBeInTheDocument()
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

  it('prints the sampling rate when the config narrows it, and names the knob', async () => {
    stubGateway({ enabled: true }, { decisions: { bucket: 25 } })
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/25% of your sessions/i)).toBeInTheDocument()
    })
    // The rate is not something this card lets you move, so the line has to say
    // where it IS moved — otherwise it reads as a number from nowhere.
    expect(screen.getByText(/set decisions\.bucket in config\.json/i)).toBeInTheDocument()
    // Zero is a state worth printing too: on, and deciding for nobody.
    cleanup()
    vi.restoreAllMocks()
    stubGateway({ enabled: true }, { decisions: { bucket: 0 } })
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/0% of your sessions/i)).toBeInTheDocument()
    })
  })

  it('carries no point row at all against a gateway without the consent route', async () => {
    // An older gateway has no point wired, so a row reading "off" would describe
    // a check that does not exist rather than one that is switched off.
    stubGateway(notFound(), { telemetry: {} })
    renderSection()
    await waitFor(() => {
      expect(screen.getByText(/older than this feature/i)).toBeInTheDocument()
    })
    expect(screen.queryByText('skills.select')).toBeNull()
    expect(screen.queryByText(/what Jev decides while this is on/i)).toBeNull()
  })

  it('names the address consent is given for', async () => {
    // Consent is to an ADDRESS, not to "sending": the reader must see where.
    stubGateway(consentOf(false, { configured_endpoint: 'https://proxy.example/v1/systemone' }))
    renderSection()
    await waitFor(() => {
      expect(screen.getByText('https://proxy.example/v1/systemone')).toBeInTheDocument()
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
      expect(screen.queryByRole('switch', { name: /tool-call arguments/ })).toBeNull()
    })

    it('appears unchecked once consent is on, for a keystone that never recorded it', async () => {
      // The state every install consented before this scope existed is in: sending
      // is allowed, tool arguments are not, and the card must draw exactly that
      // rather than inferring the scope from the main switch.
      stubGateway({ enabled: true })
      renderSection()
      await waitFor(() => {
        expect(toolArgsSwitch()).toBeInTheDocument()
      })
      expect(toolArgsSwitch().getAttribute('aria-checked')).toBe('false')
    })

    it('reflects a recorded scope', async () => {
      stubGateway(consentOf(true, { tool_args: true }))
      renderSection()
      await waitFor(() => {
        expect(toolArgsSwitch().getAttribute('aria-checked')).toBe('true')
      })
    })

    it('grants the scope through the same consent route, with no new endpoint', async () => {
      stubGateway({ enabled: true })
      const save = vi.spyOn(api, 'saveDecisionsScope').mockResolvedValue(
        consentOf(true, { tool_args: true }),
      )
      renderSection()
      await waitFor(() => {
        expect(toolArgsSwitch()).toBeInTheDocument()
      })
      toolArgsSwitch().click()
      await waitFor(() => {
        // No switch in the body at all, so this view cannot re-grant a consent
        // revoked since it last read -- and no endpoint either, because the recorded
        // address is part of what an absent switch preserves. Only its own scope is
        // named: naming another would let this click move a switch nobody touched.
        expect(save).toHaveBeenCalledWith({ toolArgs: true })
      })
    })

    it('revokes it with an explicit false rather than by omission', async () => {
      // Omission PRESERVES the recorded scope on this route, so a revoke has to send
      // the boolean. A card that omitted it would leave the scope granted.
      stubGateway(consentOf(true, { tool_args: true }))
      const save = vi.spyOn(api, 'saveDecisionsScope').mockResolvedValue(consentOf(true))
      renderSection()
      await waitFor(() => {
        expect(toolArgsSwitch().getAttribute('aria-checked')).toBe('true')
      })
      toolArgsSwitch().click()
      await waitFor(() => {
        expect(save).toHaveBeenCalledWith({ toolArgs: false })
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
  it('is not drawn while the main switch is off', () => {
    stubGateway({ enabled: false })
    renderSection()
    expect(
      screen.queryByRole('switch', {
        name: 'Also send the conversation and tool-call inputs so Jev can score compaction',
      }),
    ).toBeNull()
  })

  it('draws OFF for a consent recorded before the scope existed', async () => {
    // The whole point of a third leaf: an owner who consented to sending, and even to
    // tool arguments, has not consented to a whole transcript.
    stubGateway(consentOf(true, { tool_args: true }))
    renderSection()
    await waitFor(() => {
      expect(toolArgsSwitch().getAttribute('aria-checked')).toBe('true')
      expect(compactionSwitch().getAttribute('aria-checked')).toBe('false')
    })
  })

  it('draws ON when the keystone recorded it', async () => {
    stubGateway(consentOf(true, { compaction: true }))
    renderSection()
    await waitFor(() => {
      expect(compactionSwitch().getAttribute('aria-checked')).toBe('true')
    })
  })

  it('grants the scope through the same consent route, with no new endpoint', async () => {
    stubGateway({ enabled: true })
    const save = vi.spyOn(api, 'saveDecisionsScope').mockResolvedValue(
      consentOf(true, { compaction: true }),
    )
    renderSection()
    await waitFor(() => {
      expect(compactionSwitch()).toBeInTheDocument()
    })
    compactionSwitch().click()
    await waitFor(() => {
      // The other scopes go UNNAMED, which is deliberate: an omitted field preserves
      // the recorded scope, so acting on this switch cannot grant or erase either one
      // beside it.
      expect(save).toHaveBeenCalledWith({ compaction: true })
    })
  })

  it('revokes it with an explicit false rather than by omission', async () => {
    stubGateway(consentOf(true, { compaction: true }))
    const save = vi.spyOn(api, 'saveDecisionsScope').mockResolvedValue(consentOf(true))
    renderSection()
    await waitFor(() => {
      expect(compactionSwitch().getAttribute('aria-checked')).toBe('true')
    })
    compactionSwitch().click()
    await waitFor(() => {
      expect(save).toHaveBeenCalledWith({ compaction: false })
    })
  })

  it('names the point only while the scope is granted', async () => {
    stubGateway(consentOf(true, { compaction: true }))
    renderSection()
    await waitFor(() => {
      expect(screen.getByTitle('compaction.keep')).toBeInTheDocument()
    })
    cleanup()
    stubGateway(consentOf(true))
    renderSection()
    await waitFor(() => {
      expect(screen.getByTitle('skills.select')).toBeInTheDocument()
    })
    expect(screen.queryByTitle('compaction.keep')).toBeNull()
  })

  it('says the compaction itself is unchanged', async () => {
    // A switch that read as "better compaction" would be a promise this build does
    // not keep: nothing is applied, and the answer is a line on a notice.
    stubGateway({ enabled: true })
    renderSection()
    await waitFor(() => {
      expect(compactionSwitch()).toBeInTheDocument()
    })
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
    await waitFor(() => { expect(compactionSwitch()).toBeInTheDocument() })
    expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    compactionSwitch().click()
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
    await waitFor(() => { expect(toolArgsSwitch()).toBeInTheDocument() })
    expect(decisionsSwitch().getAttribute('aria-disabled')).not.toBe('true')
    toolArgsSwitch().click()
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
    await waitFor(() => { expect(compactionSwitch()).toBeInTheDocument() })
    compactionSwitch().click()
    await waitFor(() => {
      expect(screen.getByTestId('decisions-compaction-error')).toBeInTheDocument()
    })
    // The stored value is what the switch shows: the refused write did not take.
    expect(compactionSwitch().getAttribute('aria-checked')).toBe('false')
  })

  it('draws no error while the write is merely in flight', async () => {
    stubGateway(consentOf(true))
    const held = heldSave()
    vi.spyOn(api, 'saveDecisionsScope').mockReturnValue(held.promise)
    renderSection()
    await waitFor(() => { expect(compactionSwitch()).toBeInTheDocument() })
    compactionSwitch().click()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).toBe('true')
    })
    expect(screen.queryByTestId('decisions-compaction-error')).toBeNull()
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
    await waitFor(() => {
      expect(memoryTextSwitch()).toBeInTheDocument()
    })
    expect(memoryTextSwitch().getAttribute('aria-checked')).toBe('false')
  })

  it('reflects a recorded scope', async () => {
    stubGateway(consentOf(true, { memory_text: true }))
    renderSection()
    await waitFor(() => {
      expect(memoryTextSwitch().getAttribute('aria-checked')).toBe('true')
    })
  })

  it('is not granted by the tool-argument scope', async () => {
    // Two independent decisions. A card that read one switch off the other would
    // show a consent the owner never gave.
    stubGateway(consentOf(true, { tool_args: true }))
    renderSection()
    await waitFor(() => {
      expect(toolArgsSwitch().getAttribute('aria-checked')).toBe('true')
    })
    expect(memoryTextSwitch().getAttribute('aria-checked')).toBe('false')
  })

  it('grants the scope through the same consent route, naming only itself', async () => {
    stubGateway({ enabled: true })
    const scopeSave = vi.spyOn(api, 'saveDecisionsScope').mockResolvedValue(
      consentOf(true, { memory_text: true }),
    )
    renderSection()
    await waitFor(() => {
      expect(memoryTextSwitch()).toBeInTheDocument()
    })
    memoryTextSwitch().click()
    await waitFor(() => {
      // ONLY its own scope: naming the other would let this click grant or erase a
      // scope the owner did not touch, which the route's omission rule exists for.
      expect(scopeSave).toHaveBeenCalledWith({ memoryText: true })
    })
  })

  it('revokes it with an explicit false rather than by omission', async () => {
    stubGateway(consentOf(true, { memory_text: true }))
    const scopeSave = vi.spyOn(api, 'saveDecisionsScope').mockResolvedValue(consentOf(true))
    renderSection()
    await waitFor(() => {
      expect(memoryTextSwitch().getAttribute('aria-checked')).toBe('true')
    })
    memoryTextSwitch().click()
    await waitFor(() => {
      expect(scopeSave).toHaveBeenCalledWith({ memoryText: false })
    })
  })

  it('states what the extra data is, and what the decision can do with it', async () => {
    stubGateway({ enabled: true })
    renderSection()
    await waitFor(() => {
      expect(memoryTextSwitch()).toBeInTheDocument()
    })
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
    await waitFor(() => {
      expect(memoryTextSwitch()).toBeInTheDocument()
    })
    memoryTextSwitch().click()
    await waitFor(() => {
      expect(scopeSave).toHaveBeenCalledWith({ memoryText: true })
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
    await waitFor(() => {
      expect(memoryTextSwitch()).toBeInTheDocument()
    })
    memoryTextSwitch().click()
    await waitFor(() => {
      expect(decisionsSwitch().getAttribute('aria-disabled')).toBe('true')
    })
    release()
  })

  it('freezes the OTHER scope switch while one scope write is in flight', async () => {
    // Two outstanding writes against one keystone resolve in an order the clicks did
    // not choose, and the route records the whole file under one lock.
    stubGateway(consentOf(true, { tool_args: true }))
    let release = () => {}
    vi.spyOn(api, 'saveDecisionsScope').mockImplementation(
      () => new Promise(resolve => { release = () => resolve(consentOf(true, { tool_args: true })) }),
    )
    renderSection()
    await waitFor(() => {
      expect(memoryTextSwitch()).toBeInTheDocument()
    })
    memoryTextSwitch().click()
    await waitFor(() => {
      expect(toolArgsSwitch().getAttribute('aria-disabled')).toBe('true')
    })
    release()
  })

  it('frees every switch again once the scope write settles', async () => {
    stubGateway({ enabled: true })
    vi.spyOn(api, 'saveDecisionsScope').mockResolvedValue(consentOf(true, { memory_text: true }))
    renderSection()
    await waitFor(() => {
      expect(memoryTextSwitch()).toBeInTheDocument()
    })
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

  it('surfaces a refused tool-argument write, with a hand-off', async () => {
    stubGateway({ enabled: true })
    vi.spyOn(api, 'saveDecisionsScope').mockRejectedValue(new Error('dashboard owner required'))
    renderSection()
    await waitFor(() => {
      expect(toolArgsSwitch()).toBeInTheDocument()
    })
    toolArgsSwitch().click()
    await waitFor(() => {
      expect(screen.getByTestId('decisions-tool-args-error')).toBeInTheDocument()
    })
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
  })

  it('surfaces a refused recalled-memory write, with a hand-off', async () => {
    stubGateway({ enabled: true })
    vi.spyOn(api, 'saveDecisionsScope').mockRejectedValue(new Error('dashboard owner required'))
    renderSection()
    await waitFor(() => {
      expect(memoryTextSwitch()).toBeInTheDocument()
    })
    memoryTextSwitch().click()
    await waitFor(() => {
      expect(screen.getByTestId('decisions-memory-text-error')).toBeInTheDocument()
    })
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
  })

  it('draws neither notice while both writes are healthy', async () => {
    stubGateway({ enabled: true })
    renderSection()
    await waitFor(() => {
      expect(memoryTextSwitch()).toBeInTheDocument()
    })
    expect(screen.queryByTestId('decisions-tool-args-error')).toBeNull()
    expect(screen.queryByTestId('decisions-memory-text-error')).toBeNull()
  })

  it('lists memory.recall among what Jev decides, once its scope is granted', async () => {
    // The list answers "what does Jev decide". It named the skill choice and the
    // compaction measurement and omitted the one point that changes what a TOOL
    // RETURNS, which understates the feature exactly where the reader is deciding
    // whether to grant it.
    stubGateway(consentOf(true, { memory_text: true }))
    renderSection()
    await waitFor(() => {
      expect(memoryTextSwitch()).toBeInTheDocument()
    })
    const text = document.body.textContent ?? ''
    expect(text).toContain('Which recalled memories reach the prompt')
    expect(text).toContain('memory.recall')
  })

  it('omits the memory.recall row while its scope is not granted', async () => {
    // Gated exactly as the compaction row is: without the scope the point is inert,
    // so a row for it would name a decision that cannot happen.
    stubGateway(consentOf(true, { memory_text: false }))
    renderSection()
    await waitFor(() => {
      expect(memoryTextSwitch()).toBeInTheDocument()
    })
    const text = document.body.textContent ?? ''
    expect(text).not.toContain('Which recalled memories reach the prompt')
    // The skill row is still there, so this is a gate rather than an empty list.
    expect(text).toContain('skills.select')
  })

  it('counts the scope switches the note promises against the ones drawn', async () => {
    // The note said "Two further categories" while three switches rendered below it,
    // because the widest scope landed between the two this file's author knew about.
    // Asserted as a RELATIONSHIP rather than as a phrase: the number in the sentence
    // and the number of switches have to move together or the note misleads again.
    stubGateway({ enabled: true })
    renderSection()
    await waitFor(() => {
      expect(memoryTextSwitch()).toBeInTheDocument()
    })
    const note = screen.getByText(/leave this machine/i).textContent ?? ''
    expect(note).toContain('Three further categories')
    expect(note).toContain('the name and arguments of your tool calls')
    expect(note).toContain('the conversation and tool-call inputs')
    expect(note).toContain('short snippets of the memories recalled')
    expect(toolArgsSwitch()).toBeInTheDocument()
    expect(compactionSwitch()).toBeInTheDocument()
    expect(memoryTextSwitch()).toBeInTheDocument()
  })

  it('says WHICH switch could not be saved', async () => {
    // "Could not save this switch" over three switches names none of them, and the
    // widest scope's notice does not even sit beside its own switch. Each notice
    // interpolates the label of the switch it belongs to.
    stubGateway({ enabled: true })
    vi.spyOn(api, 'saveDecisionsScope').mockRejectedValue(new Error('dashboard owner required'))
    renderSection()
    await waitFor(() => {
      expect(memoryTextSwitch()).toBeInTheDocument()
    })
    memoryTextSwitch().click()
    await waitFor(() => {
      expect(screen.getByTestId('decisions-memory-text-error')).toBeInTheDocument()
    })
    const notice = screen.getByTestId('decisions-memory-text-error').textContent ?? ''
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
    await waitFor(() => {
      expect(compactionSwitch()).toBeInTheDocument()
    })
    compactionSwitch().click()
    await waitFor(() => {
      expect(screen.getByTestId('decisions-compaction-error')).toBeInTheDocument()
    })
    const notice = screen.getByTestId('decisions-compaction-error').textContent ?? ''
    expect(notice).toContain('Also send the conversation and tool-call inputs')
    expect(notice).not.toContain('this switch')
  })

  it('names only the scope that failed', async () => {
    // One rejected write must not light the other scope's notice.
    stubGateway({ enabled: true })
    vi.spyOn(api, 'saveDecisionsScope').mockRejectedValue(new Error('nope'))
    renderSection()
    await waitFor(() => {
      expect(memoryTextSwitch()).toBeInTheDocument()
    })
    memoryTextSwitch().click()
    await waitFor(() => {
      expect(screen.getByTestId('decisions-memory-text-error')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('decisions-tool-args-error')).toBeNull()
  })
})
