import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

// Pins the resize-notice contract: downscale details render as an accent
// RESIZED pill ON the attachment chip, with a styled hover tooltip showing
// the dimensions — not as a banner pinned above the transcript.

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
})
