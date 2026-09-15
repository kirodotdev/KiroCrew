/**
 * SessionColorField — the crew editor's session-colour control.
 *
 * The quick-pick row is the point of these tests. A crew's `session_color` is a
 * stored HEX, so a swatch commits exactly the literal it shows and the active
 * ring is matched by hex — not by a palette index that would re-derive with the
 * theme. Those are the properties a future refactor could quietly drop (most
 * plausibly by swapping in the sidebar's generated palette), so they are pinned
 * here rather than left to inspection.
 *
 * The row renders the shared FOLDER_COLOR_PALETTE rather than a preset list of
 * its own, so these assertions deliberately read that catalog instead of
 * hard-coding hexes: adding a colour to it should extend this row for free, and
 * a test that restated the list would just be a second copy of the thing the
 * field exists to avoid.
 *
 * No mocking and no providers: the field is a plain form control over a fixed
 * catalog, which is the reason it was built that way.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import { SessionColorField } from '../pages/KiroCrewAgentsPage'
import { FOLDER_COLOR_PALETTE } from '../components/folderColorCatalog'

/** The quick-pick swatches only — excludes the native `type=color` input and Clear. */
const swatches = () => FOLDER_COLOR_PALETTE.map(e => screen.getByRole('button', { name: e.label() }))
const pressed = () => swatches().filter(b => b.getAttribute('aria-pressed') === 'true')

describe('SessionColorField quick-pick row', () => {
  it('offers one swatch per catalog colour, labelled by the catalog', () => {
    render(<SessionColorField value="" onChange={() => {}} />)
    expect(swatches()).toHaveLength(FOLDER_COLOR_PALETTE.length)
  })

  it('the catalog hexes are lowercase 6-digit, as the hex match assumes', () => {
    // The field compares `value.toLowerCase()` against these literally, so an
    // uppercase or shorthand entry would silently never ring.
    for (const { value } of FOLDER_COLOR_PALETTE) expect(value).toMatch(/^#[0-9a-f]{6}$/)
  })

  it('commits the exact catalog hex when a swatch is clicked', () => {
    const onChange = vi.fn()
    render(<SessionColorField value="" onChange={onChange} />)
    const target = FOLDER_COLOR_PALETTE[2]
    fireEvent.click(screen.getByRole('button', { name: target.label() }))
    expect(onChange).toHaveBeenCalledWith(target.value)
  })

  it('marks the swatch holding the current value, case-insensitively', () => {
    const target = FOLDER_COLOR_PALETTE[1]
    render(<SessionColorField value={target.value.toUpperCase()} onChange={() => {}} />)
    expect(pressed()).toHaveLength(1)
    expect(pressed()[0]).toHaveAccessibleName(target.label())
  })

  it('marks no swatch for a custom colour outside the catalog', () => {
    render(<SessionColorField value="#0a0b0c" onChange={() => {}} />)
    expect(pressed()).toHaveLength(0)
  })

  it('marks no swatch when the crew has no colour', () => {
    render(<SessionColorField value="" onChange={() => {}} />)
    expect(pressed()).toHaveLength(0)
  })

  it('does not add a second way to unset the colour', () => {
    // Clear already owns "no colour"; a slashed cell in the row would be a
    // duplicate control for one action.
    render(<SessionColorField value={FOLDER_COLOR_PALETTE[0].value} onChange={() => {}} />)
    expect(screen.getByRole('button', { name: /clear/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /no colou?r/i })).not.toBeInTheDocument()
  })

  it('still commits a hex typed into the field', () => {
    const onChange = vi.fn()
    render(<SessionColorField value="" onChange={onChange} />)
    fireEvent.change(screen.getByLabelText(/hex/i), { target: { value: '#ABCDEF' } })
    expect(onChange).toHaveBeenCalledWith('#abcdef')
  })
})
