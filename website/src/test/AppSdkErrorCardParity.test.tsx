import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { ChatMessage } from '../types'
import { ErrorCard } from '../pages/chat/ErrorCard'
import { defaultMessageRenderers, resolveRenderer } from '../app-sdk/messageRenderers'

/**
 * Parity pin for issue #6209: the app-sdk `error` registry entry must render the
 * SHARED ErrorCard rather than a hand-rolled div carrying a byte-for-byte copy of
 * its recipe. The pre-fix render was already visually identical, so a snapshot
 * would pass on unfixed code and prove nothing — instead this pins what the change
 * actually guarantees:
 *
 *  1. the row exposes `data-testid="error-card"`, which only ErrorCard emits
 *     (the hand-rolled div had no testid → genuinely red before the fix);
 *  2. its class list matches ErrorCard's non-continuable branch exactly, so the
 *     two surfaces can no longer drift apart (also red before the fix — the
 *     testid query throws on the retired div);
 *  3. no continue affordance leaks in — the app-sdk surface has no turn to
 *     resume, so a later `onContinue` regression must fail here. Unlike 1-2
 *     this is a FORWARD guard, not a #6209 regression pin: it passes on the
 *     retired div too;
 *  4. hostile content renders as escaped text (both the retired div and
 *     ErrorCard render `content` as a JSX text child — this writes the
 *     no-innerHTML guarantee down; also a forward guard).
 *
 * ErrorCard is deliberately NOT mocked: rendering the real component is the point.
 */

function msg(over: Partial<ChatMessage> = {}): ChatMessage {
  return { role: 'error', content: 'zzq-parity boom', ...over } as ChatMessage
}

/** Render whatever the registry resolves for `m`, with the list's layout callbacks. */
function renderErrorRow(m: ChatMessage) {
  const entry = resolveRenderer(m, defaultMessageRenderers)
  expect(entry?.id).toBe('error')
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

describe('app-sdk error entry — ErrorCard parity (#6209)', () => {
  it('renders the shared ErrorCard, not a hand-rolled div', () => {
    renderErrorRow(msg())
    // Present only via ErrorCard — the duplicated div carried no testid.
    const card = screen.getByTestId('error-card')
    expect(card).toHaveTextContent('zzq-parity boom')
  })

  it("matches ErrorCard's non-continuable class recipe exactly", () => {
    const registry = renderErrorRow(msg())
    const viaRegistry = registry.getByTestId('error-card').className
    registry.unmount()

    const reference = render(<ErrorCard content="zzq-reference" />)
    const direct = reference.getByTestId('error-card').className

    // Identity with the component is the whole pin: any restyle of ErrorCard's
    // settled branch flows through both sides, so parity cannot drift.
    expect(viaRegistry).toBe(direct)
  })

  it('offers no continue affordance on the app-sdk surface', () => {
    renderErrorRow(msg())
    expect(screen.queryByTestId('error-card-continue')).toBeNull()
    expect(screen.getByTestId('error-card')).not.toHaveAttribute('data-continuable')
  })

  it('renders hostile content as text, never as markup', () => {
    // Both the retired div and ErrorCard render `content` as a JSX text child;
    // this writes that guarantee down so a move to innerHTML cannot slip by.
    renderErrorRow(msg({ content: '<img src=x onerror="window.zzqPwned=1">' }))
    const card = screen.getByTestId('error-card')
    expect(card.querySelector('img')).toBeNull()
    expect(card).toHaveTextContent('<img src=x onerror="window.zzqPwned=1">')
  })
})
