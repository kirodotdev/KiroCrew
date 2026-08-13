import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

// Pins the resize-notice contract: downscale details render as an accent pill
// IN FLOW UNDER the attachment chip, with a styled hover tooltip showing the
// dimensions — not as a banner above the transcript, and not overlaid on the
// thumbnail. The chip's width comes from the image's aspect ratio, so an
// overlaid pill has no width to fit into (a portrait screenshot gives it a 48px
// chip against a 105px widest catalog value) and ends up either spilling onto
// the neighbouring chip or covering the thumbnail it annotates.

const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
}

const IMG = '/tmp/uploads/big-test-image.png'
const OTHER = '/tmp/uploads/untouched.png'
const RESIZE = { name: 'big-test-image.png', fromW: 2400, fromH: 3200, toW: 1176, toH: 1568, fromBytes: 900000, toBytes: 300000 }

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('ChatInput attachment resize badge', () => {
  it('shows a RESIZED pill on a downscaled image chip', () => {
    renderWithProviders(
      <ChatInput {...defaultProps} pendingFiles={[IMG]} resizedInfo={{ [IMG]: RESIZE }} />,
    )
    const badge = screen.getByText('RESIZED')
    expect(badge).toBeInTheDocument()
    expect(badge).toHaveAttribute('aria-label', 'Resized to fit model limits: 2400×3200 to 1176×1568')
  })

  it('lays the pill out in flow under the thumbnail, never over it', () => {
    renderWithProviders(
      <ChatInput {...defaultProps} pendingFiles={[IMG]} resizedInfo={{ [IMG]: RESIZE }} />,
    )
    const badge = screen.getByText('RESIZED')
    // Not overlaid: an absolutely positioned pill is what covered the thumbnail.
    expect(badge.className).not.toMatch(/\babsolute\b/)
    // The chip stacks image then pill, so the chip is as wide as the wider of
    // the two rather than clipping either.
    const chip = badge.parentElement as HTMLElement
    expect(chip.className).toMatch(/\bflex-col\b/)
    expect(chip).toContainElement(screen.getByAltText(IMG))
    // nowrap is what makes the CHIP grow instead of the pill breaking into the
    // multi-line block that covered the thumbnail in per-character scripts.
    expect(badge.className).toMatch(/\bwhitespace-nowrap\b/)
  })

  it('opens a tooltip with the dimensions on hover and closes on leave', () => {
    renderWithProviders(
      <ChatInput {...defaultProps} pendingFiles={[IMG]} resizedInfo={{ [IMG]: RESIZE }} />,
    )
    const badge = screen.getByText('RESIZED')
    fireEvent.mouseEnter(badge)
    const tip = screen.getByRole('tooltip')
    expect(tip).toHaveTextContent('Resized to fit model limits')
    expect(tip).toHaveTextContent('2400×3200 → 1176×1568')
    fireEvent.mouseLeave(badge)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('opens the tooltip on keyboard focus too', () => {
    renderWithProviders(
      <ChatInput {...defaultProps} pendingFiles={[IMG]} resizedInfo={{ [IMG]: RESIZE }} />,
    )
    fireEvent.focus(screen.getByText('RESIZED'))
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
  })

  it('renders no badge for images that were not resized', () => {
    renderWithProviders(
      <ChatInput {...defaultProps} pendingFiles={[OTHER]} resizedInfo={{ [IMG]: RESIZE }} />,
    )
    expect(screen.queryByText('RESIZED')).not.toBeInTheDocument()
  })

  it('badges only the resized chip when mixed with untouched files', () => {
    renderWithProviders(
      <ChatInput {...defaultProps} pendingFiles={[IMG, OTHER]} resizedInfo={{ [IMG]: RESIZE }} />,
    )
    expect(screen.getAllByText('RESIZED')).toHaveLength(1)
  })

  it('renders no badge when resizedInfo is absent entirely', () => {
    renderWithProviders(<ChatInput {...defaultProps} pendingFiles={[IMG]} />)
    expect(screen.queryByText('RESIZED')).not.toBeInTheDocument()
  })

  // A 31px-wide chip cannot be told apart from the next 31px-wide chip. This is
  // a recognisability floor, separate from the overlap fix — with the pill in
  // flow the overlap is 0 at any width — and bg-bg-hover is what makes the
  // letterbox the floor creates read as a tile instead of a partly-empty frame.
  // No ceiling is asserted: capping wide images has no reported defect behind it.
  it('gives the thumbnail a width floor and a letterbox backing', () => {
    renderWithProviders(
      <ChatInput {...defaultProps} pendingFiles={[IMG]} resizedInfo={{ [IMG]: RESIZE }} />,
    )
    const thumb = screen.getByAltText(IMG)
    expect(thumb.className).toMatch(/\bmin-w-12\b/)
    expect(thumb.className).toMatch(/\bobject-contain\b/)
    expect(thumb.className).toMatch(/\bbg-bg-hover\b/)
  })

  // A chip carrying a pill is taller, so the wrapper's height compensation has
  // to know: without this the extra row eats into the textarea.
  it('reserves the taller strip height only when a staged image was resized', () => {
    // A persisted manual height is what activates the minHeight compensation.
    localStorage.setItem('mc-input-height', '300')
    const outerOf = () => screen.getByLabelText('Message input').closest('.input-area') as HTMLElement
    const { rerender } = renderWithProviders(
      <ChatInput {...defaultProps} pendingFiles={[IMG]} />,
    )
    // FILE_PREVIEW_H (81) + INPUT_DRAG_MIN_H (93)
    expect(outerOf().style.minHeight).toBe('174px')
    rerender(<ChatInput {...defaultProps} pendingFiles={[IMG]} resizedInfo={{ [IMG]: RESIZE }} />)
    // FILE_PREVIEW_H_RESIZED (101): the pill's 18px plus the 2px gap.
    expect(outerOf().style.minHeight).toBe('194px')
  })
})
