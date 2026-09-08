/**
 * Feature Previews "See what it looks like" dialog (Settings > Developer).
 *
 * Contract under test:
 * - a preview WITH captures offers a "See what it looks like" button; one WITHOUT captures
 *   offers nothing at all (no button, so no dialog with an empty frame)
 * - the dialog shows the current theme's capture, the summary, the
 *   where-to-find line, and pages through multiple captures
 * - the switch inside the dialog IS the card's switch: flipping either one
 *   writes the same preview flag, and the other reflects it
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, within, cleanup } from '@testing-library/react'
import { useState } from 'react'
import { MemoryRouter } from 'react-router-dom'

import { FeaturePreviewIntroButton, type FeaturePreviewIntro } from '../components/FeaturePreviewIntroDialog'
import { FeaturePreviewsSection } from '../pages/settings/FeaturePreviewsSection'

const INTRO: FeaturePreviewIntro = {
  summary: 'What it does.',
  whereToFind: 'Under the New button.',
  media: [
    { kind: 'image', light: '/app-assets/feature-previews/a-light.png', dark: '/app-assets/feature-previews/a-dark.png', caption: 'First capture' },
    { kind: 'gif', light: '/app-assets/feature-previews/b-light.gif', dark: '/app-assets/feature-previews/b-dark.gif', caption: 'Second capture' },
  ],
}

function Harness({ intro, initial = false }: { intro: FeaturePreviewIntro; initial?: boolean }) {
  // Stand-in for the card: owns the flag state the way the section does.
  const [on, setOn] = useState(initial)
  return (
    <>
      <span data-testid="outer">{on ? 'on' : 'off'}</span>
      <FeaturePreviewIntroButton title="Sample preview" intro={intro} checked={on} onChange={setOn} />
    </>
  )
}

describe('FeaturePreviewIntroButton', () => {
  afterEach(() => { cleanup(); document.documentElement.removeAttribute('data-theme') })

  it('renders nothing when the preview has no captures', () => {
    render(<Harness intro={{ ...INTRO, media: [] }} />)
    expect(screen.queryByRole('button', { name: /looks like/ })).not.toBeInTheDocument()
  })

  it('offers a See-what-it-looks-like button named for the preview when it has captures', () => {
    render(<Harness intro={INTRO} />)
    expect(screen.getByRole('button', { name: 'See what Sample preview looks like' })).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('opens a dialog with the capture, summary and where-to-find copy', () => {
    render(<Harness intro={INTRO} />)
    fireEvent.click(screen.getByRole('button', { name: 'See what Sample preview looks like' }))
    const dialog = screen.getByRole('dialog', { name: 'Sample preview' })
    expect(within(dialog).getByText('What it does.')).toBeInTheDocument()
    expect(within(dialog).getByText('Under the New button.')).toBeInTheDocument()
    const img = within(dialog).getByRole('img', { name: 'First capture' })
    expect(img).toHaveAttribute('src', '/app-assets/feature-previews/a-light.png')
  })

  it('picks the dark capture when the document theme is dark', () => {
    document.documentElement.setAttribute('data-theme', 'dark')
    render(<Harness intro={INTRO} />)
    fireEvent.click(screen.getByRole('button', { name: 'See what Sample preview looks like' }))
    expect(screen.getByRole('img', { name: 'First capture' })).toHaveAttribute('src', '/app-assets/feature-previews/a-dark.png')
  })

  it('pages through captures and marks a GIF as one', () => {
    render(<Harness intro={INTRO} />)
    fireEvent.click(screen.getByRole('button', { name: 'See what Sample preview looks like' }))
    const dialog = screen.getByRole('dialog')
    // A still is badged too: the frame must say "picture" before a reader
    // clicks a photographed control that sits next to the real footer switch.
    expect(within(dialog).getByText('Screenshot')).toBeInTheDocument()
    expect(within(dialog).queryByText('GIF')).not.toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Next capture' }))
    expect(within(dialog).getByRole('img', { name: 'Second capture' })).toHaveAttribute('src', '/app-assets/feature-previews/b-light.gif')
    expect(within(dialog).getByText('GIF')).toBeInTheDocument()
    expect(within(dialog).getByRole('tab', { name: 'Capture 2 of 2' })).toHaveAttribute('aria-selected', 'true')
    // Wraps: next from the last returns to the first.
    fireEvent.click(within(dialog).getByRole('button', { name: 'Next capture' }))
    expect(within(dialog).getByRole('img', { name: 'First capture' })).toBeInTheDocument()
  })

  it("the dialog's switch is the card's switch", () => {
    render(<Harness intro={INTRO} />)
    fireEvent.click(screen.getByRole('button', { name: 'See what Sample preview looks like' }))
    const dialog = screen.getByRole('dialog')
    const inner = within(dialog).getByRole('switch', { name: 'Sample preview' })
    expect(inner).toHaveAttribute('aria-checked', 'false')
    expect(within(dialog).getByText('Turn this preview on')).toBeInTheDocument()
    fireEvent.click(inner)
    expect(inner).toHaveAttribute('aria-checked', 'true')
    // The footer label follows the state: an instruction while off, a state while on.
    expect(within(dialog).getByText(/^This preview is on/)).toBeInTheDocument()
    expect(screen.getByTestId('outer').textContent).toBe('on')
  })
})

describe('FeaturePreviewsSection — See-what-it-looks-like per card', () => {
  beforeEach(() => {
    for (const k of ['mc-preview-webhooks', 'mc-preview-crew', 'mc-preview-remote-crew-chat']) localStorage.removeItem(k)
  })
  afterEach(cleanup)

  const renderSection = () => render(<MemoryRouter><FeaturePreviewsSection /></MemoryRouter>)

  it('offers the button for the previews that have real captures, and not for the one that does not', () => {
    renderSection()
    expect(screen.getByRole('button', { name: 'See what Webhooks looks like' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'See what Crew Members looks like' })).toBeInTheDocument()
    // "Chat on a crew" only appears with a live tunnel to a second machine, which
    // has no honest single-instance capture — so no button rather than an empty dialog.
    expect(screen.queryByRole('button', { name: 'See what Chat on a crew looks like' })).not.toBeInTheDocument()
  })

  it('flipping the switch inside the dialog writes the flag and the card switch follows', () => {
    renderSection()
    fireEvent.click(screen.getByRole('button', { name: 'See what Webhooks looks like' }))
    const dialog = screen.getByRole('dialog', { name: 'Webhooks' })
    fireEvent.click(within(dialog).getByRole('switch', { name: 'Webhooks' }))
    expect(localStorage.getItem('mc-preview-webhooks')).toBe('1')
    expect(within(dialog).getByRole('switch', { name: 'Webhooks' })).toHaveAttribute('aria-checked', 'true')
    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(screen.getByRole('switch', { name: 'Webhooks' })).toHaveAttribute('aria-checked', 'true')
  })

  it('the button stays offered while the flag is on, beside the ingress link', () => {
    localStorage.setItem('mc-preview-webhooks', '1')
    renderSection()
    expect(screen.getByRole('button', { name: 'See what Webhooks looks like' })).toBeInTheDocument()
    expect(screen.getByText('Open Webhooks')).toBeInTheDocument()
  })
})
