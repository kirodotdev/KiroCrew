import { render, screen, fireEvent } from '@testing-library/react'
import type { FileContents } from '@pierre/diffs'
import DiffPanel from './DiffPanel'
import { copyToClipboard } from '../utils/clipboard'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn() }))

/* DiffPanel no longer owns a diff renderer: it hands a file PAIR to
 * `PierreFilePair`, which paints rows, gutters and its own file header inside a
 * shadow root behind a lazy chunk. Nothing Pierre draws is assertable from the
 * light DOM (see the notes in `src/test/DiffBlock.test.tsx` and
 * `src/test/FileChangeChips.test.tsx`), so what is left to test here is what
 * DiffPanel itself contributes:
 *
 *   - the identical-contents banner, which renders INSTEAD of a diff surface
 *   - the old/new `FileContents` it derives (a missing side is `null`, i.e. an
 *     add or a delete rather than an empty file)
 *   - the option mapping: `sideBySide` -> `diffStyle`, `lineNumbers` -> the
 *     inverted `disableLineNumbers`
 *   - the referential stability of both, which is the whole reason
 *     `PierreFilePair` can be `memo`'d
 *   - the footer path copy
 *
 * `../pierre` is stubbed to capture that hand-off. The stub stands in for a
 * FIRST-PARTY wrapper whose prop contract lives in this repo — it is not a
 * stand-in for Pierre's rendering, which is Playwright's job. Stubbing it also
 * keeps the lazy chunk out of the picture, so these cases need no awaits. */
const captured: {
  oldFile?: FileContents | null
  newFile?: FileContents | null
  options?: Record<string, unknown>
  renders: number
} = { renders: 0 }

vi.mock('../pierre', () => ({
  PierreFilePair: (props: {
    oldFile: FileContents | null
    newFile: FileContents | null
    options?: Record<string, unknown>
  }) => {
    captured.oldFile = props.oldFile
    captured.newFile = props.newFile
    captured.options = props.options
    captured.renders += 1
    return <div data-testid="zzq-file-pair" />
  },
}))

beforeEach(() => {
  vi.mocked(copyToClipboard).mockReset()
  captured.oldFile = undefined
  captured.newFile = undefined
  captured.options = undefined
  captured.renders = 0
})

describe('DiffPanel', () => {
  it('shows the identical banner instead of a diff surface when both sides match', () => {
    render(<DiffPanel filePath="zzq/a.ts" original="same" modified="same" />)
    expect(screen.getByText('Contents are identical')).toBeInTheDocument()
    expect(screen.queryByTestId('zzq-file-pair')).not.toBeInTheDocument()
  })

  it('two empty sides are a new-empty-file case, not an identical comparison', () => {
    // Both-empty is `original === modified`, so the banner would claim the two
    // sides were compared and matched. They carry no content to compare — the
    // diff surface renders instead, with nothing on either side.
    render(<DiffPanel filePath="zzq/a.ts" original="" modified="" />)
    expect(screen.getByTestId('zzq-file-pair')).toBeInTheDocument()
    expect(screen.queryByText('Contents are identical')).not.toBeInTheDocument()
    expect(captured.oldFile).toBeNull()
    expect(captured.newFile).toBeNull()
  })

  it('names both sides with the same path when both carry content', () => {
    render(<DiffPanel filePath="zzq/deep/a.ts" original="before" modified="after" />)
    expect(captured.oldFile).toEqual({ name: 'zzq/deep/a.ts', contents: 'before' })
    expect(captured.newFile).toEqual({ name: 'zzq/deep/a.ts', contents: 'after' })
  })

  it('sends a null old side for an added file and a null new side for a deleted one', () => {
    // Pierre reads a null side as add/delete; an empty-contents file would be
    // rendered as a modification of an empty file instead.
    const { unmount } = render(<DiffPanel filePath="zzq/added.ts" original="" modified="fresh" />)
    expect(captured.oldFile).toBeNull()
    expect(captured.newFile).toEqual({ name: 'zzq/added.ts', contents: 'fresh' })
    unmount()

    render(<DiffPanel filePath="zzq/gone.ts" original="was here" modified="" />)
    expect(captured.oldFile).toEqual({ name: 'zzq/gone.ts', contents: 'was here' })
    expect(captured.newFile).toBeNull()
  })

  it('maps sideBySide to diffStyle and lineNumbers to the inverted disable flag', () => {
    const { unmount } = render(
      <DiffPanel filePath="zzq/a.ts" original="a" modified="b" sideBySide={false} lineNumbers />,
    )
    expect(captured.options).toEqual({ diffStyle: 'unified', disableLineNumbers: false })
    unmount()

    // Defaults: split, and line numbers off (the side panel is narrow).
    render(<DiffPanel filePath="zzq/a.ts" original="a" modified="b" />)
    expect(captured.options).toEqual({ diffStyle: 'split', disableLineNumbers: true })
  })

  it('keeps the file pair and options referentially stable across an unrelated re-render', () => {
    // PierreFilePair is memo'd because a heavy file paints thousands of shadow
    // DOM rows. That bailout only holds while these props keep their identity,
    // so a re-render with identical inputs must not produce new objects.
    const { rerender } = render(<DiffPanel filePath="zzq/a.ts" original="a" modified="b" />)
    const first = { old: captured.oldFile, new: captured.newFile, options: captured.options }
    rerender(<DiffPanel filePath="zzq/a.ts" original="a" modified="b" />)
    expect(captured.oldFile).toBe(first.old)
    expect(captured.newFile).toBe(first.new)
    expect(captured.options).toBe(first.options)

    // …and a changed input does yield a fresh object, so the memo is not stale.
    rerender(<DiffPanel filePath="zzq/a.ts" original="a" modified="b" sideBySide={false} />)
    expect(captured.options).not.toBe(first.options)
    expect(captured.options).toEqual({ diffStyle: 'unified', disableLineNumbers: true })
  })

  it('the footer copies the file path', () => {
    render(<DiffPanel filePath="zzq/deep/a.ts" original="a" modified="b" />)
    fireEvent.click(screen.getByTitle('Click to copy path'))
    expect(copyToClipboard).toHaveBeenCalledWith('zzq/deep/a.ts')
  })

  it('the footer shows the path even when the identical banner replaced the diff', () => {
    render(<DiffPanel filePath="zzq/same.ts" original="x" modified="x" />)
    expect(screen.getByText('zzq/same.ts')).toBeInTheDocument()
    fireEvent.click(screen.getByTitle('Click to copy path'))
    expect(copyToClipboard).toHaveBeenCalledWith('zzq/same.ts')
  })
})
