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
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { readMembersPage } from './membersPageFamily'

import { SearchFilterBar, FilterMenuButton, FilterChip } from '../components/SearchFilterBar'

const read = (...p: string[]) => readFileSync(join(__dirname, '..', ...p), 'utf8')

/** The sidebar's own files: the facade and the owners under pages/chat-sidebar/ it
 *  composes, where its search row and chips are drawn. */
function sidebarFiles(): string[][] {
  const root = join(__dirname, '..')
  const out: string[][] = [['pages', 'ChatSidebar.tsx']]
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const p = join(dir, name)
      if (statSync(p).isDirectory()) walk(p)
      else if (/\.(ts|tsx)$/.test(name) && !/\.test\./.test(name)) out.push(relative(root, p).split(/[\\/]/))
    }
  }
  walk(join(root, 'pages', 'chat-sidebar'))
  return out
}

/** Per page: the files that render its search row, and the text the negative pins read. */
const PAGES: ReadonlyArray<readonly [label: string, files: readonly string[][], scan: () => string]> = [
  ['ChatSidebar', sidebarFiles(), () => source(sidebarFiles())],
  ['MembersPage', [['pages', 'members', 'MembersPageRoster.tsx']], readMembersPage],
]

/** One page's source text: every file it is built from, joined. */
const source = (files: readonly string[][]) => files.map(f => read(...f)).join('\n')

describe('search row parity — one SearchFilterBar on both list panels', () => {
  it.each(PAGES)('%s mounts SearchFilterBar with a FilterMenuButton trigger', (_label, files, scan) => {
    const src = source(files)
    const all = scan()
    expect(src).toMatch(/import \{[^}]*\bSearchFilterBar\b[^}]*\bFilterMenuButton\b[^}]*\} from '(\.\.\/)+components\/SearchFilterBar'/)
    expect(src).toContain('<SearchFilterBar')
    expect(src).toContain('<FilterMenuButton')
    // The list's search field is the shared row now; a second SearchInput in
    // the page is fine for OTHER fields (the sidebar's older-sessions search),
    // but never for the roster / session list search itself.
    expect(all).not.toMatch(/<SearchInput[^>]*search_members/)
    expect(all).not.toMatch(/<SearchInput[^>]*search_sessions'\)/)
    // The trigger's chrome lives in the component, not re-typed per page.
    expect(all).not.toContain('<ListFilter size={14} />')
  })

  it('MembersPage carries no filter-toggle chip row of its own — filters live in the menu', () => {
    const src = readMembersPage()
    expect(src).not.toMatch(/data-testid="member-filters"[^>]*>\s*<button/)
    expect(src).not.toContain('rounded-full text-[11px] border transition-colors')
  })

  it.each(PAGES)('%s marks its active filters with the shared FilterChip row, not an inline pill', (_label, files, scan) => {
    const src = source(files)
    expect(src).toMatch(/import \{[^}]*\bFilterChip\b[^}]*\bFILTER_CHIP_ROW_CLS\b[^}]*\} from '(\.\.\/)+components\/SearchFilterBar'/)
    expect(src).toContain('<FilterChip')
    expect(src).toContain('className={FILTER_CHIP_ROW_CLS}')
    // The pill's colour recipe lives in the component; neither page re-types it.
    // (The sidebar's aggregate tag chip is a different control — several
    // colours in one pill — and keeps its own markup.)
    expect(scan()).not.toContain('color-mix(in srgb, ${')
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

  it('`pause` splits the chip: the body pauses or resumes, only the X clears', () => {
    const onClear = vi.fn()
    const onToggle = vi.fn()
    const chip = (paused: boolean) => (
      <FilterChip
        label="Starred (2)" color="var(--accent)"
        pause={{
          paused, onToggle,
          pauseLabel: 'Pause Starred filter', resumeLabel: 'Resume Starred filter',
          pausedText: 'paused', pauseHint: 'pause', resumeHint: 'resume',
        }}
        clearLabel="Clear Starred filter" onClear={onClear} testId="p"
      />
    )
    const { getByTestId, rerender } = render(chip(false))
    const toggle = getByTestId('p-toggle')
    expect(toggle.getAttribute('aria-label')).toBe('Pause Starred filter')
    expect(toggle.getAttribute('aria-pressed')).toBe('false')
    expect(toggle.getAttribute('title')).toBe('Pause Starred filter')
    // The hint is drawn by a pseudo-element, so it adds no text and no width.
    expect(toggle.textContent).toBe('Starred (2)')
    expect(toggle.getAttribute('data-hint')).toBe('pause')
    expect(toggle.className.split(/\s+/)).toEqual(expect.arrayContaining([
      'after:content-[attr(data-hint)]', 'after:absolute', 'after:inset-0', 'after:hidden', 'hover:after:flex', 'focus-visible:after:flex',
    ]))
    expect(toggle.className).not.toMatch(/after:(bottom|top)-full/)
    expect(toggle.querySelector('svg')).toBeNull()
    fireEvent.click(toggle)
    expect(onToggle).toHaveBeenCalledTimes(1)
    expect(onClear).not.toHaveBeenCalled()

    rerender(chip(true))
    expect(getByTestId('p').className.split(/\s+/)).toEqual(expect.arrayContaining(['border-dashed', 'opacity-70']))
    expect(getByTestId('p').getAttribute('style')).toContain('var(--accent)')
    expect(getByTestId('p-toggle').getAttribute('aria-pressed')).toBe('true')
    expect(getByTestId('p-toggle').getAttribute('title')).toBe('Resume Starred filter')
    expect(getByTestId('p-toggle').getAttribute('data-hint')).toBe('resume')
    expect(getByTestId('p-toggle').textContent).toBe('Starred (2) · paused')
    expect(getByTestId('p').querySelectorAll('button')).toHaveLength(2)

    const clear = getByTestId('p-clear')
    expect(clear.getAttribute('aria-label')).toBe('Clear Starred filter')
    fireEvent.click(clear)
    expect(onClear).toHaveBeenCalledTimes(1)
    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  it('a pausable chip shrinks to its row and truncates the label, never the X', () => {
    const { getByTestId } = render(
      <FilterChip
        label="Recent · 1h (4)" color="var(--accent)"
        pause={{
          paused: true, onToggle: () => {},
          pauseLabel: 'Pause Recent filter', resumeLabel: 'Resume Recent filter',
          pausedText: 'paused', pauseHint: 'pause', resumeHint: 'resume',
        }}
        clearLabel="Clear Recent filter" onClear={() => {}} testId="p"
      />,
    )
    const cls = (id: string) => getByTestId(id).className.split(/\s+/)
    expect(cls('p')).toEqual(expect.arrayContaining(['max-w-full', 'min-w-0']))
    expect(cls('p-toggle')).toContain('min-w-0')
    expect(getByTestId('p-toggle').querySelector('.truncate')!.textContent).toBe('Recent · 1h (4) · paused')
    expect(cls('p-clear')).toContain('shrink-0')
  })

  it('a clear-only chip draws no paused state', () => {
    const { getByTestId } = render(
      <FilterChip label="Starred (2)" color="var(--accent)" clearLabel="Clear Starred filter" onClear={() => {}} testId="c" />,
    )
    expect(getByTestId('c').className.split(/\s+/)).not.toContain('border-dashed')
    expect(getByTestId('c').querySelector('svg.lucide-pause')).toBeNull()
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
