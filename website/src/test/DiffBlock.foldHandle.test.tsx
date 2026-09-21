/**
 * The fold chevron DiffBlock overlays on Pierre's file header must be reachable.
 *
 * Pierre paints its `default` header `position: relative; z-index: 2` inside a
 * shadow root, which opens no stacking context of its own — so a chevron at
 * `z-0` in the same wrapper was painted over and every click landed on the
 * header instead. A card opened from its chip then had no way back. The
 * geometry lives in Playwright (scripts/capture-diff-fold-default.mjs); what
 * jsdom can pin is the two halves of the fix: the handle's stacking class, and
 * the header gutter DiffBlock hands Pierre so the filename clears the chevron.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'

const hoisted = vi.hoisted(() => ({
  options: [] as { unsafeCSS?: string }[],
}))

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierrePatch: ({ options }: { options: { unsafeCSS?: string } }) => {
    hoisted.options.push(options)
    return <div data-testid="pierre-patch" />
  },
}))

import DiffBlock from '../components/DiffBlock'

const diff = `--- a/file.ts\n+++ b/file.ts\n@@ -1,1 +1,1 @@\n-old\n+new`
const latest = () => hoisted.options[hoisted.options.length - 1]

beforeEach(() => {
  hoisted.options.length = 0
  localStorage.clear()
})

describe('DiffBlock fold handle', () => {
  it('stacks the chevron above Pierre header (z-index 2) so it takes the click', () => {
    render(<DiffBlock code={diff} complete onFold={() => {}} />)
    const handle = screen.getByRole('button', { name: 'Hide diff' })
    expect(handle.className).toMatch(/\bz-10\b/)
    expect(handle.className).not.toMatch(/\bz-0\b/)
  })

  it('pads Pierre header past the chevron only when the handle is present', () => {
    render(<DiffBlock code={diff} complete onFold={() => {}} />)
    expect(latest().unsafeCSS).toContain('[data-diffs-header="default"]{padding-inline-start:32px}')

    hoisted.options.length = 0
    render(<DiffBlock code={diff} complete />)
    expect(latest().unsafeCSS).not.toContain('padding-inline-start:32px')
  })
})
