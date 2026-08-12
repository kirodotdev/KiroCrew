/**
 * Coverage tests for the localStorage debug page.
 *
 * The file sat at 3/92 statements: only its module-level constants ran. Nothing
 * below the top of the module had ever executed — not the scanner, not the
 * prefix classifier, not one of the five mutating handlers (refresh / delete key
 * / delete group / export / clear-orphans), and none of the render branches
 * (the three usage-bar colours, the over-quota warning, the per-group delete
 * button, the value inspector's expand + truncation, the filter, the 200-row
 * cap).
 *
 * Harness notes:
 *
 * - Plain `render`, no providers. The page reads no Redux state, no router and
 *   no query client; wrapping it in `renderWithProviders` would only pull in
 *   ThemeProvider's rAF/setTimeout palette commit, which is not the code under
 *   test and is exactly the kind of post-teardown callback that reddens a suite
 *   with an unhandled error. Fake timers are armed anyway as a fence.
 * - `fireEvent`, not `userEvent`: every interaction here is a synchronous state
 *   update, and userEvent's own timer plumbing does not mix well with the fake
 *   timers above.
 * - Storage is the deterministic in-memory Storage the suite setup installs
 *   (`integration/setup.ts`), so seeding means `setItem` on that double and
 *   never on a real browser store. It is cleared before AND after every test so
 *   nothing leaks into a neighbouring file.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'

import LocalStorageDebug from '../pages/LocalStorageDebug'
import { i18nT } from '../i18n/t'

// Resolve the button's tooltip through the SAME lookup the component uses, rather
// than repeating its English wording here. The literal this test used to match
// ("Delete cached scroll positions") stopped existing when the copy was rewritten
// to "Delete cached row heights and saved reading positions ...", which turned a
// pure copy edit into a red shard. Going through the catalog means a reworded
// string can never break this test, while a MISSING key still will -- which is
// the failure actually worth catching.
const ORPHAN_TITLE = i18nT(
  'pages.localStorageDebug.delete_cached_scroll_positions_from_old_sessions',
)

/** Approximate quota the page assumes (5 MiB). */
const QUOTA = 5 * 1024 * 1024

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  localStorage.clear()
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
  localStorage.clear()
})

/** Replace storage contents with `pairs`, in the given insertion order. */
function seed(pairs: Record<string, string>): void {
  localStorage.clear()
  for (const [k, v] of Object.entries(pairs)) localStorage.setItem(k, v)
}

/** The inspector row for one storage key (the clickable `role="button"` line). */
function keyRow(key: string): HTMLElement {
  const row = screen.getByText(key).closest('[role="button"]')
  if (!row) throw new Error(`no inspector row for ${key}`)
  return row as HTMLElement
}

/** The category-table row for one prefix label. */
function groupRow(prefix: string): HTMLElement {
  const cell = screen.getByText(prefix)
  if (!cell.parentElement) throw new Error(`no category row for ${prefix}`)
  return cell.parentElement
}

/** Every inspector row currently rendered, in DOM order. */
function renderedKeys(): string[] {
  return screen
    .queryAllByLabelText('Delete key')
    .map(b => (b.closest('[role="button"]') as HTMLElement).firstElementChild?.textContent ?? '')
}

/** The usage meter's filled bar. */
function usageBar(container: HTMLElement): HTMLElement {
  const bar = container.querySelector('.h-full.rounded-full')
  if (!bar) throw new Error('no usage bar')
  return bar as HTMLElement
}

// ─── scanning, grouping, sorting ─────────────────────────────────────────────

describe('LocalStorageDebug scan', () => {
  it('reports an empty store with no rows and no keys', () => {
    const { container } = render(<LocalStorageDebug />)

    expect(screen.getByText('0 keys')).toBeInTheDocument()
    expect(renderedKeys()).toEqual([])
    expect(screen.queryByText('(static keys)')).toBeNull()
    expect(usageBar(container).getAttribute('style')).toContain('width: 0%')
  })

  it('sorts keys by descending size and counts them in the overview', () => {
    seed({ small: 'a', huge: 'z'.repeat(500), medium: 'm'.repeat(50) })

    render(<LocalStorageDebug />)

    expect(screen.getByText('3 keys')).toBeInTheDocument()
    expect(renderedKeys()).toEqual(['huge', 'medium', 'small'])
  })

  it('classifies every known prefix and buckets the rest as static keys', () => {
    seed({
      'vc_heights_chat': 'x',
      'kirocrew:touched-files:s1': 'x',
      'mc-cmt-read:42': 'x',
      'mimir-tasks:board': 'x',
      'sort:alpha': 'x',
      'mc-chat-1': 'x',
      'mc-paste-1': 'x',
      'theme': 'dark',
      'lang': 'en',
    })

    render(<LocalStorageDebug />)

    for (const label of [
      'vc_heights_*',
      'kirocrew:touched-files:*',
      'mc-cmt-read:*',
      'mimir-tasks:*',
      'sort:*',
      'mc-chat-*',
      'mc-paste-*',
    ]) {
      expect(within(groupRow(label)).getByText('1')).toBeInTheDocument()
    }
    // `theme` and `lang` match no pattern, so they share the fallback bucket.
    expect(within(groupRow('(static keys)')).getByText('2')).toBeInTheDocument()
  })

  it('skips a null key index and treats a missing value as empty', () => {
    seed({ ghost: 'x', real: 'y' })
    const origKey = Storage.prototype.key
    vi.spyOn(Storage.prototype, 'key').mockImplementation(function (
      this: Storage,
      i: number,
    ) {
      return i === 0 ? null : origKey.call(this, i)
    })
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => null)

    render(<LocalStorageDebug />)

    expect(screen.getByText('1 keys')).toBeInTheDocument()
    expect(renderedKeys()).toEqual(['real'])

    fireEvent.click(keyRow('real'))
    const pre = keyRow('real').parentElement?.querySelector('pre')
    expect(pre).not.toBeNull()
    expect(pre?.textContent).toBe('')
  })
})

// ─── usage meter ─────────────────────────────────────────────────────────────

describe('LocalStorageDebug usage meter', () => {
  it('paints the bar with the accent colour below half quota', () => {
    seed({ tiny: 'a' })

    const { container } = render(<LocalStorageDebug />)

    expect(usageBar(container).className).toContain('bg-accent')
    expect(screen.queryByText(/app may crash on next write/)).toBeNull()
  })

  it('paints the bar amber past half quota without warning yet', () => {
    seed({ chunky: 'a'.repeat(Math.round(QUOTA * 0.6)) })

    const { container } = render(<LocalStorageDebug />)

    expect(usageBar(container).className).toContain('bg-warn')
    expect(screen.queryByText(/app may crash on next write/)).toBeNull()
  })

  it('warns and clamps the bar at 100% once the store exceeds the quota', () => {
    seed({ overflowing: 'a'.repeat(QUOTA + 1000) })

    const { container } = render(<LocalStorageDebug />)

    const bar = usageBar(container)
    expect(bar.className).toContain('bg-danger')
    expect(bar.getAttribute('style')).toContain('width: 100%')
    expect(screen.getByText(/Storage is 100% full/)).toBeInTheDocument()
  })
})

// ─── mutating actions ────────────────────────────────────────────────────────

describe('LocalStorageDebug actions', () => {
  it('re-scans storage when refresh is pressed', () => {
    seed({ first: 'a' })

    render(<LocalStorageDebug />)
    expect(renderedKeys()).toEqual(['first'])

    localStorage.setItem('second', 'bb')
    fireEvent.click(screen.getByTitle('Refresh'))

    expect(renderedKeys()).toEqual(['second', 'first'])
    expect(screen.getByText('2 keys')).toBeInTheDocument()
  })

  it('deletes one key from storage and from the list', () => {
    seed({ keep: 'a', drop: 'bbbb' })

    render(<LocalStorageDebug />)

    fireEvent.click(within(keyRow('drop')).getByLabelText('Delete key'))

    expect(localStorage.getItem('drop')).toBeNull()
    expect(localStorage.getItem('keep')).toBe('a')
    expect(renderedKeys()).toEqual(['keep'])
  })

  it('offers a group delete for prefixed buckets but not for static keys', () => {
    seed({ 'sort:alpha': 'a', 'theme': 'dark' })

    render(<LocalStorageDebug />)

    expect(within(groupRow('sort:*')).getByRole('button', { name: /Clear/ })).toBeInTheDocument()
    expect(within(groupRow('(static keys)')).queryByRole('button')).toBeNull()
  })

  it('deletes every key in a group', () => {
    seed({ 'sort:alpha': 'a', 'sort:beta': 'bb', 'theme': 'dark' })

    render(<LocalStorageDebug />)

    fireEvent.click(within(groupRow('sort:*')).getByRole('button', { name: /Clear/ }))

    expect(localStorage.getItem('sort:alpha')).toBeNull()
    expect(localStorage.getItem('sort:beta')).toBeNull()
    expect(localStorage.getItem('theme')).toBe('dark')
    expect(screen.queryByText('sort:*')).toBeNull()
    expect(renderedKeys()).toEqual(['theme'])
  })

  it('clears only the scroll-height orphan caches', () => {
    seed({ 'vc_heights_a': 'a', 'vc_heights_b': 'bb', 'sort:alpha': 'c' })

    render(<LocalStorageDebug />)

    fireEvent.click(screen.getByTitle(ORPHAN_TITLE))

    expect(localStorage.getItem('vc_heights_a')).toBeNull()
    expect(localStorage.getItem('vc_heights_b')).toBeNull()
    expect(localStorage.getItem('sort:alpha')).toBe('c')
    expect(renderedKeys()).toEqual(['sort:alpha'])
  })

  it('exports every key as a downloadable JSON blob', async () => {
    seed({ alpha: 'one', beta: 'two' })

    const blobs: Blob[] = []
    const names: string[] = []
    const origCreate = URL.createObjectURL
    const origRevoke = URL.revokeObjectURL
    const revoked: string[] = []
    URL.createObjectURL = vi.fn((b: Blob) => {
      blobs.push(b)
      return 'blob:ls-debug'
    }) as typeof URL.createObjectURL
    URL.revokeObjectURL = vi.fn((u: string) => {
      revoked.push(u)
    }) as typeof URL.revokeObjectURL
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function capture(this: HTMLAnchorElement) {
        names.push(this.download)
      })

    try {
      render(<LocalStorageDebug />)
      fireEvent.click(screen.getByTitle('Export JSON'))

      expect(blobs).toHaveLength(1)
      expect(blobs[0].type).toBe('application/json')
      expect(JSON.parse(await blobs[0].text())).toEqual({ alpha: 'one', beta: 'two' })
      expect(names).toHaveLength(1)
      expect(names[0]).toMatch(/^kirocrew-localstorage-\d+\.json$/)
      expect(revoked).toEqual(['blob:ls-debug'])
    } finally {
      clickSpy.mockRestore()
      URL.createObjectURL = origCreate
      URL.revokeObjectURL = origRevoke
    }
  })
})

// ─── value inspector ─────────────────────────────────────────────────────────

describe('LocalStorageDebug inspector', () => {
  it('expands and collapses a value on click', () => {
    seed({ 'sort:alpha': 'stored-payload' })

    render(<LocalStorageDebug />)
    const row = keyRow('sort:alpha')
    expect(row).toHaveAttribute('aria-expanded', 'false')

    fireEvent.click(row)
    expect(keyRow('sort:alpha')).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('stored-payload')).toBeInTheDocument()

    fireEvent.click(keyRow('sort:alpha'))
    expect(keyRow('sort:alpha')).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('stored-payload')).toBeNull()
  })

  it('toggles with Enter and Space, and ignores other keys', () => {
    seed({ 'sort:alpha': 'stored-payload' })

    render(<LocalStorageDebug />)

    fireEvent.keyDown(keyRow('sort:alpha'), { key: 'Enter' })
    expect(keyRow('sort:alpha')).toHaveAttribute('aria-expanded', 'true')

    fireEvent.keyDown(keyRow('sort:alpha'), { key: ' ' })
    expect(keyRow('sort:alpha')).toHaveAttribute('aria-expanded', 'false')

    fireEvent.keyDown(keyRow('sort:alpha'), { key: 'x' })
    expect(keyRow('sort:alpha')).toHaveAttribute('aria-expanded', 'false')
  })

  it('does not toggle when the keystroke came from the delete button', () => {
    seed({ 'sort:alpha': 'stored-payload' })

    render(<LocalStorageDebug />)

    fireEvent.keyDown(within(keyRow('sort:alpha')).getByLabelText('Delete key'), { key: 'Enter' })

    expect(keyRow('sort:alpha')).toHaveAttribute('aria-expanded', 'false')
  })

  it('truncates a value longer than 2000 characters', () => {
    seed({ bulky: 'q'.repeat(2500) })

    render(<LocalStorageDebug />)
    fireEvent.click(keyRow('bulky'))

    const pre = keyRow('bulky').parentElement?.querySelector('pre')
    expect(pre).not.toBeNull()
    const shown = pre?.textContent ?? ''
    expect(shown).toContain('(truncated)')
    expect(shown.replace(/[^q]/g, '')).toHaveLength(2000)
  })

  it('shows an untruncated value at exactly the 2000-character boundary', () => {
    seed({ edge: 'q'.repeat(2000) })

    render(<LocalStorageDebug />)
    fireEvent.click(keyRow('edge'))

    const pre = keyRow('edge').parentElement?.querySelector('pre')
    expect(pre?.textContent).not.toContain('(truncated)')
    expect(pre?.textContent).toHaveLength(2000)
  })
})

// ─── filter and overflow cap ─────────────────────────────────────────────────

describe('LocalStorageDebug filter', () => {
  it('filters keys case-insensitively and restores the full list when cleared', () => {
    seed({ 'sort:alpha': 'a', 'mc-chat-1': 'b', 'theme': 'c' })

    render(<LocalStorageDebug />)
    const input = screen.getByLabelText('Filter keys')

    fireEvent.change(input, { target: { value: 'SORT:' } })
    expect(renderedKeys()).toEqual(['sort:alpha'])

    fireEvent.change(input, { target: { value: '' } })
    expect(renderedKeys()).toHaveLength(3)
  })

  it('renders nothing when the filter matches no key', () => {
    seed({ 'sort:alpha': 'a' })

    render(<LocalStorageDebug />)
    fireEvent.change(screen.getByLabelText('Filter keys'), { target: { value: 'nope' } })

    expect(renderedKeys()).toEqual([])
  })

  it('caps the list at 200 rows and says how many were hidden', () => {
    const pairs: Record<string, string> = {}
    for (let i = 0; i < 201; i++) pairs[`sort:k${String(i).padStart(3, '0')}`] = 'v'
    seed(pairs)

    render(<LocalStorageDebug />)

    expect(renderedKeys()).toHaveLength(200)
    expect(screen.getByText(/Showing 200 of\s+201/)).toBeInTheDocument()
  })

  it('drops the overflow notice once the filter narrows below the cap', () => {
    const pairs: Record<string, string> = {}
    for (let i = 0; i < 201; i++) pairs[`sort:k${String(i).padStart(3, '0')}`] = 'v'
    seed(pairs)

    render(<LocalStorageDebug />)
    fireEvent.change(screen.getByLabelText('Filter keys'), { target: { value: 'k00' } })

    expect(renderedKeys()).toHaveLength(10)
    expect(screen.queryByText(/Showing 200 of/)).toBeNull()
  })
})
