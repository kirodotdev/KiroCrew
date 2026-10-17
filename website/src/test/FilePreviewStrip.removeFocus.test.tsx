/**
 * The image chip's remove button is hidden at rest (`opacity-0`) on a
 * hover-capable device and shown on hover. A keyboard user never hovers, so
 * without a focus rule they tab onto an invisible control -- the one the
 * "Remove it and try again" refusal copy points at.
 *
 * jsdom does not compute Tailwind's opacity, so this pins the class contract:
 * the button stays hidden at rest on hover-capable devices, and is revealed
 * when focus lands anywhere inside the chip (`group-focus-within/preview`) --
 * which includes the button itself, since it sits inside the chip -- with the
 * house `focus-ring-accent` cue.
 */
import React from 'react'
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { FilePreviewStrip } from '../components/chat-input/FilePreviewStrip'

afterEach(cleanup)

const IMG = '/home/alice/shots/screen.png'

function removeButton(): HTMLElement {
  render(<FilePreviewStrip files={[IMG]} onRemove={() => {}} />)
  return screen.getByRole('button', { name: 'Remove' })
}

describe('FilePreviewStrip image chip remove button', () => {
  it('sits inside the group/preview chip the focus-within rule keys on', () => {
    const btn = removeButton()
    expect(btn.closest('.group\\/preview')).not.toBeNull()
  })

  it('stays hidden at rest and keeps the hover and touch reveals', () => {
    const cls = removeButton().className.split(/\s+/)
    expect(cls).toContain('opacity-0')
    expect(cls).toContain('group-hover/preview:opacity-100')
    expect(cls).toContain('[@media(hover:none)]:opacity-100')
  })

  it('is revealed on keyboard focus inside the chip', () => {
    const cls = removeButton().className.split(/\s+/)
    expect(cls).toContain('group-focus-within/preview:opacity-100')
  })

  it('carries the house focus ring', () => {
    const cls = removeButton().className.split(/\s+/)
    expect(cls).toContain('focus-ring-accent')
  })
})
