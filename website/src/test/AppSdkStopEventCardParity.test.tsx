import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { ChatMessage } from '../types'
import StopEventCard from '../pages/chat/StopEventCard'
import { defaultMessageRenderers, resolveRenderer } from '../app-sdk/messageRenderers'

/**
 * Parity pin for issue #6229: the app-sdk `stop_event` registry entry must render
 * the SHARED StopEventCard rather than a hand-rolled div carrying a byte-for-byte
 * copy of its stopping/stopped recipe. Sibling of the #6209 ErrorCard pin.
 *
 * Unlike #6209 the pre-fix row was NOT visually equivalent, and that is the
 * substance of this file. A stop row's `content` is the card's own JSON envelope
 * — the gateway writes `{"kind":"stop_event","id":…,"state":…,"outcome":…}` into
 * `cls` and mirrors it into `content` for consumers that read only `content`
 * (dashboard/chat_handlers.py) — so the retired div, which rendered `content`
 * verbatim, printed that envelope into the transcript. What is pinned:
 *
 *  1. the row exposes `data-testid="stop-event-card"`, which only StopEventCard
 *     emits (the hand-rolled div had no testid → red before the fix);
 *  2. its class list matches StopEventCard's own branch for the same state, so
 *     the two surfaces cannot drift apart;
 *  3. the envelope never reaches the transcript — the regression the retired div
 *     shipped;
 *  4. the three states read differently, where the retired div was one static
 *     row for all of them.
 *
 * StopEventCard is deliberately NOT mocked: rendering the real component is the
 * point.
 */

/** Exactly what the gateway writes: envelope in `cls`, mirrored into `content`,
 *  `meta` parsed back off `cls` by the wire layer (dashboard/state.py). */
function gatewayStopRow(state: string): ChatMessage {
  const data = {
    kind: 'stop_event',
    id: 'stop-zzqparity',
    state,
    outcome: null,
    ts_start: '2026-01-01T00:00:00+00:00',
  }
  const json = JSON.stringify(data)
  return { role: 'system', content: json, cls: json, meta: data } as unknown as ChatMessage
}

/** Render whatever the registry resolves for `m`, with the list's layout callbacks. */
function renderStopRow(m: ChatMessage) {
  const entry = resolveRenderer(m, defaultMessageRenderers)
  expect(entry?.id).toBe('stop_event')
  const node = entry!.render(m, {
    index: 0,
    messages: [m],
    running: false,
    key: 'zzq-key',
    onFileOpen: () => {},
    hideCardOwnedOAuth: false,
    autoDeniedIds: new Set<string>(),
    wrapper: children => <div data-testid="wrapper">{children}</div>,
    row: children => <div data-testid="row">{children}</div>,
  })
  return render(<>{node}</>)
}

describe('app-sdk stop_event entry — StopEventCard parity (#6229)', () => {
  it('renders the shared StopEventCard, not a hand-rolled div', () => {
    renderStopRow(gatewayStopRow('stopping'))
    // Present only via StopEventCard — the duplicated div carried no testid.
    const card = screen.getByTestId('stop-event-card')
    expect(screen.getByTestId('row')).toContainElement(card)
    expect(card.getAttribute('data-state')).toBe('stopping')
  })

  it("matches StopEventCard's own class recipe for every state", () => {
    // Identity with the component is the whole pin: any restyle of a branch flows
    // through both sides, so the recipe cannot drift. `stop_failed_reset` is
    // included because it is the branch that is NOT byte-identical to the other
    // two (it adds a ring), which a copied class string would silently flatten.
    for (const state of ['stopping', 'stopped', 'stop_failed_reset']) {
      const m = gatewayStopRow(state)
      const registry = renderStopRow(m)
      const viaRegistry = registry.getByTestId('stop-event-card').className
      registry.unmount()

      const reference = render(<StopEventCard message={m} />)
      const direct = reference.getByTestId('stop-event-card').className
      reference.unmount()

      expect(viaRegistry).toBe(direct)
    }
  })

  it('keeps the stop envelope out of the transcript', () => {
    // The regression the retired div shipped: `content` IS the envelope, so it
    // rendered raw JSON where the dashboard rendered the state.
    renderStopRow(gatewayStopRow('stopping'))
    const row = screen.getByTestId('row')
    expect(row).not.toHaveTextContent('"kind":"stop_event"')
    expect(row).not.toHaveTextContent('stop-zzqparity')
    expect(row).not.toHaveTextContent('ts_start')
  })

  it('reads each state differently instead of drawing one static row', () => {
    const seen = new Set<string>()
    for (const state of ['stopping', 'stopped', 'stop_failed_reset']) {
      const v = renderStopRow(gatewayStopRow(state))
      seen.add(v.getByTestId('stop-event-card').textContent ?? '')
      v.unmount()
    }
    expect(seen.size).toBe(3)
  })
})
