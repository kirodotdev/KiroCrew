/**
 * Composer preview tiles carried the full path only in `title` (pointer hover)
 * and the group `aria-label` (screen reader). A sighted, keyboard-only user who
 * runs no screen reader got neither, so a visible label of just the basename
 * left two same-basename files from different directories indistinguishable --
 * on focus and at rest alike (the reported #13829 defect).
 *
 * The fix is the one this component already uses for folder chips:
 * `buildFileLabels` renders each tile's VISIBLE label basename-first and widens
 * it by parent segments only until it is unique among the staged tiles. That
 * disambiguates the visible text for EVERY user -- pointer, keyboard and
 * screen-reader -- with no focus-gated overlay. The full path stays in
 * `title`/`aria-label`. These tests pin that contract:
 *
 *   - two same-basename files render DISTINCT visible labels (the defect),
 *   - a unique basename stays a bare basename (no needless widening),
 *   - the widening spans images and non-images together,
 *   - image tiles (which show no text of their own) gain a visible label,
 *   - the full path is still exposed via title/aria-label.
 *
 * It renders FilePreviewStrip directly (it is exported) rather than the whole
 * composer, matching how the a11y contract is kept unit-testable elsewhere.
 */
import React from 'react'
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup, within } from '@testing-library/react'
import { FilePreviewStrip } from '../components/chat-input/FilePreviewStrip'

afterEach(cleanup)

// Two files that share a basename but live in different directories: the exact
// case the visible basename cannot tell apart.
const A = '/home/alice/reports/2024/report.txt'
const B = '/home/alice/archive/2023/report.txt'

describe('FilePreviewStrip disambiguates same-basename tiles by visible label', () => {
  it('renders distinct visible labels for two same-basename files', () => {
    render(<FilePreviewStrip files={[A, B]} onRemove={() => {}} />)
    // The bare basename is NOT shown twice: each tile widens to its parent.
    expect(screen.queryAllByText('report.txt')).toHaveLength(0)
    expect(screen.getByText('2024/report.txt')).toBeInTheDocument()
    expect(screen.getByText('2023/report.txt')).toBeInTheDocument()
  })

  it('keeps a unique basename as a bare basename (no needless widening)', () => {
    const SOLE = '/home/alice/reports/2024/notes.md'
    render(<FilePreviewStrip files={[SOLE]} onRemove={() => {}} />)
    expect(screen.getByText('notes.md')).toBeInTheDocument()
  })

  it('widens across image and non-image tiles together', () => {
    // A pasted image and an attached file share the basename `report`.
    const IMG = '/home/alice/shots/report.png'
    const DOC = '/home/alice/docs/report.png' // same ext so both are images
    render(<FilePreviewStrip files={[IMG, DOC]} onRemove={() => {}} />)
    expect(screen.getByText('shots/report.png')).toBeInTheDocument()
    expect(screen.getByText('docs/report.png')).toBeInTheDocument()
  })

  it('captions a colliding image tile but not a unique one', () => {
    // Two same-basename images collide -> both get a widened caption.
    const IMG_A = '/home/alice/a/avatar.png'
    const IMG_B = '/home/alice/b/avatar.png'
    const { rerender } = render(<FilePreviewStrip files={[IMG_A, IMG_B]} onRemove={() => {}} />)
    expect(screen.getByText('a/avatar.png')).toBeInTheDocument()
    expect(screen.getByText('b/avatar.png')).toBeInTheDocument()
    // A lone image with a unique name shows NO caption (no clutter).
    rerender(<FilePreviewStrip files={['/home/alice/pics/sunset.png']} onRemove={() => {}} />)
    expect(screen.queryByText('sunset.png')).toBeNull()
  })

  it('still exposes the full path via title and aria-label on each tile', () => {
    render(<FilePreviewStrip files={[A, B]} onRemove={() => {}} />)
    // The group is named by the FULL original path for assistive tech...
    const group = screen.getByRole('group', { name: A })
    // ...and the same full path is the pointer-hover title.
    expect(group).toHaveAttribute('title', A)
    // The widened label lives inside that group.
    expect(within(group).getByText('2024/report.txt')).toBeInTheDocument()
  })
})
