import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import ScheduleTemplateGallery from '../components/ScheduleTemplateGallery'
import { SCHEDULE_PRESETS } from '../utils/schedulePresets'

describe('ScheduleTemplateGallery writes badge', () => {
  it('paints the "Writes to your repos" badge with the warn-tint ink, not the solid-fill ink', () => {
    // At least one preset writes; the gallery lists every preset, so the badge
    // renders for it. If this ever becomes false the test is wrong, not the UI.
    expect(SCHEDULE_PRESETS.some(p => p.writes)).toBe(true)

    render(<ScheduleTemplateGallery open onClose={vi.fn()} onPick={vi.fn()} />)

    // There is one badge per writes-preset; they are identical, so pin the first.
    const badge = screen.getAllByText('Writes to your repos')[0].closest('span')!
    // `text-warn-text` is the warn hue meant for a `bg-warn-subtle` wash. The
    // old `text-warn-fg` is black in the dark themes and vanishes over the 12%
    // tint — dark-on-dark (#21450 / #21335).
    expect(badge).toHaveClass('bg-warn-subtle', 'text-warn-text')
    expect(badge).not.toHaveClass('text-warn-fg')
  })
})
