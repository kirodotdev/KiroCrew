/**
 * Credit-multiplier badge in the model picker.
 *
 * The rules worth guarding are all about what happens when the number is NOT
 * there. kiro re-prices models (GPT-5.6 Luna moved 0.6x -> 0.1x in 2026-07), so
 * an invented multiplier is not a harmless default — it is a wrong price shown
 * with the same confidence as a right one. Every "absent" path below must render
 * no badge rather than fall back to 1x.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'

import { i18next } from '../i18n'
import ModelDropdownList, { formatMultiplier, costTier } from '../components/ModelDropdownList'
import { withAutoFirst } from '../providers/modelList'

/** The badge for `name`, or null when that row rendered none. */
function badgeFor(name: string): HTMLElement | null {
  const row = screen.getByText(name).closest('[role="option"]')!
  // The visible glyph is aria-hidden; find it by the mono badge class it carries.
  return row.querySelector('span.rounded-full')
}

describe('formatMultiplier — matches how kiro spells its own rates', () => {
  it.each([
    [1, '1.0x'],
    [1.3, '1.3x'],
    [2.2, '2.2x'],
    [0.25, '0.25x'],
    [0.05, '0.05x'],
    [4.4, '4.4x'],
  ])('%s renders as %s', (input, expected) => {
    expect(formatMultiplier(input)).toBe(expected)
  })

  it('rounds float noise away instead of printing it', () => {
    expect(formatMultiplier(0.1 + 0.2)).toBe('0.3x')
  })

  it.each([
    [0.005, '0.01x'],
    [0.004, '0.004x'],
    [0.001, '0.001x'],
    [0.0001, '0.0001x'],
  ])('never rounds %s down to a free-looking 0x (renders %s)', (input, expected) => {
    // toFixed(2) alone turns anything under 0.005 into "0.00" -> "0x", which
    // reads as "this model is free" — the exact misreading isPricedMultiplier
    // rejects a literal 0 to avoid. Today's floor is 0.01x, but the reason this
    // change reads the rate live at all is that kiro re-prices models.
    expect(formatMultiplier(input)).toBe(expected)
  })

  it('always shows at least one decimal, matching how kiro publishes rates', () => {
    // A column mixing "1x" and "2.2x" reads as two different kinds of number
    // and gives the pills different widths.
    expect(formatMultiplier(1)).toBe('1.0x')
    expect(formatMultiplier(2)).toBe('2.0x')
  })

  // The digits land inside translated sentences, so they must come from the
  // locale seam (src/i18n/format.ts), not toFixed. `website/AGENTS.md` makes this
  // mandatory and docs/i18n-catalog.md names toFixed as the case no source scan
  // can detect: "Latin digits are wrong for bn".
  describe('locale-aware digits', () => {
    afterEach(async () => { await i18next.changeLanguage('en') })

    it('renders Bengali numerals under bn instead of Latin ones', async () => {
      await i18next.changeLanguage('bn')
      const out = formatMultiplier(2.2)
      // Bengali digits are U+09E6..U+09EF. toFixed can never produce them.
      expect(out).toMatch(/[\u09e6-\u09ef]/)
      expect(out).not.toMatch(/[0-9]/)
      expect(out.endsWith('x')).toBe(true)
    })

    it('keeps the sub-0.005 floor non-zero in a non-Latin locale too', async () => {
      await i18next.changeLanguage('bn')
      const out = formatMultiplier(0.004)
      expect(out).toMatch(/[\u09e6-\u09ef]/)
      // Assert the PROPERTY (the rendered rate is not zero), not one regression
      // string: `not.toBe('০x')` would still pass on a slip that rendered
      // `০.০x`. Map the Bengali digits back to Latin and check the value.
      const latin = out.replace(/[\u09e6-\u09ef]/g, d => String(d.charCodeAt(0) - 0x09e6))
      expect(Number.parseFloat(latin)).toBeGreaterThan(0)
    })

    it('uses the locale decimal separator under de', async () => {
      await i18next.changeLanguage('de')
      expect(formatMultiplier(2.2)).toBe('2,2x')
    })
  })
})

describe('costTier — brackets the default user’s own multipliers as "standard"', () => {
  it.each([
    [0.01, 'budget'], [0.05, 'budget'], [0.5, 'budget'],
    [1, 'standard'], [1.3, 'standard'], [1.5, 'standard'],
    [1.6, 'premium'], [2.2, 'premium'], [4.4, 'premium'],
  ])('%s is %s', (value, tier) => {
    expect(costTier(value as number)).toBe(tier)
  })
})

describe('ModelDropdownList — badge rendering', () => {
  it('shows the multiplier the backend reported', () => {
    render(<ModelDropdownList
      models={[{ name: 'claude-opus-5', rateMultiplier: 2.2 }]}
      activeModel="" onSelect={vi.fn()} />)
    expect(badgeFor('claude-opus-5')).toHaveTextContent('2.2x')
  })

  it('renders NO badge when the row carries no multiplier', () => {
    render(<ModelDropdownList
      models={[{ name: 'minimax-m2.5', description: 'MiniMax M2.5 model' }]}
      activeModel="" onSelect={vi.fn()} />)
    expect(badgeFor('minimax-m2.5')).toBeNull()
  })

  it('does not invent 1x for a row with no multiplier', () => {
    render(<ModelDropdownList
      models={[{ name: 'minimax-m2.5' }]} activeModel="" onSelect={vi.fn()} />)
    expect(screen.queryByText('1x')).toBeNull()
  })

  it.each([0, -1, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY])(
    'renders no badge for the malformed multiplier %s', (bad) => {
      render(<ModelDropdownList
        models={[{ name: 'weird-model', rateMultiplier: bad }]}
        activeModel="" onSelect={vi.fn()} />)
      // 0/NaN/Infinity are not prices — a "0x" badge would read as "free", and
      // "NaNx"/"Infinityx" as a bug. Assert absence outright: an earlier version
      // of this test allowed "badge is null OR its text is non-numeric", which
      // a badge rendering "NaNx" satisfies, so 3 of the 4 cases passed even
      // with the guard removed.
      expect(badgeFor('weird-model')).toBeNull()
    })

  it('carries the tier on the border, never on the digits', () => {
    render(<ModelDropdownList models={[
      { name: 'qwen3-coder-next', rateMultiplier: 0.05 },
      { name: 'gpt-5.6-terra', rateMultiplier: 1 },
      { name: 'gpt-5.6-sol', rateMultiplier: 2.4 },
    ]} activeModel="" onSelect={vi.fn()} />)
    expect(badgeFor('qwen3-coder-next')!.className).toContain('border-ok')
    expect(badgeFor('gpt-5.6-terra')!.className).toContain('border-muted')
    expect(badgeFor('gpt-5.6-sol')!.className).toContain('border-warn')
    // The digits must stay on the guaranteed-legible token in EVERY tier — this
    // is what keeps contrast passing on all 22 themes without per-theme hues.
    for (const n of ['qwen3-coder-next', 'gpt-5.6-terra', 'gpt-5.6-sol']) {
      expect(badgeFor(n)!.className).toContain('text-text')
    }
  })

  it('keeps its own surface on the selected row so the tier hue stays readable', () => {
    render(<ModelDropdownList
      models={[{ name: 'claude-opus-5', rateMultiplier: 2.2 }]}
      activeModel="claude-opus-5" onSelect={vi.fn()} />)
    // Without this the popover's accent-subtle wash shows through the badge.
    expect(badgeFor('claude-opus-5')!.className).toContain('bg-bg-elevated')
  })

  it('explains the bare glyph to a screen reader', () => {
    render(<ModelDropdownList
      models={[{ name: 'claude-opus-5', rateMultiplier: 2.2 }]}
      activeModel="" onSelect={vi.fn()} />)
    const row = screen.getByRole('option')
    // The option's accessible name is built from its contents, so the
    // explanation has to be IN the tree, not on a title= attribute.
    expect(row.textContent).toContain('2.2x the credit cost of Auto')
    expect(row.querySelector('[aria-hidden="true"]')).toHaveTextContent('2.2x')
  })

  it('renders Auto’s short label from the catalog, not the row', () => {
    render(<ModelDropdownList
      models={[{ name: 'auto', description: '', rateMultiplier: 1 }]}
      activeModel="" onSelect={vi.fn()} />)
    // The row carries no description; the label comes from
    // components.modelDropdownList.auto_default at render time.
    expect(screen.getByText('Default')).toBeInTheDocument()
  })

  it('prefers the catalog label over whatever description the row carries', () => {
    // kiro's own Auto description is long enough to unbalance the list, so the
    // picker must not fall back to it.
    render(<ModelDropdownList models={[{
      name: 'auto',
      description: 'Models chosen by task for optimal usage and consistent quality',
      rateMultiplier: 1,
    }]} activeModel="" onSelect={vi.fn()} />)
    expect(screen.getByText('Default')).toBeInTheDocument()
    expect(screen.queryByText(/Models chosen by task/)).toBeNull()
  })

  it('does not tell a screen reader that Auto costs 1x of Auto', () => {
    render(<ModelDropdownList
      models={[{ name: 'auto', rateMultiplier: 1 }]} activeModel="" onSelect={vi.fn()} />)
    const row = screen.getByRole('option')
    expect(row.textContent).toContain('1.0x, the baseline credit cost')
    expect(row.textContent).not.toContain('the credit cost of Auto')
  })

  it('gives every badge the same width so the column is not ragged', () => {
    render(<ModelDropdownList models={[
      { name: 'qwen3-coder-next', rateMultiplier: 0.05 },
      { name: 'gpt-5.6-terra', rateMultiplier: 1 },
    ]} activeModel="" onSelect={vi.fn()} />)
    // Without a min-width the pills size to content, so "1.0x" is visibly
    // narrower than "0.05x" and the badge column gains a ragged left edge.
    for (const n of ['qwen3-coder-next', 'gpt-5.6-terra']) {
      expect(badgeFor(n)!.className).toMatch(/min-w-\[/)
      expect(badgeFor(n)!.className).toContain('tabular-nums')
    }
  })
})

describe('withAutoFirst — Auto keeps the multiplier it was served', () => {
  it('folds the live auto row’s data onto the short-labelled entry', () => {
    const [auto] = withAutoFirst([
      { name: 'auto', description: 'Models chosen by task…', contextWindow: 1_000_000, rateMultiplier: 1 },
      { name: 'claude-opus-5', description: '…', rateMultiplier: 2.2 },
    ])
    expect(auto.name).toBe('auto')
    // No English literal on the row: the short label is a catalog key resolved
    // where it renders. Carrying 'Default' here put untranslated user-visible
    // copy in a data module, which the i18n gate measures per-file vs the base.
    expect(auto.description).toBe('')
    expect(auto.rateMultiplier).toBe(1)      // live data preserved
    expect(auto.contextWindow).toBe(1_000_000)
  })

  it('leaves Auto unpriced when the backend sent no auto row', () => {
    // Cold start / degraded list: nothing told us Auto's rate, so nothing is shown.
    const [auto] = withAutoFirst([{ name: 'claude-opus-5', description: '…', rateMultiplier: 2.2 }])
    expect(auto.rateMultiplier).toBeUndefined()
  })

  it('puts auto first exactly once and keeps the backend order after it', () => {
    const out = withAutoFirst([
      { name: 'claude-opus-5', description: '' },
      { name: 'auto', description: '' },
      { name: 'gpt-5.6-luna', description: '' },
    ])
    expect(out.map(m => m.name)).toEqual(['auto', 'claude-opus-5', 'gpt-5.6-luna'])
  })

  it('folds duplicate auto rows into one option', () => {
    // A second auto row is a backend bug; rendering it twice (with a colliding
    // React key) is worse than dropping it.
    const out = withAutoFirst([
      { name: 'auto', description: 'first', rateMultiplier: 1 },
      { name: 'auto', description: 'second', rateMultiplier: 9 },
      { name: 'glm-5', description: '' },
    ])
    expect(out.map(m => m.name)).toEqual(['auto', 'glm-5'])
    expect(out[0].rateMultiplier).toBe(1) // the FIRST auto row wins
  })

  it('drops nameless rows rather than rendering an empty option', () => {
    const out = withAutoFirst([{ name: '', description: 'junk' }, { name: 'glm-5', description: '' }])
    expect(out.map(m => m.name)).toEqual(['auto', 'glm-5'])
  })
})
