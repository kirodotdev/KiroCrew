/**
 * The mid-turn handling receipt, and the composer mode that produces it.
 *
 * Three things are pinned, each a way this surface could lie:
 *
 *  - the READER. Every field arrives over the wire, and `choice` IS the claim, so
 *    a record that does not name one of the two shipped paths draws nothing rather
 *    than a line about a branch that does not exist.
 *  - the USER ROW. A message whose handling nobody decided carries no record, which
 *    is every send on a default install, so the absent-field path is the common
 *    path: the row must render exactly as it does today.
 *  - the MODE. `Auto (Jev)` is offered only while the host says the seam is
 *    available, and a stored `auto` resolves back to Steer while it is not --
 *    otherwise a withdrawn consent leaves a mode on screen whose send the gateway
 *    refuses to decide.
 */
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

import BusySendButton, { readBusySendMode, BUSY_SEND_MODE_LS_KEY } from '../components/BusySendButton'
import { queryClient } from '../api/queryClient'
import SteerDecisionLine from '../pages/chat/SteerDecisionLine'
import UserMessage from '../pages/chat/UserMessage'
import { __resetVerdicts, readSteerRecord } from '../pages/chat/decisionRecord'

/** A record as the gateway stamps it, for a message Jev chose to queue. */
const WIRE = {
  turn_id: 't-steer-1',
  ts: '2026-09-20T07:00:00Z',
  point: 'message.steer',
  choice: 'queue',
  baseline: 'steer',
  p: 0.83,
  latency_ms: 190,
  session: 'abcd',
  scrubbed: false,
}

beforeEach(() => {
  __resetVerdicts()
  queryClient.clear()
  localStorage.clear()
})

afterEach(() => {
  cleanup()
  queryClient.clear()
  localStorage.clear()
  vi.restoreAllMocks()
})

describe('readSteerRecord', () => {
  it('reads a full record and keeps every measurement', () => {
    expect(readSteerRecord(WIRE)).toEqual({
      turnId: 't-steer-1',
      point: 'message.steer',
      choice: 'queue',
      p: 0.83,
      latencyMs: 190,
    })
  })

  it('does not keep the baseline arm, which no line prints', () => {
    // It is `steer` on every row, so a line printing it would print one word
    // forever. The logged row still carries it for the `jq` fold the docs name.
    expect(readSteerRecord(WIRE)).not.toHaveProperty('baseline')
  })

  it('ignores keys it does not know, so a producer may stamp more', () => {
    expect(readSteerRecord({ ...WIRE, turn_chars: 0, future: 'x' })?.choice).toBe('queue')
  })

  it('draws nothing for a choice outside the two shipped paths', () => {
    // A third value would name a code path that does not exist, unlike an unknown
    // tier or skill key, which name data this reader simply has not learned.
    expect(readSteerRecord({ ...WIRE, choice: 'interrupt' })).toBeNull()
    expect(readSteerRecord({ ...WIRE, choice: '' })).toBeNull()
    expect(readSteerRecord({ ...WIRE, choice: 7 })).toBeNull()
  })

  it('draws nothing without a turn id, which the verdict POST needs', () => {
    expect(readSteerRecord({ ...WIRE, turn_id: '' })).toBeNull()
  })

  it('draws nothing for another point’s record', () => {
    expect(readSteerRecord({ ...WIRE, point: 'skills.select' })).toBeNull()
  })

  it('reads a record with no point at all as this one is not', () => {
    // An absent point is the older producer's shape and belongs to the skill
    // reader; a steer line inferred from fields alone would be a guess.
    expect(readSteerRecord({ turn_id: 't', choice: 'queue' })?.choice).toBe('queue')
  })

  it('prints no score for a number that is not a probability', () => {
    expect(readSteerRecord({ ...WIRE, p: 1.4 })?.p).toBeNull()
    expect(readSteerRecord({ ...WIRE, p: 'high' })?.p).toBeNull()
  })

  it('floors an unparsable latency to zero, which draws no latency', () => {
    expect(readSteerRecord({ ...WIRE, latency_ms: -5 })?.latencyMs).toBe(0)
    expect(readSteerRecord({ ...WIRE, latency_ms: null })?.latencyMs).toBe(0)
  })

  it('reads a record that states no baseline at all', () => {
    const { baseline: _unused, ...withoutBaseline } = WIRE
    expect(readSteerRecord(withoutBaseline)?.choice).toBe('queue')
  })
})

describe('the line', () => {
  const record = readSteerRecord(WIRE)!

  it('says what Jev CHOSE, with the score and the latency', () => {
    render(<SteerDecisionLine record={record} />)
    const line = screen.getByTestId('steer-decision-line')
    expect(line.textContent).toContain('chose')
    expect(screen.getByTestId('steer-decision-scores').textContent).toContain('0.83')
    expect(screen.getByTestId('steer-decision-scores').textContent).toContain('190')
  })

  it('names the score instead of printing a bare number', () => {
    // A hover title is unreachable on touch and silent to a screen reader, so the
    // only explanation of "0.83" has to be on the line.
    render(<SteerDecisionLine record={record} />)
    expect(screen.getByTestId('steer-decision-scores').textContent).toMatch(/confidence/i)
  })

  it('says the other choice for a steer answer', () => {
    render(<SteerDecisionLine record={readSteerRecord({ ...WIRE, choice: 'steer' })!} />)
    const text = screen.getByTestId('steer-decision-line').textContent ?? ''
    expect(text).toContain('interrupt')
    expect(text).not.toContain('after this turn')
  })

  it('describes the CHOICE, never the delivery', () => {
    // A chosen steer whose live client is gone falls through to the queue, so a
    // line phrased as "steered the running turn" would report the opposite of what
    // happened on exactly that row.
    render(<SteerDecisionLine record={readSteerRecord({ ...WIRE, choice: 'steer' })!} />)
    const text = screen.getByTestId('steer-decision-line').textContent ?? ''
    expect(text).not.toMatch(/steered/i)
  })

  it('labels the thumbs as the choice, not as a second mention of Jev', () => {
    render(<SteerDecisionLine record={record} />)
    const label = screen.getByTestId('decision-strip-rate-label-jev').textContent ?? ''
    expect(label).not.toMatch(/^Jev$/)
    expect(label.toLowerCase()).toContain('choice')
  })

  it('prints no parenthetical when the record carries neither number', () => {
    render(
      <SteerDecisionLine record={readSteerRecord({ ...WIRE, p: null, latency_ms: 0 })!} />,
    )
    expect(screen.queryByTestId('steer-decision-scores')).toBeNull()
  })

  it('offers the Jev thumbs and no baseline pair', () => {
    // One decision by one party: what the product would have done instead is the
    // `baseline` field, and rating it would be rating a path this send did not take.
    render(<SteerDecisionLine record={record} />)
    expect(screen.getByTestId('decision-strip-right-jev')).toBeInTheDocument()
    expect(screen.queryByTestId('decision-strip-right-baseline')).toBeNull()
  })
})

describe('the user row', () => {
  const renderRow = (meta?: Record<string, unknown>) =>
    render(
      <UserMessage
        content="and bump the version"
        meta={meta}
        renderContent={(content) => <span>{content}</span>}
      />,
    )

  it('renders exactly as today when the row carries no decision record', () => {
    // The shipping state on every default install: nothing was decided, so there
    // is no receipt and the row must not grow an empty one.
    renderRow()
    expect(screen.queryByTestId('steer-decision-line')).toBeNull()
    expect(screen.getByText('and bump the version')).toBeInTheDocument()
  })

  it('draws the line once the row carries one', () => {
    renderRow({ decisions_strip: WIRE })
    expect(screen.getByTestId('steer-decision-line')).toBeInTheDocument()
  })

  it('draws the line on a row the steer badge also claims', () => {
    renderRow({ decisions_strip: { ...WIRE, choice: 'steer' }, steer: true, steerState: 'consumed' })
    expect(screen.getByTestId('steer-decision-line')).toBeInTheDocument()
  })

  it('draws no line for a record it cannot validate', () => {
    renderRow({ decisions_strip: { point: 'message.steer' } })
    expect(screen.queryByTestId('steer-decision-line')).toBeNull()
  })

  it('draws no line for the skill record that rides an assistant row', () => {
    renderRow({ decisions_strip: { turn_id: 't', point: 'skills.select', baseline: [], jev: [] } })
    expect(screen.queryByTestId('steer-decision-line')).toBeNull()
  })
})

describe('a chosen steer the running turn did not take', () => {
  // A steer the turn ended before reading runs as its own turn. "interrupt this
  // turn" on that row reads as a decision about the NEW turn, which nobody made.
  const STEER_RECEIPT = { ...WIRE, choice: 'steer' }
  const SAID_STEER = 'Jev chose: interrupt this turn'
  const SAID_SHORT = 'Jev chose to interrupt'
  const SAID_FULL = 'Jev chose to interrupt, but this runs as its own message'
  const SAID_QUEUE = 'Jev chose: run after this turn'
  const renderRow = (meta: Record<string, unknown>) =>
    render(
      <UserMessage
        content="and bump the version"
        meta={meta}
        renderContent={(content) => <span>{content}</span>}
      />,
    )
  const line = () => screen.getByTestId('steer-decision-line')

  it('says only who chose on a requeued steer row, without repeating its lifecycle line', () => {
    const { container } = renderRow({ decisions_strip: STEER_RECEIPT, steer: true, steerState: 'requeued' })
    expect(line().textContent).toContain(SAID_SHORT)
    expect(line().textContent).not.toContain('its own message')
    // An intent, not an outcome: a requeued steer may have been read before
    // its RPC died, so the receipt makes no claim about whether it applied.
    expect(line().textContent).not.toContain('not applied')
    // The requeued line says it once for the row.
    expect(container.textContent?.match(/its own message/g)).toHaveLength(1)
    expect(line().textContent).not.toContain(SAID_STEER)
    expect(line().getAttribute('data-choice')).toBe('steer')
    expect(line().getAttribute('data-ran-as-own-turn')).toBe('true')
  })

  it('wraps the own-message sentence so a narrow pane keeps its last clause', () => {
    renderRow({ decisions_strip: STEER_RECEIPT, steer: true, steerState: 'requeued' })
    const choice = screen.getByTestId('steer-decision-choice')
    expect(choice.className).not.toContain('truncate')
    expect(choice.className).toContain('break-words')
    expect(line().className).toContain('flex-wrap')
  })

  it('still truncates the plain steer sentence', () => {
    renderRow({ decisions_strip: STEER_RECEIPT, steer: true, steerState: 'consumed' })
    expect(screen.getByTestId('steer-decision-choice').className).toContain('truncate')
  })

  it('says only who chose on a superseded steer row, which keeps the requeued lifecycle line', () => {
    // The transcript hides a superseded row; a surface that still draws it shows
    // the requeued line, so the receipt does not repeat its outcome.
    const { container } = renderRow({ decisions_strip: STEER_RECEIPT, steer: true, steerState: 'superseded' })
    expect(line().textContent).toContain(SAID_SHORT)
    expect(line().textContent).not.toContain('its own message')
    expect(container.textContent?.match(/its own message/g)).toHaveLength(1)
    expect(line().getAttribute('data-ran-as-own-turn')).toBe('true')
  })

  it('says so on a plain user row that carries the receipt', () => {
    // The row the drain writes for a requeued steer, and the queued row of a steer
    // that was unavailable: neither has `meta.steer`, so no turn took either.
    renderRow({ decisions_strip: STEER_RECEIPT })
    expect(line().textContent).toContain(SAID_FULL)
    expect(line().getAttribute('data-choice')).toBe('steer')
    expect(line().getAttribute('data-ran-as-own-turn')).toBe('true')
  })

  it('keeps "interrupt this turn" on a steer the turn has or may still take', () => {
    for (const meta of [
      { steer: true, steerState: 'written' },
      { steer: true, steerState: 'consumed' },
      { steer: true },
    ]) {
      renderRow({ decisions_strip: STEER_RECEIPT, ...meta })
      expect(line().textContent).toContain(SAID_STEER)
      expect(line().hasAttribute('data-ran-as-own-turn')).toBe(false)
      cleanup()
    }
  })

  it('leaves a queue receipt as it is on every row', () => {
    for (const meta of [
      {},
      { steer: true, steerState: 'requeued' },
      { steer: true, steerState: 'consumed' },
    ]) {
      renderRow({ decisions_strip: WIRE, ...meta })
      expect(line().textContent).toContain(SAID_QUEUE)
      expect(line().getAttribute('data-choice')).toBe('queue')
      expect(line().hasAttribute('data-ran-as-own-turn')).toBe(false)
      cleanup()
    }
  })

  it('ignores the flag for a queue choice on the line itself', () => {
    render(<SteerDecisionLine record={readSteerRecord(WIRE)!} ownMessage="full" />)
    expect(line().textContent).toContain(SAID_QUEUE)
    expect(line().hasAttribute('data-ran-as-own-turn')).toBe(false)
  })
})

describe('the split send button’s third mode', () => {
  it('offers Auto only when the host says the seam is available', () => {
    const { rerender } = render(
      <BusySendButton mode="steer" onModeChange={() => {}} onFire={() => {}} />,
    )
    fireEvent.click(screen.getByTestId('busy-send-caret'))
    expect(screen.queryByTestId('busy-send-mode-auto')).toBeNull()
    expect(screen.getByTestId('busy-send-mode-steer')).toBeInTheDocument()
    expect(screen.getByTestId('busy-send-mode-queue')).toBeInTheDocument()

    rerender(
      <BusySendButton mode="steer" onModeChange={() => {}} onFire={() => {}} autoAvailable />,
    )
    expect(screen.getByTestId('busy-send-mode-auto')).toBeInTheDocument()
  })

  it('fires the selected mode and reports it on the control', () => {
    const onFire = vi.fn()
    render(
      <BusySendButton mode="auto" onModeChange={() => {}} onFire={onFire} autoAvailable />,
    )
    const fire = screen.getByTestId('busy-send-button')
    expect(fire.getAttribute('data-mode')).toBe('auto')
    expect(fire.getAttribute('aria-label')).toContain('Jev')
    fireEvent.click(fire)
    expect(onFire).toHaveBeenCalledTimes(1)
  })

  it('reads a stored auto back, and anything it does not know as steer', () => {
    localStorage.setItem(`${BUSY_SEND_MODE_LS_KEY}:chat-1`, 'auto')
    expect(readBusySendMode('chat-1')).toBe('auto')
    localStorage.setItem(`${BUSY_SEND_MODE_LS_KEY}:chat-1`, 'consult-the-oracle')
    expect(readBusySendMode('chat-1')).toBe('steer')
  })
})
