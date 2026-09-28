import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

/**
 * The composer hint is long (`Message Kiro Crew… (/command · @file · $skill)`)
 * and it is a label, so in a narrow split-view pane it is held to one line and
 * its cut tail fades out. The other placeholders this textarea shows are
 * sentences (gateway offline, stopping, recording), and those keep wrapping so
 * the box grows and the user reads the whole reason.
 *
 * jsdom computes no `::placeholder` box, so the rendered proof is the capture
 * harness (`website/scripts/capture-chatinput-placeholder-narrow.mjs`). What
 * these tests defend is the class contract that harness measures, and its scope.
 */
const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
}

const ONE_LINE = ['placeholder:whitespace-nowrap', 'placeholder:overflow-hidden', 'placeholder:[mask-image:', 'placeholder:[-webkit-mask-image:']

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('ChatInput placeholder stays on one line', () => {
  it('keeps the hint unwrapped, clipped and faded', () => {
    renderWithProviders(<ChatInput {...defaultProps} />)
    const textarea = screen.getByPlaceholderText(/\/command/)
    for (const token of ONE_LINE) expect(textarea.className).toContain(token)
  })

  it('lets a status placeholder wrap instead', () => {
    renderWithProviders(<ChatInput {...defaultProps} connected={false} />)
    const textarea = screen.getByPlaceholderText(/will not send/i)
    for (const token of ONE_LINE) expect(textarea.className).not.toContain(token)
  })

  it('leaves a caller-supplied placeholder wrapping too', () => {
    // `resolvedPlaceholder` is `placeholder || <the hint>`, so without the
    // `!placeholder` guard a caller's own sentence inherited the one-line rule.
    renderWithProviders(<ChatInput {...defaultProps} placeholder="Ask a side question about this selection" />)
    const textarea = screen.getByPlaceholderText(/Ask a side question/)
    for (const token of ONE_LINE) expect(textarea.className).not.toContain(token)
  })

  it('leaves the typed value wrapping as before', () => {
    // The one-line rule is scoped to the placeholder: an unprefixed `nowrap` or
    // `overflow-hidden` here would hide a draft the user is still typing.
    renderWithProviders(<ChatInput {...defaultProps} />)
    const classes = screen.getByPlaceholderText(/\/command/).className.split(/\s+/)
    expect(classes).not.toContain('whitespace-nowrap')
    expect(classes).not.toContain('overflow-hidden')
  })
})
