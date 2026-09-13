/**
 * A row whose change could not fit the before/after pair carries a `patch`.
 *
 * The backend emits it for exactly one case: both snapshots are cut from the
 * start of the file, so an edit past the per-file cap leaves them byte-identical
 * and the change would otherwise be invisible. When a patch is present it is the
 * authority for the row — the counts come from its own +/- markers, it renders as
 * a unified patch (whose hunk headers carry the true line numbers), and the row
 * must NOT fall into the "nothing to show" state its equal pair would imply.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, cleanup } from '@testing-library/react'

const hoisted = vi.hoisted(() => ({
  patches: [] as string[],
  pairs: 0,
  patchOptions: [] as Record<string, unknown>[],
}))

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierrePatch: ({ patch, options }: { patch: string; options?: Record<string, unknown> }) => {
    hoisted.patches.push(patch)
    hoisted.patchOptions.push(options ?? {})
    return <div data-testid="pierre-patch">{patch}</div>
  },
  PierreFilePair: () => {
    hoisted.pairs += 1
    return <div data-testid="pierre-pair" />
  },
}))

import FileChangeChips from '../components/FileChangeChips'

// The shape the backend produces past the cap: an EQUAL pair plus the patch.
const PATCH = [
  '--- a/huge.tsx',
  '+++ b/huge.tsx',
  '@@ -6901,3 +6901,4 @@',
  ' context',
  '-gone',
  '+added one',
  '+added two',
  '',
].join('\n')
const capped = [{ path: '/huge.tsx', before: 'same prefix', after: 'same prefix', patch: PATCH }]
const row = (c: HTMLElement) => c.querySelector<HTMLElement>('[data-testid^="fcc-toggle-"]')

beforeEach(() => {
  hoisted.patches.length = 0
  hoisted.patchOptions.length = 0
  hoisted.pairs = 0
  cleanup()
})

describe('a row carrying a patch', () => {
  it('stays expandable even though its before/after pair is equal', () => {
    const { container } = render(<FileChangeChips fileChanges={capped} />)
    expect(row(container)).toBeTruthy()
  })

  it('renders the patch rather than a file pair', () => {
    const { container } = render(<FileChangeChips fileChanges={capped} />)
    fireEvent.click(row(container)!)
    expect(hoisted.patches).toHaveLength(1)
    expect(hoisted.patches[0]).toContain('@@ -6901,3 +6901,4 @@')
    expect(hoisted.pairs).toBe(0)
  })

  it('keeps its own header, so Pierre must not draw a second one', () => {
    const { container } = render(<FileChangeChips fileChanges={capped} />)
    fireEvent.click(row(container)!)
    expect(container.querySelector('[data-testid="fcc-header-/huge.tsx"]')).toBeTruthy()
    expect(hoisted.patchOptions[0].disableFileHeader).toBe(true)
  })

  it('counts from the patch markers, not from the equal pair', () => {
    // countLines('same prefix','same prefix') is 0/0; the patch is -1/+2.
    const { container } = render(<FileChangeChips fileChanges={capped} />)
    const text = container.textContent ?? ''
    expect(text).toContain('2')
    expect(text).not.toMatch(/no changes/i)
  })

  it('an equal pair with NO patch is still not expandable', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[{ path: '/x.ts', before: 'a', after: 'a' }]} />,
    )
    expect(row(container)).toBeNull()
  })
})
