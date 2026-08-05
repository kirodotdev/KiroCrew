/**
 * ContextBar tooltip is percentage-only (absolute token counts live in the
 * click popover, not the hover tooltip). The fill color shifts to warn (>=75%)
 * / danger (>=90%).
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import ContextBar, { fmtTokens, composeContextReadout } from '../components/ContextBar'

describe('ContextBar', () => {
  it('shows the rounded percentage in the tooltip', () => {
    const { container } = render(<ContextBar pct={44} />)
    expect(container.querySelector('span')?.getAttribute('title')).toBe('Context: 44%')
  })

  it('clamps the percentage at 100', () => {
    const { container } = render(<ContextBar pct={140} />)
    expect(container.querySelector('span')?.getAttribute('title')).toBe('Context: 100%')
  })

  it('uses accent fill below 75%', () => {
    const { container } = render(<ContextBar pct={50} />)
    const rects = container.querySelectorAll('rect')
    expect(rects[1].getAttribute('fill')).toBe('var(--accent)')
  })

  it('uses warn fill at 75–89%', () => {
    const { container } = render(<ContextBar pct={80} />)
    const rects = container.querySelectorAll('rect')
    expect(rects[1].getAttribute('fill')).toBe('var(--warn)')
  })

  it('uses danger fill at 90%+', () => {
    const { container } = render(<ContextBar pct={95} />)
    const rects = container.querySelectorAll('rect')
    expect(rects[1].getAttribute('fill')).toBe('var(--danger)')
  })
})

describe('fmtTokens', () => {
  it('renders sub-1000 counts without a suffix', () => {
    expect(fmtTokens(0)).toBe('0')
    expect(fmtTokens(512)).toBe('512')
  })
  it('uses compact K notation for thousands', () => {
    expect(fmtTokens(96000)).toBe('96K')
    expect(fmtTokens(200000)).toBe('200K')
  })
  it('rolls up to M at a million (locale-aware compact)', () => {
    expect(fmtTokens(1000000)).toBe('1M')
  })
  it('renders non-finite / negative input as zero', () => {
    expect(fmtTokens(NaN)).toBe('0')
    expect(fmtTokens(-5)).toBe('0')
  })
})

describe('composeContextReadout', () => {
  const pct = 48, used = 96000, total = 200000

  it('renders both segments joined by " · " when both toggles are on', () => {
    expect(composeContextReadout(pct, used, total, { showPct: true, showTokens: true })).toBe('48% · 96K/200K')
  })

  it('renders the percentage alone when only showPct is on', () => {
    expect(composeContextReadout(pct, used, total, { showPct: true })).toBe('48%')
  })

  it('renders token usage alone when only showTokens is on', () => {
    expect(composeContextReadout(pct, used, total, { showTokens: true })).toBe('96K/200K')
  })

  it('renders an empty string when neither toggle is on', () => {
    expect(composeContextReadout(pct, used, total, {})).toBe('')
  })

  it('clamps the percentage to 0–100', () => {
    expect(composeContextReadout(140, used, total, { showPct: true })).toBe('100%')
  })

  it('prefixes ~ on token usage when approximate', () => {
    expect(composeContextReadout(pct, used, total, { showTokens: true, approx: true })).toBe('~96K/200K')
  })
})
