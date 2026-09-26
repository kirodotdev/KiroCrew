import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import FileChangeChips, { countLines, headerClickAction, type FileChangeEntry } from '../components/FileChangeChips'
import enManual from '../i18n/locales/en.manual.json'
import pluralKeys from '../i18n/pluralKeys.json'
import { CATALOGS } from '../i18n/catalogs'

const change = (path: string, before: string, after: string) => ({ path, before, after })
const rows = (c: HTMLElement) => c.querySelectorAll('[data-testid^="fcc-row-"]')

/* The expanded style renders a lightweight header per closed file and mounts
 * Pierre only after disclosure. Two consequences for this suite:
 *
 *  - Closed filenames, counts, diffstat cells and controls are ordinary DOM and
 *    covered by the focused header tests. Pierre's open header still lives in a
 *    shadow root behind a lazy chunk that never resolves in this suite.
 *  - The line counting used by both header forms is a pure exported function,
 *    so it is tested directly instead of through presentation details.
 *
 * Header parity and mount lifecycle are covered in the focused companion tests. */
describe('countLines', () => {
  it('counts pure additions', () => {
    expect(countLines('a', 'a\nb\nc')).toEqual({ added: 2, removed: 0 })
  })

  it('counts pure removals', () => {
    expect(countLines('a\nb\nc', 'a')).toEqual({ added: 0, removed: 2 })
  })

  it('reports a pure move as +N/-N, not 0/0', () => {
    // LCS attributes a moved line to both sides; a multiset count would call
    // this unchanged, which reads as "nothing happened" on a real reorder.
    expect(countLines('a\nb', 'b\na')).toEqual({ added: 1, removed: 1 })
  })

  it('reports nothing when the content is identical', () => {
    expect(countLines('same\ntext', 'same\ntext')).toEqual({ added: 0, removed: 0 })
  })

  it('counts a mixed edit', () => {
    expect(countLines('one\ntwo\nthree', 'one\ntwo-edited\nthree\nfour')).toEqual({ added: 2, removed: 1 })
  })

  it('treats empty content as zero lines, not one phantom line', () => {
    // ''.split('\n') is [''], which would mis-count a new file as +1/-1.
    expect(countLines('a\nb', '')).toEqual({ added: 0, removed: 2 })
    expect(countLines('', 'a')).toEqual({ added: 1, removed: 0 })
  })

  /* Past 1M LCS cells the counter drops to a multiset count to bound cost.
   * That fallback is cheaper and measurably weaker, so these pin what it
   * reports at that size — including the one case where it disagrees with
   * LCS — rather than restating the expectations above. */
  const numbered = (n: number) => Array.from({ length: n }, (_, i) => `line-${i}`)

  it('counts adds and removes past the LCS cell cap', () => {
    const before = numbered(1100)
    const after = [...before.slice(0, 1095), 'new-a', 'new-b', 'new-c']
    expect(before.length * after.length).toBeGreaterThan(1_000_000)
    expect(countLines(before.join('\n'), after.join('\n'))).toEqual({ added: 3, removed: 5 })
  })

  it('under-reports a pure reorder past the cap, unlike the LCS path below it', () => {
    const before = numbered(1200)
    const reordered = [...before].reverse()
    expect(before.length * reordered.length).toBeGreaterThan(1_000_000)
    expect(countLines(before.join('\n'), reordered.join('\n'))).toEqual({ added: 0, removed: 0 })

    // The same reorder under the cap runs through LCS, which does attribute it.
    const small = numbered(40)
    expect(countLines(small.join('\n'), [...small].reverse().join('\n')).added).toBeGreaterThan(0)
  })

  it('nets out duplicate lines past the cap instead of counting every occurrence', () => {
    const before = [...numbered(1100), 'dup', 'dup', 'dup']
    const after = [...numbered(1100), 'dup']
    expect(before.length * after.length).toBeGreaterThan(1_000_000)
    expect(countLines(before.join('\n'), after.join('\n'))).toEqual({ added: 0, removed: 2 })
  })
})

describe('FileChangeChips', () => {
  it('renders nothing when fileChanges is empty', () => {
    const { container } = render(<FileChangeChips fileChanges={[]} />)
    expect(container.firstChild).toBeNull()
  })

  it('renders nothing when fileChanges is undefined', () => {
    // Component guards against undefined too — keeps consumers from needing
    // their own falsy guard.
    const { container } = render(<FileChangeChips fileChanges={undefined as unknown as FileChangeEntry[]} />)
    expect(container.firstChild).toBeNull()
  })

  it('renders one row per file change', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[change('/a.ts', 'a', 'a\nb'), change('/b.py', 'x', 'y')]} />,
    )
    expect(rows(container)).toHaveLength(2)
    expect(container.querySelector('[data-testid="fcc-row-/a.ts"]')).toBeInTheDocument()
    expect(container.querySelector('[data-testid="fcc-row-/b.py"]')).toBeInTheDocument()
  })

  it('keys rows by path (no duplicate-key warnings)', () => {
    const warn = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { container } = render(
      <FileChangeChips fileChanges={[change('/x.ts', 'a', 'b'), change('/y.ts', 'a', 'b')]} />,
    )
    expect(rows(container)).toHaveLength(2)
    expect(warn).not.toHaveBeenCalledWith(expect.stringContaining('same key'))
    warn.mockRestore()
  })

  it('spells out the file count and aggregate totals in the card header', () => {
    render(<FileChangeChips fileChanges={[change('/a.ts', 'a', 'a\nb'), change('/b.ts', 'a\nb', 'a')]} />)
    expect(screen.getByText('2 files changed')).toBeInTheDocument()
    // Singular forms, and no second ±pair on the right of the same row.
    expect(screen.getByText('1 addition')).toBeInTheDocument()
    expect(screen.getByText('1 removal')).toBeInTheDocument()
  })

  it('pluralises the header totals and omits a side that is zero', () => {
    render(<FileChangeChips fileChanges={[change('/new.ts', '', 'a\nb\nc')]} />)
    expect(screen.getByText('3 additions')).toBeInTheDocument()
    expect(screen.queryByText(/removal/)).not.toBeInTheDocument()
  })

  it('shows an unavailable state instead of counts or an inline diff for truncated snapshots', () => {
    const file = { ...change('/large.ts', 'same', 'same'), truncated: true, snapshot_limit_chars: 200_000 }
    render(<FileChangeChips fileChanges={[file]} />)
    expect(screen.getByText('Diff unavailable: file is too large to compare (over 200,000 characters).')).toBeInTheDocument()
    expect(screen.queryByText('no changes')).not.toBeInTheDocument()
    expect(screen.queryByText(/addition|removal/)).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Show or hide the diff for /large.ts')).not.toBeInTheDocument()
  })

  const TURN_SENTENCE = "This turn's changes were too large to keep every file's diff (over 400,000 characters total)."
  const OPEN_HINT = 'Click a file name to open it.'
  /* The per-file marker names what a demoted row still offers: the file,
   * without its diff. It never describes a file that is absent. */
  const FILE_ONLY = 'File only'
  const demoted = (path: string) => ({ ...change(path, '', ''), truncated: true, content_omitted: true, turn_budget_chars: 400_000 })

  it('names the turn budget, not this file\'s size, when a turn drops a file\'s content', () => {
    // A small file demoted to path-only must not be described as too large:
    // the size that ran out belongs to the turn, not to this file.
    render(<FileChangeChips fileChanges={[demoted('/small.ts')]} />)
    expect(screen.getByText(TURN_SENTENCE)).toBeInTheDocument()
    expect(screen.queryByText(/too large to compare/)).not.toBeInTheDocument()
  })

  it('says the turn-budget sentence once per card and tags each demoted row', () => {
    // The sentence describes the turn. Three demoted rows carry three short
    // tags under ONE notice row, so each row keeps its width for the filename.
    const { container } = render(
      <FileChangeChips fileChanges={[change('/kept.ts', 'a', 'a\nb'), demoted('/a.ts'), demoted('/b.ts'), demoted('/c.ts')]} />,
    )
    expect(screen.getAllByText(TURN_SENTENCE)).toHaveLength(1)
    expect(container.querySelector('[data-fcc-turn-notice]')).toBeInTheDocument()
    expect(screen.getAllByText(FILE_ONLY)).toHaveLength(3)
    for (const path of ['/a.ts', '/b.ts', '/c.ts']) {
      expect(screen.getByTestId(`fcc-row-${path}`).querySelector('[data-fcc-demoted-tag]')).toBeInTheDocument()
    }
    expect(screen.getByTestId('fcc-row-/kept.ts').querySelector('[data-fcc-demoted-tag]')).toBeNull()
  })

  it('restates the turn-budget reason on the demoted tag, as the artifact badge does', () => {
    // The tag is neither a button nor a label; its title is where a hover finds
    // the reason the card's notice row gives once.
    render(<FileChangeChips fileChanges={[demoted('/small.ts')]} />)
    expect(screen.getByText(FILE_ONLY)).toHaveAttribute('title', TURN_SENTENCE)
  })

  /* ── `meta.file_changes_omitted_files`: files the turn's snapshot limits
   *   left out of `fileChanges` altogether (row-cap refusals plus budget-dropped
   *   entries). It counts files, the header's unit, and it is one fact about
   *   the turn. */
  const OMITTED_RE = /more files? changed in this turn (is|are) not listed\./

  it('states the omitted-files notice once per card when the count is above zero', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[change('/a.ts', 'a', 'a\nb'), demoted('/b.ts'), demoted('/c.ts')]} omittedFiles={7} />,
    )
    expect(screen.getAllByText(OMITTED_RE)).toHaveLength(1)
    const notice = container.querySelector('[data-fcc-omitted-notice]')
    expect(notice).toHaveTextContent('7 more files changed in this turn are not listed.')
    // Same unit as the "3 files changed" header, so the two numbers add up;
    // no word a reader has no referent for, and no promise of a way to see
    // a file the turn did not keep.
    expect(screen.getByText('3 files changed')).toBeInTheDocument()
    expect(notice).toHaveTextContent(/files/)
    expect(notice).not.toHaveTextContent(/snapshot|write|shown|show/i)
  })

  it('agrees the noun and verb with a count of one', () => {
    render(<FileChangeChips fileChanges={[change('/a.ts', 'a', 'b')]} omittedFiles={1} />)
    expect(screen.getByText('1 more file changed in this turn is not listed.')).toBeInTheDocument()
  })

  it('gives each state its own words: a listed file says what it offers, an absent file says it is not listed', () => {
    // A demoted row is still a file the reader can open, so its marker names
    // what is here. The card-level line is about files that are not here at
    // all. Neither state borrows the other's wording, in either style.
    for (const style of ['expanded', 'minimal'] as const) {
      const { container, unmount } = render(
        <FileChangeChips fileChanges={[change('/a.ts', 'a', 'a\nb'), demoted('/b.ts')]} style={style} omittedFiles={4} />,
      )
      const omitted = container.querySelector('[data-fcc-omitted-notice]')
      expect(omitted, style).toHaveTextContent('4 more files changed in this turn are not listed.')
      expect(omitted, style).not.toHaveTextContent(FILE_ONLY)
      expect(omitted, style).not.toHaveTextContent(/kept/)
      const marker = container.querySelector(style === 'expanded' ? '[data-fcc-demoted-tag]' : '[data-fcc-pill-marker]')
      expect(marker, style).toHaveTextContent(FILE_ONLY)
      expect(marker, style).not.toHaveTextContent(/listed|kept/)
      expect(screen.getAllByText(FILE_ONLY), style).toHaveLength(1)
      expect(screen.queryByText(/not kept/), style).not.toBeInTheDocument()
      unmount()
    }
  })

  it('carries the new vocabulary in every catalogue and the old key in none', () => {
    // The marker key is renamed with its meaning, so a catalogue that still
    // held the old key would be the old wording under a new face.
    const languages = Object.keys(CATALOGS)
    expect(languages.length).toBeGreaterThanOrEqual(12)
    for (const language of languages) {
      const chips = (CATALOGS[language].translation as { components: { fileChangeChips: Record<string, string> } }).components.fileChangeChips
      expect(chips.diff_not_kept, language).toBeUndefined()
      expect(chips.file_only, language).toMatch(/\S/)
      const forms = Object.keys(chips).filter(k => k.startsWith('omitted_files_notice_'))
      expect(forms.length, language).toBeGreaterThan(0)
      for (const form of forms) {
        expect(chips[form], `${language}.${form}`).toContain('{{files}}')
        expect(chips[form], `${language}.${form}`).not.toBe(chips.file_only)
      }
    }
    const en = (CATALOGS.en.translation as { components: { fileChangeChips: Record<string, string> } }).components.fileChangeChips
    expect(en.file_only).toBe(FILE_ONLY)
    expect(Object.values(en).some(v => /not kept/.test(v))).toBe(false)
  })

  it('reads the notice from the omitted_files plural base and carries no omitted_writes key', () => {
    const chips = (enManual as { components: { fileChangeChips: Record<string, string> } }).components.fileChangeChips
    expect(chips.omitted_files_notice_one).toBe('{{files}} more file changed in this turn is not listed.')
    expect(chips.omitted_files_notice_other).toBe('{{files}} more files changed in this turn are not listed.')
    expect(Object.keys(chips).filter(k => k.startsWith('omitted_writes_notice'))).toEqual([])
    expect(pluralKeys).toContain('components.fileChangeChips.omitted_files_notice')
    expect(pluralKeys).not.toContain('components.fileChangeChips.omitted_writes_notice')
  })

  it('renders nothing for an empty file list, whatever the omitted-files count says', () => {
    // The gateway attaches the count only beside a non-empty list and the
    // budget always retains one entry, so this state has no producer and the
    // component draws nothing rather than a notice with no rows under it.
    for (const style of ['expanded', 'minimal'] as const) {
      const { container, unmount } = render(<FileChangeChips fileChanges={[]} style={style} omittedFiles={5} />)
      expect(container.firstChild, style).toBeNull()
      unmount()
    }
  })

  it('renders no omitted-files notice when the count is zero or the field is absent', () => {
    const { container, unmount } = render(<FileChangeChips fileChanges={[change('/a.ts', 'a', 'b')]} omittedFiles={0} />)
    expect(container.querySelector('[data-fcc-omitted-notice]')).toBeNull()
    expect(screen.queryByText(OMITTED_RE)).not.toBeInTheDocument()
    unmount()
    const absent = render(<FileChangeChips fileChanges={[change('/a.ts', 'a', 'b')]} />)
    expect(absent.container.querySelector('[data-fcc-omitted-notice]')).toBeNull()
  })

  it('renders a huge count as the plain formatted number, with no lower-bound marker', () => {
    // The gateway sends a plain count with no ceiling, so the notice has no
    // saturation branch and never appends a plus.
    render(<FileChangeChips fileChanges={[change('/a.ts', 'a', 'b')]} omittedFiles={9_876_543_210} />)
    expect(screen.getByText('9,876,543,210 more files changed in this turn are not listed.')).toBeInTheDocument()
    expect(screen.queryByText(/\+ more files/)).not.toBeInTheDocument()
  })

  it('ignores a malformed omitted-files field rather than drawing a notice for it', () => {
    for (const bad of ['7', -3, Number.NaN, {}, null]) {
      const { container, unmount } = render(<FileChangeChips fileChanges={[change('/a.ts', 'a', 'b')]} omittedFiles={bad} />)
      expect(container.querySelector('[data-fcc-omitted-notice]'), String(bad)).toBeNull()
      unmount()
    }
  })

  it('minimal style says the omitted-files notice once after the pills', () => {
    // The pill row is the other surface; it renders the sentence itself, so the
    // assertion lives here and not in a count across both surfaces.
    const { container } = render(
      <FileChangeChips fileChanges={[change('/a.ts', 'a', 'a\nb'), demoted('/b.ts')]} style="minimal" omittedFiles={3} />,
    )
    expect(screen.getAllByText(OMITTED_RE)).toHaveLength(1)
    expect(container.querySelector('[data-fcc-omitted-notice]')).toHaveTextContent(
      '3 more files changed in this turn are not listed.',
    )
    expect(container.querySelector('[data-fcc-omitted-notice]')?.closest('button')).toBeNull()
  })

  it('renders no notice row and no tag when nothing was demoted', () => {
    const { container } = render(<FileChangeChips fileChanges={[change('/kept.ts', 'a', 'a\nb')]} />)
    expect(container.querySelector('[data-fcc-turn-notice]')).toBeNull()
    expect(screen.queryByText(FILE_ONLY)).not.toBeInTheDocument()
  })

  it('keeps the per-file notice on its own row: that one really is about one file', () => {
    const large = { ...change('/large.ts', 'same', 'same'), truncated: true, snapshot_limit_chars: 200_000 }
    render(<FileChangeChips fileChanges={[large, demoted('/small.ts')]} />)
    expect(screen.getByTestId('fcc-row-/large.ts')).toHaveTextContent('Diff unavailable: file is too large to compare (over 200,000 characters).')
    expect(screen.getByTestId('fcc-row-/large.ts').querySelector('[data-fcc-demoted-tag]')).toBeNull()
    expect(screen.getByTestId('fcc-row-/small.ts')).toHaveTextContent(FILE_ONLY)
    expect(screen.getByTestId('fcc-row-/small.ts')).not.toHaveTextContent(/too large/)
  })

  it('tells the reader the files still open only when opening is wired', () => {
    // A demoted row's filename is a button exactly when onFileOpen exists;
    // the hint must not promise a click that does nothing.
    const onFileOpen = vi.fn()
    const { unmount } = render(<FileChangeChips fileChanges={[demoted('/small.ts')]} onFileOpen={onFileOpen} />)
    expect(screen.getByText(new RegExp(OPEN_HINT.replace('.', '\\.')))).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Open /small.ts in side panel' }))
    expect(onFileOpen).toHaveBeenCalledWith('/small.ts')
    unmount()
    render(<FileChangeChips fileChanges={[demoted('/small.ts')]} />)
    expect(screen.queryByText(/Click a file name/)).not.toBeInTheDocument()
  })

  it('minimal style says the turn-budget sentence once after the pills and never in one', () => {
    // The sentence is about the turn: three demoted pills share it, so the row
    // carries it once and each pill keeps only what differs, its filename.
    const { container } = render(
      <FileChangeChips fileChanges={[change('/kept.ts', 'a', 'a\nb'), demoted('/a.ts'), demoted('/b.ts'), demoted('/c.ts')]} style="minimal" />,
    )
    expect(screen.getAllByText(TURN_SENTENCE)).toHaveLength(1)
    const notice = container.querySelector('[data-fcc-turn-notice]')
    expect(notice).toBeInTheDocument()
    expect(notice?.closest('button')).toBeNull()
    for (const [path, name] of [['/a.ts', 'a.ts'], ['/b.ts', 'b.ts'], ['/c.ts', 'c.ts']]) {
      const pill = screen.getByRole('button', { name: path })
      expect(pill.querySelector('[data-fcc-pill-filename]'), path).toHaveTextContent(name)
      expect(pill.querySelector('[data-fcc-pill-marker]'), path).toHaveTextContent(FILE_ONLY)
    }
    // The marker is the tag's text at a muted size, never the bordered tag
    // itself: the tag belongs to the card.
    expect(container.querySelector('[data-fcc-demoted-tag]')).toBeNull()
    expect(screen.getAllByText(FILE_ONLY)).toHaveLength(3)
    expect(screen.queryByText(/too large to compare/)).not.toBeInTheDocument()
  })

  it('minimal style renders no turn-budget line when nothing was demoted', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[change('/kept.ts', 'a', 'a\nb'), { ...change('/large.ts', 'x', 'y'), truncated: true, snapshot_limit_chars: 200_000 }]} style="minimal" />,
    )
    expect(container.querySelector('[data-fcc-turn-notice]')).toBeNull()
    expect(screen.queryByText(TURN_SENTENCE)).not.toBeInTheDocument()
  })

  it('minimal style adds the open hint to its turn-budget line only when opening is wired', () => {
    const { unmount } = render(<FileChangeChips fileChanges={[demoted('/small.ts')]} style="minimal" onFileOpen={vi.fn()} />)
    expect(screen.getByText(new RegExp(OPEN_HINT.replace('.', '\\.')))).toBeInTheDocument()
    unmount()
    render(<FileChangeChips fileChanges={[demoted('/small.ts')]} style="minimal" />)
    expect(screen.getByText(TURN_SENTENCE)).toBeInTheDocument()
    expect(screen.queryByText(/Click a file name/)).not.toBeInTheDocument()
  })

  it('minimal style names a demoted pill\'s own file in the pill face', () => {
    // The filename is the one fact that differs between demoted pills, so it is
    // the pill's whole face; the turn-budget sentence is said once by the row.
    render(<FileChangeChips fileChanges={[demoted('/src/a.ts'), demoted('/src/b.ts')]} style="minimal" />)
    for (const [path, name] of [['/src/a.ts', 'a.ts'], ['/src/b.ts', 'b.ts']]) {
      const pill = screen.getByRole('button', { name: path })
      expect(pill.querySelector('[data-fcc-pill-filename]'), path).toHaveTextContent(name)
      expect(pill.textContent, path).toBe(`${name}${FILE_ONLY}`)
      expect(pill, path).toHaveClass('text-left')
    }
  })

  it('minimal style marks a demoted pill and leaves a healthy or per-file truncated pill unmarked', () => {
    // Without the marker a demoted pill is a bare filename that reads as a
    // healthy one. Muted and a size down, so the filename stays the face.
    const large = { ...change('/src/large.ts', 'x', 'y'), truncated: true, snapshot_limit_chars: 200_000 }
    render(<FileChangeChips fileChanges={[change('/src/kept.ts', 'a', 'a\nb'), large, demoted('/src/gone.ts')]} style="minimal" />)
    const marker = screen.getByRole('button', { name: '/src/gone.ts' }).querySelector('[data-fcc-pill-marker]')
    expect(marker).toHaveTextContent(FILE_ONLY)
    expect(marker).toHaveClass('text-muted', 'text-[10px]')
    expect(marker).not.toHaveClass('border', 'rounded-full')
    expect(screen.getByRole('button', { name: '/src/kept.ts' }).querySelector('[data-fcc-pill-marker]')).toBeNull()
    expect(screen.getByRole('button', { name: '/src/large.ts' }).querySelector('[data-fcc-pill-marker]')).toBeNull()
    expect(screen.getAllByText(FILE_ONLY)).toHaveLength(1)
  })

  it('minimal style names the kept file\'s stats pill only in a row that holds a demoted pill', () => {
    // A row where the demoted pills say which file they are about and the
    // stats pill does not leaves the reader guessing what the stats stand for,
    // so in that row the stats pill names its file too. Alone, the stats pill
    // keeps its stats-only face (asserted by the hover-label test below).
    const { unmount } = render(
      <FileChangeChips fileChanges={[change('/src/kept.ts', 'a', 'a\nb'), demoted('/src/gone.ts')]} style="minimal" />,
    )
    const kept = screen.getByRole('button', { name: '/src/kept.ts' })
    expect(kept.querySelector('[data-fcc-pill-filename]')).toHaveTextContent('kept.ts')
    expect(kept).toHaveTextContent('+1')
    expect(kept).not.toHaveClass('text-left')
    unmount()
    render(
      <FileChangeChips fileChanges={[change('/src/kept.ts', 'a', 'a\nb'), { ...change('/src/large.ts', 'x', 'y'), truncated: true, snapshot_limit_chars: 200_000 }]} style="minimal" />,
    )
    // A per-file truncated pill is not a demoted one: the stats pill beside it
    // stays anonymous, as it does with no truncated neighbour at all.
    expect(screen.getByRole('button', { name: '/src/kept.ts' }).querySelector('[data-fcc-pill-filename]')).toBeNull()
  })

  it('renders the per-file truncated pill as the sentence alone, with no filename element', () => {
    // This pill predates the turn budget and is not the budget's to restyle:
    // its face is the per-file sentence, its filename stays on hover, and it
    // carries no left-alignment override.
    render(<FileChangeChips fileChanges={[{ ...change('/src/large.ts', 'x', 'y'), truncated: true, snapshot_limit_chars: 200_000 }, demoted('/src/gone.ts')]} style="minimal" />)
    const pill = screen.getByRole('button', { name: '/src/large.ts' })
    expect(pill.textContent).toBe('Diff unavailable: file is too large to compare (over 200,000 characters).')
    expect(pill.querySelector('[data-fcc-pill-filename]')).toBeNull()
    expect(pill).not.toHaveClass('text-left')
  })

  it('opens a path-only minimal chip as the file, not an empty diff', () => {
    const onOpenDiff = vi.fn()
    const onFileOpen = vi.fn()
    render(
      <FileChangeChips
        fileChanges={[demoted('/small.ts')]}
        style="minimal"
        onOpenDiff={onOpenDiff}
        onFileOpen={onFileOpen}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '/small.ts' }))
    expect(onFileOpen).toHaveBeenCalledWith('/small.ts')
    expect(onOpenDiff).not.toHaveBeenCalled()
  })

  it('suppresses aggregate totals when any snapshot in the batch is truncated', () => {
    const truncated = { ...change('/large.ts', 'same', 'same'), truncated: true, snapshot_limit_chars: 200_000 }
    render(<FileChangeChips fileChanges={[truncated, change('/complete.ts', 'a', 'a\nb')]} />)
    expect(screen.getByText('2 files changed')).toBeInTheDocument()
    expect(screen.queryByText(/addition|removal/)).not.toBeInTheDocument()
  })

  it('opens a truncated minimal chip as the file, not an unavailable diff', () => {
    const onOpenDiff = vi.fn()
    const onFileOpen = vi.fn()
    const file = { ...change('/large.ts', 'before', 'after'), truncated: true, snapshot_limit_chars: 200_000 }
    render(
      <FileChangeChips
        fileChanges={[file]}
        style="minimal"
        onOpenDiff={onOpenDiff}
        onFileOpen={onFileOpen}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '/large.ts' }))
    expect(onFileOpen).toHaveBeenCalledWith('/large.ts')
    expect(onOpenDiff).not.toHaveBeenCalled()
  })

  it('lets a truncated minimal chip wrap within a narrow pane', () => {
    const file = { ...change('/large.ts', 'before', 'after'), truncated: true, snapshot_limit_chars: 200_000 }
    render(<FileChangeChips fileChanges={[file]} style="minimal" />)
    const chip = screen.getByLabelText('/large.ts')
    expect(chip).toHaveClass('max-w-full', 'min-w-0', 'min-h-[22px]', 'h-auto', 'whitespace-normal', 'py-1')
    expect(chip).not.toHaveClass('h-[22px]')
  })

  it('falls back to expanded for an unknown style value', () => {
    // Defensive default in the renderer map covers stale localStorage values
    // (e.g. legacy "tooltip"/"compact"/"full") until the migration runs.
    const { container } = render(
      <FileChangeChips
        fileChanges={[change('/legacy.ts', 'a', 'a\nb')]}
        // @ts-expect-error — intentional invalid style for the fallback path
        style="tooltip"
      />,
    )
    expect(container.querySelector('[data-testid="fcc-row-/legacy.ts"]')).toBeInTheDocument()
  })

  it('caps long lists at 8 rows behind a "Show N more" toggle', () => {
    const files = Array.from({ length: 11 }, (_, i) => change(`/f${i}.ts`, 'a', 'a\nb'))
    const { container } = render(<FileChangeChips fileChanges={files} />)
    expect(rows(container)).toHaveLength(8)
    // Header still reports the TRUE total.
    expect(screen.getByText('11 files changed')).toBeInTheDocument()
    // Expand reveals the remainder…
    fireEvent.click(screen.getByText('Show 3 more'))
    expect(rows(container)).toHaveLength(11)
    // …and collapses again.
    fireEvent.click(screen.getByText('Show less'))
    expect(rows(container)).toHaveLength(8)
  })

  it('does not cap lists at or below the threshold', () => {
    const files = Array.from({ length: 8 }, (_, i) => change(`/s${i}.ts`, 'a', 'b'))
    const { container } = render(<FileChangeChips fileChanges={files} />)
    expect(screen.queryByText(/Show \d+ more/)).not.toBeInTheDocument()
    expect(rows(container)).toHaveLength(8)
  })

  /* ── Header click routing. Pierre owns the header's inner nodes and its lazy
   *   chunk never resolves under vitest, so the real `[data-title]` cannot be
   *   clicked here — and a hand-stubbed `composedPath` does not survive React's
   *   dispatch, which resolves the target from that same path. The rule is
   *   therefore a pure function of the path and is tested as one; that the
   *   'toggle' verdict visibly collapses the row is Playwright's job. */
  const pathOf = (...sels: string[]) => sels.map(s => {
    const el = document.createElement('div')
    el.setAttribute(s, '')
    return el
  })

  it('routes a click on Pierre’s filename node to opening the file', () => {
    expect(headerClickAction(pathOf('data-title', 'data-diffs-header'))).toBe('open')
  })

  it('routes header whitespace to toggling, so the row has no dead zone', () => {
    expect(headerClickAction(pathOf('data-diffs-header'))).toBe('toggle')
  })

  it('routes lightweight header whitespace to toggling', () => {
    expect(headerClickAction(pathOf('data-fcc-header'))).toBe('toggle')
  })

  it('routes a lightweight filename to opening the file', () => {
    expect(headerClickAction(pathOf('data-fcc-filename', 'data-fcc-header'))).toBe('open')
  })

  it('ignores clicks below the header, so selecting code never collapses it', () => {
    expect(headerClickAction(pathOf('data-code'))).toBe('ignore')
    expect(headerClickAction([])).toBe('ignore')
  })

  it('ignores a filename node that is not inside a header', () => {
    // Order in the path is irrelevant, presence is what decides — but a title
    // without a header is not a header click at all.
    expect(headerClickAction(pathOf('data-title'))).toBe('ignore')
  })

  it('carries the full path as a tooltip so duplicate basenames stay distinguishable', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[change('/src/a/index.ts', 'a', 'b'), change('/src/b/index.ts', 'a', 'b')]} />,
    )
    const titles = [...container.querySelectorAll('[data-testid^="fcc-row-"]')].map(r => r.getAttribute('title'))
    expect(titles).toEqual(['/src/a/index.ts', '/src/b/index.ts'])
  })

  /* ── Minimal style: hand-rolled pills, no Pierre, so still fully assertable
   *   here — and the one style that still routes clicks to `onOpenDiff`. */
  it('minimal style hides the filename in the pill but exposes a hover label', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[change('/minimal.ts', 'a', 'a\nb')]} style="minimal" />,
    )
    const button = container.querySelector('button')
    expect(button?.textContent).not.toContain('minimal.ts')
    expect(container.textContent).toContain('minimal.ts')
    expect(button?.textContent).toContain('+1')
  })

  it('minimal style shows a no-changes caption when before === after', () => {
    render(<FileChangeChips fileChanges={[change('/same.ts', 'x', 'x')]} style="minimal" />)
    expect(screen.getByText('no changes')).toBeInTheDocument()
  })

  it('minimal style click triggers onOpenDiff with (path, after, before)', () => {
    const onOpenDiff = vi.fn()
    const { container } = render(
      <FileChangeChips
        fileChanges={[change('/min-click.ts', 'a', 'b')]}
        style="minimal"
        onOpenDiff={onOpenDiff}
      />,
    )
    fireEvent.click(container.querySelector('button')!)
    expect(onOpenDiff).toHaveBeenCalledWith('/min-click.ts', 'b', 'a')
  })

  it('does not throw on a minimal click when onOpenDiff is missing', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[change('/no-handler.ts', 'a', 'b')]} style="minimal" />,
    )
    expect(() => fireEvent.click(container.querySelector('button')!)).not.toThrow()
  })
})
