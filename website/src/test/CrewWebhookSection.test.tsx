/**
 * The crew editor's webhook pane answers one crew-shaped question — "can an
 * outside system wake THIS crew, and with what credential" — from the global
 * token store. The load-bearing cases are the honesty rules:
 *
 *  - a fetch failure must not render as "nothing can wake this crew";
 *  - an UNBOUND token can name any crew per request, so its existence belongs
 *    in this pane's answer even though it is not in this crew's list;
 *  - the kill switch silences bound tokens too, so a live-looking list without
 *    that line would overstate what can actually call in.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, renderHook } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import CrewWebhookSection from '../components/CrewWebhookSection'
import { useCrewEditorSections, type CrewEditorFacts } from '../components/crew/crewEditorSections'

const mockApi = vi.hoisted(() => ({ webhooks: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))

function token(over: Record<string, unknown> = {}) {
  return {
    id: 'wht_' + Math.random().toString(16).slice(2, 8),
    label: 'CI runner',
    display_prefix: 'kc_whk_4f2b',
    last4: '9c1d',
    created_at: 1_700_000_000,
    last_used_at: null,
    require_signature: false,
    legacy: false,
    agent: '',
    enabled: true,
    ...over,
  }
}

function view(tokens: unknown[], over: Record<string, unknown> = {}) {
  return { enabled: true, switch_on: true, has_tokens: tokens.length > 0, tokens, ...over }
}

function renderPane(crew = 'oncall') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <CrewWebhookSection crew={crew} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  mockApi.webhooks.mockReset()
})

describe('crew webhook pane — lists only this crew\'s bindings', () => {
  it('shows bound tokens and omits ones bound elsewhere', async () => {
    mockApi.webhooks.mockResolvedValue(view([
      token({ label: 'CI runner', agent: 'oncall' }),
      token({ label: 'Deploy bot', agent: 'deploy' }),
    ]))
    renderPane('oncall')
    await waitFor(() => expect(screen.getByText('CI runner')).toBeInTheDocument())
    expect(screen.queryByText('Deploy bot')).not.toBeInTheDocument()
    expect(screen.getAllByTestId('crew-webhook-row')).toHaveLength(1)
  })

  it('shows the non-secret slice and the signing badge', async () => {
    mockApi.webhooks.mockResolvedValue(view([
      token({ agent: 'oncall', require_signature: true }),
    ]))
    renderPane('oncall')
    await waitFor(() => expect(screen.getByText(/kc_whk_4f2b…9c1d/)).toBeInTheDocument())
    expect(screen.getByText('Signed')).toBeInTheDocument()
    expect(screen.getByText(/never used/)).toBeInTheDocument()
  })

  it('states the empty case in words when nothing is bound', async () => {
    mockApi.webhooks.mockResolvedValue(view([]))
    renderPane('oncall')
    await waitFor(() => expect(screen.getByTestId('crew-webhook-empty')).toBeInTheDocument())
  })
})

describe('crew webhook pane — honesty rules', () => {
  it('reports a failed fetch as unknown, never as "nothing wakes this crew"', async () => {
    mockApi.webhooks.mockRejectedValue(new Error('boom'))
    renderPane('oncall')
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
    expect(screen.queryByTestId('crew-webhook-empty')).not.toBeInTheDocument()
  })

  it('discloses unbound tokens beside the empty state — they can wake this crew too', async () => {
    mockApi.webhooks.mockResolvedValue(view([token({ agent: '' })]))
    renderPane('oncall')
    await waitFor(() => expect(screen.getByTestId('crew-webhook-empty')).toBeInTheDocument())
    expect(screen.getByTestId('crew-webhook-unbound-note').textContent).toContain('1 token')
    // Not listed as a row: it is not bound to this crew, it merely can reach it.
    expect(screen.queryAllByTestId('crew-webhook-row')).toHaveLength(0)
  })

  it('says so when the kill switch silences everything, bound tokens included', async () => {
    mockApi.webhooks.mockResolvedValue(view(
      [token({ agent: 'oncall' })], { switch_on: false },
    ))
    renderPane('oncall')
    await waitFor(() => expect(screen.getByTestId('crew-webhook-switch-off')).toBeInTheDocument())
    // The list still renders — the tokens exist — the line qualifies them, and
    // the rows dim the same way a per-token Off does, so a user scanning for
    // why their webhook died does not leave reassured by live-looking rows.
    const row = screen.getByTestId('crew-webhook-row')
    expect(row.className).toContain('opacity-60')
    // But no per-row Off badge: the token's own switch is on, and the pane-level
    // notice already carries the global cause once instead of once per row.
    expect(screen.queryByTestId('crew-webhook-row-off')).not.toBeInTheDocument()
  })
})

describe('crew webhook pane — a disabled binding is visible but not live', () => {
  it('lists it dimmed with an Off badge instead of hiding it', async () => {
    // Hiding it would send a user wondering why their webhook stopped firing
    // off to mint a duplicate; showing it live would overstate the wake surface.
    mockApi.webhooks.mockResolvedValue(view([
      token({ label: 'CI runner', agent: 'oncall', enabled: false }),
    ]))
    renderPane('oncall')
    await waitFor(() => expect(screen.getByText('CI runner')).toBeInTheDocument())
    expect(screen.getByTestId('crew-webhook-row-off')).toBeInTheDocument()
  })

  it('excludes a disabled unbound token from the any-crew disclosure', async () => {
    mockApi.webhooks.mockResolvedValue(view([token({ agent: '', enabled: false })]))
    renderPane('oncall')
    await waitFor(() => expect(screen.getByTestId('crew-webhook-empty')).toBeInTheDocument())
    expect(screen.queryByTestId('crew-webhook-unbound-note')).not.toBeInTheDocument()
  })

  it('mutes the any-crew disclosure while the kill switch is off', async () => {
    // "Nothing can call in" and "this token can wake any crew" cannot both be
    // present-tense true on one screen; the disclosure yields to the switch.
    mockApi.webhooks.mockResolvedValue(view(
      [token({ agent: '' })], { switch_on: false },
    ))
    renderPane('oncall')
    await waitFor(() => expect(screen.getByTestId('crew-webhook-switch-off')).toBeInTheDocument())
    expect(screen.queryByTestId('crew-webhook-unbound-note')).not.toBeInTheDocument()
  })
})

describe('crew editor rail — the webhook row is a real pane row', () => {
  const facts = (over: Partial<CrewEditorFacts> = {}): CrewEditorFacts => ({
    templateLabel: 'Agent Template', activeSchedules: 1, totalSchedules: 2, routingWords: 0,
    sharesStorage: false, canDelete: true, webhookTokens: 0, webhookTokensActive: 0,
    dirtyPanes: new Set(),
    ...over,
  })
  const webhookRow = (f: CrewEditorFacts) => {
    const { result } = renderHook(() => useCrewEditorSections(f))
    return result.current.find(r => r.key === 'webhook')!
  }

  it('is enabled and counts live over total, the Schedules row\'s language', () => {
    const row = webhookRow(facts({ webhookTokens: 2, webhookTokensActive: 2 }))
    expect(row.disabled).toBeUndefined()
    expect(row.count).toBe('2/2')
  })

  it('shows a disabled binding as a shortfall, not as a live wake path', () => {
    expect(webhookRow(facts({ webhookTokens: 2, webhookTokensActive: 1 })).count).toBe('1/2')
  })

  it('carries no count at zero and none when the store is unreadable', () => {
    // Zero and unknown must both stay blank, but for different reasons: a "0"
    // badge beside Schedules' "2/3" implies a ratio, and an unreadable store is
    // not an answer at all.
    expect(webhookRow(facts()).count).toBeUndefined()
    expect(webhookRow(facts({ webhookTokens: 3, webhookTokensActive: 3, webhooksUnknown: true })).count).toBeUndefined()
  })

  it('recomputes when the token count lands after the first render', () => {
    // The registry memo lists its dependencies explicitly; a fact missing from
    // that list freezes the row at the value of the first render, which is
    // exactly what the async webhooks query would hit. `dirtyPanes` must be the
    // SAME Set across both renders — a fresh identity would recompute the memo
    // for the wrong reason and hide a missing dependency.
    const dirtyPanes: CrewEditorFacts['dirtyPanes'] = new Set()
    const { result, rerender } = renderHook((f: CrewEditorFacts) => useCrewEditorSections(f), {
      initialProps: facts({ webhookTokens: 0, dirtyPanes }),
    })
    expect(result.current.find(r => r.key === 'webhook')!.count).toBeUndefined()
    rerender(facts({ webhookTokens: 2, webhookTokensActive: 2, dirtyPanes }))
    expect(result.current.find(r => r.key === 'webhook')!.count).toBe('2/2')
  })
})
