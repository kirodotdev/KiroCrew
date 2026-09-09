/**
 * An open file row must not lose its patch when the message's file-change
 * payload is replaced underneath it.
 *
 * Pierre re-initializes the diff view whenever its inputs change identity, and
 * its cache key is derived from the file CONTENTS — so handing it new contents
 * re-diffs asynchronously and paints nothing until that lands. Observed on a
 * phone as an expanded diff going blank for ~700ms while the chevron still read
 * expanded. The row therefore pins its inputs while Pierre is mounted.
 *
 * The pin is only observable through the props handed to Pierre, so this mocks
 * that component to record them, and renders the header-prefix slot because that
 * is where the chevron lives.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, cleanup, act } from '@testing-library/react'

import { ROW_ANIM_MS } from '../components/fileChangeChipsCss'

type Rec = { oldContents: string; newContents: string }
const hoisted = vi.hoisted(() => ({ seen: [] as Rec[], headers: [] as string[] }))

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreFilePair: ({ oldFile, newFile, fallbackHeader, renderHeaderPrefix }: {
    oldFile: { contents: string } | null
    newFile: { contents: string } | null
    fallbackHeader?: () => React.ReactNode
    renderHeaderPrefix?: () => React.ReactNode
  }) => {
    hoisted.seen.push({ oldContents: oldFile?.contents ?? '', newContents: newFile?.contents ?? '' })
    // The real wrapper renders this inside the WarmSwap fallback; recording its
    // rendered text is how a pure test can see the strip is there at all.
    const hdr = fallbackHeader?.()
    hoisted.headers.push(hdr ? JSON.stringify(hdr, (k, v) => (k === '_owner' ? undefined : v)) : '')
    return <div data-testid="pierre-pair">{renderHeaderPrefix?.()}</div>
  },
}))

import FileChangeChips from '../components/FileChangeChips'

const latest = () => hoisted.seen[hoisted.seen.length - 1]
const chip = (after: string) => [{ path: '/a.ts', before: '', after }]
/** The chevron — the only control rendered into Pierre's header prefix slot. */
const toggle = (c: HTMLElement) => c.querySelector<HTMLElement>('[data-testid^="fcc-toggle-"]')!

beforeEach(() => {
  hoisted.seen.length = 0
  hoisted.headers.length = 0
  cleanup()
})

describe('an open row pins its diff inputs', () => {
  it('keeps the contents it opened with when the payload is replaced', () => {
    const { container, rerender } = render(<FileChangeChips fileChanges={chip('one\ntwo')} />)
    fireEvent.click(toggle(container))
    expect(latest().newContents).toBe('one\ntwo')

    // Same message, a differently-serialised payload — what a trailing-newline
    // disagreement between two producers of the same snapshot looks like.
    rerender(<FileChangeChips fileChanges={chip('one\ntwo\n')} />)

    expect(latest().newContents).toBe('one\ntwo')
  })

  it('adopts the newer contents on the next open', () => {
    const { container, rerender } = render(<FileChangeChips fileChanges={chip('one\ntwo')} />)
    fireEvent.click(toggle(container))
    rerender(<FileChangeChips fileChanges={chip('one\ntwo\nthree')} />)
    expect(latest().newContents).toBe('one\ntwo')

    // Collapse and let the collapse animation finish. Reopening INSIDE that
    // window deliberately keeps the pin — Pierre is still mounted there, so
    // re-initializing would blank exactly what this guards. Once the window
    // closes Pierre unmounts, and re-initializing on the next open is
    // unavoidable anyway, so the fresh contents cost nothing.
    vi.useFakeTimers()
    try {
      fireEvent.click(toggle(container))
      act(() => { vi.advanceTimersByTime(ROW_ANIM_MS + 20) })
      fireEvent.click(toggle(container))
    } finally {
      vi.useRealTimers()
    }

    expect(latest().newContents).toBe('one\ntwo\nthree')
  })

  it('renders the current contents on a first open, not a stale pin', () => {
    const { container, rerender } = render(<FileChangeChips fileChanges={chip('one')} />)
    rerender(<FileChangeChips fileChanges={chip('one\ntwo')} />)
    fireEvent.click(toggle(container))
    expect(latest().newContents).toBe('one\ntwo')
  })
})

describe('a row with nothing to show offers no disclosure', () => {
  // A file past the backend's per-file snapshot cap has both sides truncated to
  // the SAME prefix, so a real edit beyond the cut arrives as before === after.
  // Pierre can only paint an empty diff from that, and the row previously spent
  // a tap replacing its header with the warm fallback and then collapsing to
  // nothing -- a flash-and-vanish indistinguishable from a broken diff.
  const identical = [{ path: '/huge.tsx', before: 'same bytes', after: 'same bytes' }]

  it('renders no toggle at all', () => {
    const { container } = render(<FileChangeChips fileChanges={identical} />)
    expect(container.querySelector('[data-testid^="fcc-toggle-"]')).toBeNull()
    // The header itself is still there: the file was touched and the row says so.
    expect(container.querySelector('[data-testid="fcc-header-/huge.tsx"]')).toBeTruthy()
  })

  it('never mounts Pierre, even when the header is clicked', () => {
    const { container } = render(<FileChangeChips fileChanges={identical} />)
    const header = container.querySelector<HTMLElement>('[data-fcc-header]')!
    fireEvent.click(header)
    expect(hoisted.seen).toHaveLength(0)
  })

  it('still offers a real diff on a row that has one', () => {
    const { container } = render(<FileChangeChips fileChanges={chip('one\ntwo')} />)
    expect(container.querySelector('[data-testid^="fcc-toggle-"]')).toBeTruthy()
  })
})

describe('the row keeps its header across the Pierre handoff', () => {
  it('supplies a fallback header, so the strip survives the warm window', () => {
    // Opening swaps this row's own header out for Pierre's, and Pierre's arrives
    // only once the impl paints. Without a header in the warm fallback the row
    // loses its filename, counts and disclosure control for that window, which
    // reads as the row flashing away and coming back.
    const { container } = render(<FileChangeChips fileChanges={chip('one\ntwo')} />)
    fireEvent.click(toggle(container))
    const header = hoisted.headers[hoisted.headers.length - 1]
    expect(header).toBeTruthy()
    // It must be the SAME strip, not a placeholder: the filename identifies the
    // row and the toggle lets the reader close it again mid-warm.
    expect(header).toContain('a.ts')
  })
})
