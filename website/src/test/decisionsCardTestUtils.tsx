/**
 * Fixtures and queries shared by decisionsCard.test.tsx and decisionsCard.scopes.test.tsx:
 * the section render, the main and tool-argument switches, the gateway's consent payload
 * and point rows, and the stub for the gateway reads every case starts from.
 */
import { vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { api, type DecisionsConsentData } from '../api/client'
import { FeaturePreviewsSection } from '../pages/settings/FeaturePreviewsSection'

/** Rendered through the whole section, because that is where the card ships. */
export function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter><FeaturePreviewsSection /></MemoryRouter>
    </QueryClientProvider>,
  )
}

/** The card's own switch. Named in full so "Decisions" cannot match another row. */
export const decisionsSwitch = () => screen.getByRole('switch', { name: 'Decisions (Jev)' })

export const ENDPOINT = 'https://api.typesafe.ai/v1/systemone'

/**
 * The point rows the gateway projects, in the order its registry lists them.
 *
 * Spelled out here rather than derived, because what the card must do with them is
 * exactly to draw whatever arrives: a test that built these from the same table the
 * card reads would assert nothing about the projection.
 */
export const pointsOf = (
  enabled: boolean,
  toolArgs = false,
  compaction = false,
  memoryText = false,
  judgeProvider = 'auto',
  nudgeEvidence = false,
) => {
  const granted: Record<string, boolean> = {
    tool_args: toolArgs,
    compaction,
    memory_text: memoryText,
    nudge_evidence: nudgeEvidence,
  }
  const status = (scope: string | null) =>
    !enabled ? 'off' : scope && !granted[scope] ? 'needs_scope' : 'active'
  // The gate's own resolution, not a second reading of the switch: `auto` picks Jev
  // when Jev is ARMED -- consent plus this point's scope -- and the small model
  // otherwise, so a pinned lane is honoured and `auto` follows the grant.
  const jevArmed = enabled && nudgeEvidence
  const judgeLane =
    judgeProvider === 'jev' ? 'jev' : judgeProvider === 'llm' ? 'llm' : jevArmed ? 'jev' : 'llm'
  return [
    { id: 'skills.select', needs_scope: null, status: status(null) },
    { id: 'tool.risk', needs_scope: 'tool_args', status: status('tool_args') },
    { id: 'message.steer', needs_scope: null, status: status(null) },
    { id: 'model.route', needs_scope: null, status: status(null) },
    { id: 'compaction.keep', needs_scope: 'compaction', status: status('compaction') },
    { id: 'memory.recall', needs_scope: 'memory_text', status: status('memory_text') },
    // The judge, whose row the gateway resolves from the LANE as well as the keystone.
    // Its small-model lane needs neither the endpoint consent nor a scope, so that lane
    // reports active with the switch off. The Jev lane sends this point's own evidence,
    // so it runs only on the `nudge_evidence` scope, and `auto` resolves the same way
    // the gate's own resolver does -- against whether Jev is ARMED, which is consent
    // plus that grant. Present here because this fixture stands in for the gateway's
    // `DECISION_POINT_NAMES` projection, and a fixture that omits a shipped point lets
    // its row and its switch go untested -- which is how the missing switch reached QA.
    {
      id: 'nudge.wake',
      needs_scope: 'nudge_evidence',
      lane: judgeLane,
      status: judgeLane === 'jev' ? status('nudge_evidence') : 'active',
    },
  ]
}

/** A consent payload as the gateway returns it, bound to the default endpoint. */
export const consentOf = (enabled: boolean, overrides: Partial<DecisionsConsentData> = {}): DecisionsConsentData => ({
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
  // The wake judge's evidence scope, false on the same terms.
  nudge_evidence: false,
  points: pointsOf(
    enabled,
    overrides.tool_args === true,
    overrides.compaction === true,
    overrides.memory_text === true,
    overrides.nudge_evidence === true,
  ),
  ...overrides,
})

/** The row for one point, by the plain-words name the reader sees. */
export const pointRow = (name: string) =>
  screen.getByRole('tab', {
    name: new RegExp(name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i'),
  })

/** Open a point's own panel. Highlighting is not selection: nothing else moves. */
export const openPoint = (name: string) => pointRow(name).click()

/** The accessible name of each scope's consent switch, as the card labels it. */
export const SCOPE_SWITCH: Record<string, string> = {
  tool_args: 'Also send tool-call arguments so Jev can flag risky calls',
  compaction: 'Also send the conversation and tool-call inputs so Jev can score compaction',
  memory_text:
    'Also send snippets of recalled memories so Jev can drop the ones that do not help',
  nudge_evidence: 'Also send what a watching loop has found so Jev can skip a turn',
}

/** The tool-argument consent switch, on `tool.risk`'s own panel. */
export const toolArgsSwitch = () => screen.getByRole('switch', { name: SCOPE_SWITCH.tool_args })

/**
 * Stub all three reads: the governance answer that decides whether the card is
 * drawn, the keystone (or a rejection), and the config's bucket.
 *
 * `decisions_enabled: true` is the ungoverned default every case assumes —
 * a case that wants the withdrawn state passes `governed: false` and gets no card.
 */
export function stubGateway(
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
  // The KAS engine card shares this section; answered so its own read-failure
  // notice does not add a second alert beside this card's.
  vi.spyOn(api, 'kasBundleStatus').mockResolvedValue({
    enabled: false,
    pinned_version: '0.66.33',
    runs_version: '0.66.33',
    installed: null,
    installing: false,
    progress: { done: 0, total: 0 },
    error: '',
  } as never)
  // The provider picker's own read answers "no such route" unless a case stubs a
  // provider, so the cases see the card as an older gateway draws it.
  vi.spyOn(api, 'getDecisionsProvider').mockRejectedValue(Object.assign(new Error('not found'), { status: 404 }))
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
