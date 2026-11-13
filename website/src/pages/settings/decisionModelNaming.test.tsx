/**
 * What a reader sees names the FEATURE, "decision model", never one provider.
 *
 * The Decision models card is rendered in every state its governance and provider
 * can put it in, and the transcript's decision rows beside it, and every visible
 * text, `aria-label` and `title` is read. Only the hosted provider's own entry in the
 * picker (`decisions-provider-hosted-entry`: its name, its detail line, its API key)
 * may say "Jev"; anywhere else the name would describe the feature as that provider
 * whichever model actually answers.
 *
 * The catalog half of the same rule is `src/i18n/decisionModelNaming.test.ts`.
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../../api/client'
import { queryClient } from '../../api/queryClient'
import type { DecisionsProviderData } from '../../api/client/decisions'
import DecisionStrip from '../chat/DecisionStrip'
import SteerDecisionLine from '../chat/SteerDecisionLine'
import { __resetVerdicts, readDecisionStrip, readSteerRecord } from '../chat/decisionRecord'
import {
  ENDPOINT,
  consentOf,
  decisionsSwitch,
  renderSection,
  stubGateway,
} from '../../test/decisionsCardTestUtils'

const HOSTED_ENTRY = '[data-testid="decisions-provider-hosted-entry"]'
const SAGEMAKER = 'sagemaker://decision-model-a?region=us-west-2'

const laya = {
  id: 'laya', name: 'Laya', model: 'english', default_port: 8104, jev_relative_pct: 67, hard_relative_pct: 47,
  peak_ram_gb: 6, recommended_total_ram_gb: 24, p50_secs: 0.17, p95_secs: 0.51, timeout_ms: 2000,
  download_bytes: 846207419, installed: true,
}
const idle = { preset: '', state: 'idle', port: 0, bytes_done: 0, bytes_total: 0, error: '' }

const ALL_ALLOWED = { jev: true, laya: true, sagemaker: true, custom: true }

function provider(overrides: Partial<DecisionsProviderData> = {}): DecisionsProviderData {
  return {
    presets: [laya],
    active: 'jev',
    configured_endpoint: ENDPOINT,
    loopback: false,
    hosted_permitted: true,
    local_permitted: true,
    sagemaker_available: true,
    sagemaker_permitted: true,
    sagemaker: null,
    runtime: idle,
    provider_permitted: ALL_ALLOWED,
    ...overrides,
  } as DecisionsProviderData
}

/**
 * Every place the provider's name could reach a reader outside its own entry: the
 * text of every text node, and each `aria-label`, `aria-description` and `title`.
 */
function namesOutsideTheHostedEntry(root: HTMLElement = document.body): string[] {
  const found: string[] = []
  const outside = (node: Node) => {
    const el = node instanceof Element ? node : node.parentElement
    return !el?.closest(HOSTED_ENTRY)
  }
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (n.textContent?.includes('Jev') && outside(n)) found.push(`text: ${n.textContent.trim()}`)
  }
  for (const el of Array.from(root.querySelectorAll('[aria-label], [aria-description], [title]'))) {
    for (const attr of ['aria-label', 'aria-description', 'title']) {
      const value = el.getAttribute(attr)
      if (value?.includes('Jev') && outside(el)) found.push(`${attr}: ${value}`)
    }
  }
  return found
}

beforeEach(() => {
  localStorage.clear()
  __resetVerdicts()
  queryClient.clear()
  vi.spyOn(api, 'system').mockResolvedValue({ mem_total_gb: 32 } as never)
})

afterEach(() => {
  cleanup()
  queryClient.clear()
  vi.restoreAllMocks()
})

/** Render the section with *data* as the provider read, and wait for the picker. */
async function renderCard(data: DecisionsProviderData, consent = consentOf(true)) {
  stubGateway({ ...consent, configured_endpoint: data.configured_endpoint })
  vi.spyOn(api, 'getDecisionsProvider').mockResolvedValue(data)
  renderSection()
  await screen.findByRole('radiogroup', { name: 'Decision model' })
  await waitFor(() => expect(decisionsSwitch()).toBeInTheDocument())
}

describe('the Decision models card names no provider outside its entry', () => {
  it('with every model allowed and the hosted provider chosen, its key field open', async () => {
    await renderCard(provider())
    const entry = document.querySelector(HOSTED_ENTRY) as HTMLElement
    // The hosted provider keeps its own name, and its API key sits inside its entry.
    expect(entry.textContent).toContain('Jev, hosted by TypeSafe')
    expect(entry.querySelector('[data-testid="decisions-hosted-key"]')).not.toBeNull()
    await waitFor(() => expect(screen.getByText('TypeSafe API key')).toBeInTheDocument())
    expect(namesOutsideTheHostedEntry()).toEqual([])
  })

  it('with an allow list of one model', async () => {
    await renderCard(
      provider({
        active: 'none',
        configured_endpoint: ENDPOINT,
        hosted_permitted: false,
        provider_permitted: { jev: false, laya: true, sagemaker: false, custom: false },
      }),
      consentOf(false),
    )
    // Each refused model is drawn, greyed, with the administrator's wording.
    expect(screen.getByRole('radio', { name: 'Laya' })).not.toBeDisabled()
    expect(screen.getByRole('radio', { name: 'Your SageMaker endpoint' })).toBeDisabled()
    expect(screen.getByRole('radio', { name: /hosted by TypeSafe/ })).toBeDisabled()
    expect(screen.getAllByText('Not allowed by your administrator.').length).toBeGreaterThanOrEqual(2)
    expect(namesOutsideTheHostedEntry()).toEqual([])
  })

  it('with every model refused by the allow list', async () => {
    await renderCard(
      provider({
        active: 'none',
        configured_endpoint: 'none',
        hosted_permitted: false,
        provider_permitted: { jev: false, laya: false, sagemaker: false, custom: false },
      }),
      consentOf(false),
    )
    expect(screen.getByTestId('decisions-all-models-blocked').textContent).toMatch(
      /administrator allows none of these decision models/,
    )
    expect(screen.getByRole('radio', { name: 'No model' })).not.toBeDisabled()
    expect(namesOutsideTheHostedEntry()).toEqual([])
  })

  it('with a SageMaker endpoint answering', async () => {
    await renderCard(
      provider({
        active: 'sagemaker',
        configured_endpoint: SAGEMAKER,
        sagemaker: { endpoint_name: 'decision-model-a', region: 'us-west-2', profile: '' },
      }),
    )
    expect(screen.getByRole('radio', { name: 'Your SageMaker endpoint' })).toBeChecked()
    expect(namesOutsideTheHostedEntry()).toEqual([])
  })

  it('with a local model answering', async () => {
    await renderCard(
      provider({
        active: 'laya',
        configured_endpoint: 'http://127.0.0.1:8104/v1/systemone',
        loopback: true,
        runtime: { preset: 'laya', state: 'running', port: 8104, bytes_done: 0, bytes_total: 0, error: '' },
      }),
    )
    expect(screen.getByRole('radio', { name: 'Laya' })).toBeChecked()
    expect(namesOutsideTheHostedEntry()).toEqual([])
  })

  it('turned off by the administrator: a plain notice and no control', async () => {
    stubGateway(consentOf(true), { decisions: { bucket: 100 } }, { decisions_enabled: false })
    renderSection()
    const notice = await screen.findByTestId('decisions-admin-disabled')
    expect(notice).toHaveAttribute('role', 'status')
    expect(notice.textContent).toContain('Decision models')
    expect(notice.textContent).toContain('Your administrator has turned off decision models')
    expect(screen.queryByRole('switch', { name: 'Decision models' })).toBeNull()
    expect(screen.queryByRole('radiogroup', { name: 'Decision model' })).toBeNull()
    expect(namesOutsideTheHostedEntry()).toEqual([])
  })

  it('the scanner itself reports a name outside the entry', () => {
    // A check that can never fail guards nothing: a stray name is reported.
    const host = document.createElement('div')
    host.innerHTML = '<p>Share of sessions Jev answers for</p><button aria-label="Ask Jev"></button>'
    document.body.appendChild(host)
    expect(namesOutsideTheHostedEntry(host)).toEqual([
      'text: Share of sessions Jev answers for',
      'aria-label: Ask Jev',
    ])
    host.remove()
  })
})

describe('the transcript decision rows name the feature, whichever provider is active', () => {
  const STEER = {
    turn_id: 't-steer-1',
    point: 'message.steer',
    choice: 'queue',
    baseline: 'steer',
    p: 0.65,
    latency_ms: 315,
  }
  const SKILLS = {
    turn_id: 't-1',
    point: 'skills.select',
    baseline: ['brazil', 'crux-code-reviews'],
    jev: ['brazil'],
    p: 0.81,
    tokens_saved: 3200,
    candidates: 42,
    message_chars: 96,
    history_chars: 1800,
    latency_ms: 197,
    dropped: [],
    error: null,
  }

  for (const [label, active] of [
    ['a SageMaker endpoint', provider({ active: 'sagemaker', configured_endpoint: SAGEMAKER })],
    ['the hosted provider', provider()],
    ['a local model', provider({ active: 'laya' })],
  ] as const) {
    it(`draws no provider name while ${label} is active`, () => {
      queryClient.setQueryData(['decisionsProvider'], active)
      render(
        <div>
          <SteerDecisionLine record={readSteerRecord(STEER)!} />
          <SteerDecisionLine record={readSteerRecord({ ...STEER, choice: 'steer' })!} />
          <DecisionStrip record={readDecisionStrip(SKILLS)!} />
        </div>,
      )
      expect(screen.getAllByTestId('steer-decision-choice')[0].textContent).toBe(
        'Decision model chose: run after this turn',
      )
      expect(namesOutsideTheHostedEntry()).toEqual([])
      // Nothing on these rows is the hosted provider's entry, so nothing may name it.
      expect(document.body.textContent).not.toContain('Jev')
    })
  }
})
