/**
 * A row whose content pair did not travel still renders, and still counts.
 *
 * Measured on a real 9,611-message session: 1% of rows carried 69% of the
 * transcript's bytes, entirely in `meta.file_changes` before/after pairs (33-200
 * KB per side). Server CPU for one page was ~305ms of a 2.6s wait, so the rest was
 * bytes on the wire -- which makes payload the only lever on the dominant term.
 * The server now sends a capped patch instead once a pair crosses a size
 * threshold (130x smaller on the heaviest pairs), so `before`/`after` are OPTIONAL
 * on the wire and this file pins that the client survives their absence.
 *
 * The stats matter as much as the render: the collapsed card is a stats-only
 * pill, so a row that silently reported 0/0 would look like a change that touched
 * nothing -- the exact defect the patch field was introduced to fix.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/react'
import FileChangeChips from '../components/FileChangeChips'
import type { FileChangeEntry } from '../components/FileChangeChips'
import { i18nT } from '../i18n/t'

vi.mock('../pierre', () => ({
  PierreFilePair: () => <div data-testid="pierre-pair" />,
  PierrePatch: ({ patch }: { patch: string }) => <div data-testid="pierre-patch">{patch}</div>,
  PierreCode: () => null,
  setPierreToggle: () => {},
}))

const PATCH = [
  '--- a/src/big.ts',
  '+++ b/src/big.ts',
  '@@ -3400,3 +3400,4 @@',
  ' keep',
  '-gone',
  '+added one',
  '+added two',
  '',
].join('\n')

function entry(over: Partial<FileChangeEntry> = {}): FileChangeEntry {
  return { path: 'src/big.ts', patch: PATCH, ...over }
}

describe('a file-change row with no content pair', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('renders the card without before/after present', () => {
    // A required-field type would have made this a compile error; it reaches the
    // client as absent JSON keys, so the guard has to be a render.
    const { container } = render(<FileChangeChips fileChanges={[entry()]} />)
    expect(container.textContent).toContain('big.ts')
  })

  it('takes its ADD/DELETE counts from the patch, not from an empty pair', () => {
    // PATCH carries two additions and one deletion. Falling back to
    // countLines('', '') renders the translated "no changes" instead, which is a
    // change that touched nothing -- the exact defect the patch field exists to
    // fix. Asserting the RENDERED counts rather than any digit on the page: a
    // body-wide /2/ match passes on a line number and pins nothing (verified by
    // mutation -- it did).
    const { container } = render(<FileChangeChips fileChanges={[entry()]} />)
    const text = container.textContent || ''
    expect(text).toContain('+2')
    expect(text).toContain('-1')
    expect(text).not.toContain(i18nT('components.fileChangeChips.no_changes'))
  })

  it('still renders when BOTH the pair and the patch are absent', () => {
    // Defensive: an older row, or a producer that sent neither. It must not throw
    // -- a crash here takes the whole transcript down, not just one row.
    expect(() =>
      render(<FileChangeChips fileChanges={[{ path: 'src/x.ts' }]} />),
    ).not.toThrow()
  })

  it('keeps working for a row that DOES carry a pair', () => {
    // The threshold means small changes still travel as a pair; the two shapes
    // coexist in one card and neither may break the other.
    const both: FileChangeEntry[] = [
      entry(),
      { path: 'src/small.ts', before: 'a\n', after: 'a\nb\n' },
    ]
    const { container } = render(<FileChangeChips fileChanges={both} />)
    expect(container.textContent).toContain('small.ts')
    expect(container.textContent).toContain('big.ts')
  })
})
