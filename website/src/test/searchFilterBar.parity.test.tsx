/**
 * Parity pin: the Sessions sidebar and the Crew Members roster mount the SAME
 * search row (`components/SearchFilterBar`) with the same trailing filter
 * button, and neither hand-rolls the field, the clear button or the trigger.
 *
 * The roster once carried its own `SearchInput` plus a row of filter chips
 * under it; the sidebar keeps its filters in a menu docked in the field. Same
 * component now, so the field, its clear button, the 24px trigger and the
 * padding that keeps typed text clear of it are one implementation.
 *
 * Mutation-verified by construction: re-inlining `<SearchInput` or the trigger
 * markup in either page fails the negative pin; dropping the clear button or
 * the padding table from the component fails the render pins.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { SearchFilterBar, FilterMenuButton, FilterChip } from '../components/SearchFilterBar'

const read = (...p: string[]) => readFileSync(join(__dirname, '..', ...p), 'utf8')

const PAGES: ReadonlyArray<readonly [label: string, file: readonly string[]]> = [
  ['ChatSidebar', ['pages', 'ChatSidebar.tsx']],
  ['MembersPage', ['pages', 'members', 'MembersPage.tsx']],
]

describe('search row parity — one SearchFilterBar on both list panels', () => {
  it.each(PAGES)('%s mounts SearchFilterBar with a FilterMenuButton trigger', (_label, file) => {
    const src = read(...file)
    expect(src).toMatch(/import \{[^}]*\bSearchFilterBar\b[^}]*\bFilterMenuButton\b[^}]*\} from '(\.\.\/)+components\/SearchFilterBar'/)
    expect(src).toContain('<SearchFilterBar')
    expect(src).toContain('<FilterMenuButton')
    // The list's search field is the shared row now; a second SearchInput in
    // the page is fine for OTHER fields (the sidebar's older-sessions search),
    // but never for the roster / session list search itself.
    expect(src).not.toMatch(/<SearchInput[^>]*search_members/)
    expect(src).not.toMatch(/<SearchInput[^>]*search_sessions'\)/)
    // The trigger's chrome lives in the component, not re-typed per page.
    expect(src).not.toContain('<ListFilter size={14} />')
  })

  it('MembersPage carries no filter-toggle chip row of its own — filters live in the menu', () => {
    const src = read('pages', 'members', 'MembersPage.tsx')
    expect(src).not.toMatch(/data-testid="member-filters"[^>]*>\s*<button/)
    expect(src).not.toContain('rounded-full text-[11px] border transition-colors')
  })

  it.each(PAGES)('%s marks its active filters with the shared FilterChip row, not an inline pill', (_label, file) => {
    const src = read(...file)
    expect(src).toMatch(/import \{[^}]*\bFilterChip\b[^}]*\bFILTER_CHIP_ROW_CLS\b[^}]*\} from '(\.\.\/)+components\/SearchFilterBar'/)
    expect(src).toContain('<FilterChip')
    expect(src).toContain('className={FILTER_CHIP_ROW_CLS}')
    // The pill's colour recipe lives in the component; neither page re-types it.
    // (The sidebar's aggregate tag chip is a different control — several
    // colours in one pill — and keeps its own markup.)
    expect(src).not.toContain('color-mix(in srgb, ${')
  })
})

describe('FilterChip contract', () => {
  it('is a dismiss button in the filter colour, named by its clear label', () => {
    const onClear = vi.fn()
    const { getByTestId } = render(
      <FilterChip label="Starred (2)" color="var(--accent)" clearLabel="Clear Starred filter" onClear={onClear} testId="c" />,
    )
    const chip = getByTestId('c')
    expect(chip.tagName).toBe('BUTTON')
    expect(chip.getAttribute('aria-label')).toBe('Clear Starred filter')
    expect(chip.textContent).toBe('Starred (2)')
    for (const cls of ['rounded-full', 'text-[11px]', 'pl-2', 'pr-1']) expect(chip.className.split(/\s+/)).toContain(cls)
    fireEvent.click(chip)
    expect(onClear).toHaveBeenCalledTimes(1)
  })

  it('the aggregate variant is the neutral clear-all pill: no filter colour, the sidebar tag chip\'s chrome', () => {
    const { getByTestId } = render(
      <FilterChip aggregate label="Starred (2), Mine (6)" clearLabel="Clear Starred and Mine filter" onClear={() => {}} testId="a" />,
    )
    const chip = getByTestId('a')
    expect(chip.getAttribute('style')).toBeNull()
    for (const cls of ['rounded-full', 'text-[11px]', 'border', 'border-border', 'text-muted', 'max-w-full']) expect(chip.className.split(/\s+/)).toContain(cls)
    expect(chip.textContent).toBe('Starred (2), Mine (6)')
  })
})

describe('SearchFilterBar contract', () => {
  it('renders the field, shows the clear button only with text, and clears through onChange', () => {
    const onChange = vi.fn()
    const { getByPlaceholderText, queryByTestId, getByTestId, rerender } = render(
      <SearchFilterBar placeholder="Search…" clearLabel="Clear search" value="" onChange={onChange} inputTestId="q" />,
    )
    const input = getByPlaceholderText('Search…') as HTMLInputElement
    expect(input.getAttribute('data-testid')).toBe('q')
    expect(queryByTestId('q-clear')).toBeNull()
    fireEvent.change(input, { target: { value: 'abc' } })
    expect(onChange).toHaveBeenLastCalledWith('abc')

    rerender(<SearchFilterBar placeholder="Search…" clearLabel="Clear search" value="abc" onChange={onChange} inputTestId="q" />)
    const clear = getByTestId('q-clear')
    expect(clear.getAttribute('aria-label')).toBe('Clear search')
    fireEvent.click(clear)
    expect(onChange).toHaveBeenLastCalledWith('')
  })

  it('insets the typed text by the sidebar\'s values: one control 36px, two 56px, +20px with text', () => {
    const cases: Array<[number, string, number]> = [
      [1, '', 36], [1, 'x', 56], [2, '', 56], [2, 'x', 76],
    ]
    for (const [count, value, pad] of cases) {
      const { getByPlaceholderText, unmount } = render(
        <SearchFilterBar placeholder="p" clearLabel="c" value={value} onChange={() => {}} trailingCount={count} trailing={<span />} />,
      )
      expect((getByPlaceholderText('p') as HTMLElement).style.paddingRight).toBe(`${pad}px`)
      unmount()
    }
  })

  it('places the clear button just left of the trailing controls', () => {
    const one = render(<SearchFilterBar placeholder="p" clearLabel="c" value="x" onChange={() => {}} inputTestId="a" trailing={<span />} />)
    expect(one.getByTestId('a-clear').style.right).toBe('32px')
    one.unmount()
    const two = render(<SearchFilterBar placeholder="p" clearLabel="c" value="x" onChange={() => {}} inputTestId="b" trailingCount={2} trailing={<span />} />)
    expect(two.getByTestId('b-clear').style.right).toBe('56px')
  })

  it('FilterMenuButton is the sidebar\'s 24px trigger with a capped corner badge', () => {
    const { getByTestId, rerender } = render(<FilterMenuButton title="Sort and filter" testId="t" badge={3} />)
    const btn = getByTestId('t')
    expect(btn.tagName).toBe('BUTTON')
    expect(btn.getAttribute('aria-label')).toBe('Sort and filter')
    for (const cls of ['w-6', 'h-6', 'rounded']) expect(btn.className.split(/\s+/)).toContain(cls)
    expect(btn.textContent).toBe('3')
    rerender(<FilterMenuButton title="Sort and filter" testId="t" badge={120} />)
    expect(getByTestId('t').textContent).toBe('99+')
    rerender(<FilterMenuButton title="Sort and filter" testId="t" />)
    expect(getByTestId('t').textContent).toBe('')
  })
})
