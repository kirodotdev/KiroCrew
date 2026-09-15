import { describe, expect, it, vi } from 'vitest'
import { render } from '@testing-library/react'
import FollowUpBar from '../components/FollowUpBar'

/**
 * A declined chip must be distinguishable from its siblings WITHOUT hovering.
 *
 * In a quick-send row every chip sends on click, except one whose label would become a
 * command once the marker came off -- that one fills the composer instead. Identical-looking
 * siblings behaving differently was reachable only through a tooltip or the under-bar note,
 * i.e. after the first surprising click. The dashed edge states it at rest.
 */
describe('FollowUpBar marks a declined chip at the chip itself', () => {
  function chips(options: string[]) {
    const { container } = render(
      <FollowUpBar
        options={options}
        recommended={options[0] ?? null}
        picked={new Set<string>()}
        onSelect={vi.fn()}
        onSend={vi.fn()}
        quickSend
      />,
    )
    return Array.from(container.querySelectorAll('button')).map(b => b.className)
  }

  // `/clear` would become a live command if the marker were stripped, so the guard declines it.
  const DECLINED = '(recommended) /clear'
  const ORDINARY = 'Merge it now'

  it('gives the declined chip a dashed edge', () => {
    const withDeclined = chips([DECLINED, ORDINARY]).filter(c => c.includes('border-dashed'))
    expect(withDeclined.length).toBeGreaterThan(0)
  })

  it('leaves an ordinary chip solid, so the marking actually discriminates', () => {
    const all = chips([DECLINED, ORDINARY])
    const dashed = all.filter(c => c.includes('border-dashed'))
    expect(dashed.length).toBe(1)
    expect(all.length - dashed.length).toBeGreaterThan(0)
  })

  it('marks nothing when no chip is declined', () => {
    expect(chips([ORDINARY, 'Skip it']).some(c => c.includes('border-dashed'))).toBe(false)
  })

  it('marks it in the scroll layout too, which renders through a different map', () => {
    const { container } = render(
      <FollowUpBar
        options={[DECLINED, ORDINARY]}
        recommended={DECLINED}
        picked={new Set<string>()}
        onSelect={vi.fn()}
        onSend={vi.fn()}
        quickSend
        layout="scroll"
      />,
    )
    const all = Array.from(container.querySelectorAll('button')).map(b => b.className)
    expect(all.filter(c => c.includes('border-dashed')).length).toBe(1)
  })

  // A BARE command carries no marker, so the marker-only gate left it looking and reading
  // like an ordinary chip while the send gates silently refused it.
  const BARE = '/clear'

  it('marks a bare command chip at rest, not only a marked one', () => {
    const all = chips([BARE, ORDINARY])
    expect(all.filter(c => c.includes('border-dashed')).length).toBe(1)
  })

  it('does not promise an instant send on a bare command chip', () => {
    const { container } = render(
      <FollowUpBar
        options={[BARE, ORDINARY]}
        recommended={null}
        picked={new Set<string>()}
        onSelect={vi.fn()}
        onSend={vi.fn()}
        quickSend
      />,
    )
    const titles = Array.from(container.querySelectorAll('button')).map(
      b => b.getAttribute('title') || '',
    )
    // The ordinary sibling still offers it, so the assertion discriminates.
    expect(titles.some(t => /instantly/i.test(t))).toBe(true)
    const bare = titles.filter(t => t.includes(BARE) || /editable/i.test(t))
    expect(bare.length).toBeGreaterThan(0)
    expect(bare.some(t => /instantly|double-click/i.test(t))).toBe(false)
  })

  function note(options: string[]): string | null {
    const { container } = render(
      <FollowUpBar
        options={options}
        recommended={null}
        picked={new Set<string>()}
        onSelect={vi.fn()}
        onSend={vi.fn()}
        quickSend
      />,
    )
    const el = container.querySelector('[data-testid="recommended-kept-note"]')
    return el ? el.textContent : null
  }

  function commandNote(options: string[]): string | null {
    const { container } = render(
      <FollowUpBar
        options={options}
        recommended={null}
        picked={new Set<string>()}
        onSelect={vi.fn()}
        onSend={vi.fn()}
        quickSend
      />,
    )
    const el = container.querySelector('[data-testid="command-shaped-note"]')
    return el ? el.textContent : null
  }

  it('shows the prefix note only when a marker is actually on screen', () => {
    expect(note([DECLINED, ORDINARY])).toBeTruthy()
  })

  it('shows no prefix note for a bare command chip, which carries no prefix', () => {
    // The note's own text names "(recommended)". Firing it here described something invisible.
    expect(note([BARE, ORDINARY])).toBeNull()
  })

  it('explains a bare command chip VISIBLY, not only on hover', () => {
    // Touch and keyboard users never see a `title`, so the dashed edge was unexplained.
    const text = commandNote([BARE, ORDINARY])
    expect(text).toBeTruthy()
    expect(text).not.toContain('(recommended)')
  })

  it('shows no command note when every option is ordinary', () => {
    expect(commandNote([ORDINARY, 'Skip it'])).toBeNull()
  })

  it('shows both notes when a menu carries one of each', () => {
    // ONE note, not two: four sentences of 11px meta-prose under the composer is the defect.
    const { container } = render(
      <FollowUpBar
        options={[DECLINED, BARE]}
        recommended={null}
        picked={new Set<string>()}
        onSelect={vi.fn()}
        onSend={vi.fn()}
        quickSend
      />,
    )
    expect(container.querySelectorAll('p[data-testid$="-note"]')).toHaveLength(1)
    expect(container.querySelector('[data-testid="recommended-kept-note"]')).toBeNull()
    expect(container.querySelector('[data-testid="command-shaped-note"]')).toBeNull()
    const combined = container.querySelector('[data-testid="mixed-hold-note"]')
    expect(combined).toBeTruthy()
    // Still covers BOTH hazards, so collapsing the pair loses no explanation.
    expect(combined!.textContent).toContain('(recommended)')
    expect(combined!.textContent).toContain('run something')
  })

  it('leads the prefix note with the action, not the hazard', () => {
    // Measures the PROPERTY, not word positions: an unscoped "click one" is false with Quick
    // Send on, where an ordinary sibling chip does send instantly.
    const text = note([DECLINED, ORDINARY]) ?? ''
    expect(text).toBeTruthy()
    expect(text).toMatch(/^Clicking the/)
    const safe = text.indexOf('composer')
    const hazard = text.indexOf('sent as written')
    expect(safe).toBeGreaterThanOrEqual(0)
    expect(hazard).toBeGreaterThanOrEqual(0)
    expect(safe).toBeLessThan(hazard)
  })

  it('states the mixed note as a guarantee, not a hedge', () => {
    // The guard makes a click fill the composer, so warning that a command-shaped option
    // "could run" alarms the reader about an outcome the code prevents.
    const text = commandNote([DECLINED, BARE]) ?? ''
    expect(text).toBeFalsy()
    const { container } = render(
      <FollowUpBar
        options={[DECLINED, BARE]}
        recommended={null}
        picked={new Set<string>()}
        onSelect={vi.fn()}
        onSend={vi.fn()}
        quickSend
      />,
    )
    const mixed = container.querySelector('[data-testid="mixed-hold-note"]')!.textContent ?? ''
    expect(mixed).toContain('instead of being sent')
    expect(mixed).not.toContain('could run.')
    // Scoped subject, short, and it keeps the consequence clause its sibling carries.
    expect(mixed).toMatch(/^Clicking the/)
    expect(mixed).toContain('sent as written')
    expect(mixed.split('.').filter(s => s.trim()).length).toBeLessThanOrEqual(2)
  })

  it('describes what happens rather than enumerating which characters trigger it', () => {
    // The enumeration was wrong twice: the predicate also fires on `!`, `$`, `action::`, a
    // bracketed opener, and an embedded token anywhere. A list has to track a growing predicate.
    const text = commandNote([BARE, ORDINARY]) ?? ''
    expect(text).toBeTruthy()
    for (const sigil of ['“/”', '“@”', '"/"', '"@"', '`/`', '`@`']) {
      expect(text).not.toContain(sigil)
    }
    expect(text).toContain('composer')
  })

  it('shows exactly one note for a single-kind menu too', () => {
    for (const opts of [[DECLINED, ORDINARY], [BARE, ORDINARY]]) {
      const { container } = render(
        <FollowUpBar
          options={opts}
          recommended={null}
          picked={new Set<string>()}
          onSelect={vi.fn()}
          onSend={vi.fn()}
          quickSend
        />,
      )
      expect(container.querySelectorAll('p[data-testid$="-note"]')).toHaveLength(1)
    }
  })
})
