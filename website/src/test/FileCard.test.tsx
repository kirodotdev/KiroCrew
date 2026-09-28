import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { FileCard } from '../components/FileCard'

describe('FileCard', () => {
  it('renders audio player for audio/mpeg', () => {
    render(<FileCard file={{ filename: 'standup.mp3', content_type: 'audio/mpeg', description: 'Daily standup' }} />)
    expect(screen.getByText('standup.mp3')).toBeInTheDocument()
    expect(screen.getByText(/Daily standup/)).toBeInTheDocument()
    const audio = document.querySelector('audio')
    expect(audio).toBeInTheDocument()
    expect(audio?.src).toContain('/api/outbox/standup.mp3')
  })

  it('renders video player for video/mp4', () => {
    render(<FileCard file={{ filename: 'clip.mp4', content_type: 'video/mp4' }} />)
    expect(screen.getByText('clip.mp4')).toBeInTheDocument()
    const video = document.querySelector('video')
    expect(video).toBeInTheDocument()
    expect(video?.src).toContain('/api/outbox/clip.mp4')
  })

  it('keeps long descriptions on their own wrapping line', () => {
    const description = 'A long description that should remain readable instead of disappearing behind the metadata row'
    render(<FileCard file={{ filename: 'report.pdf', content_type: 'application/pdf', size: 2048, description }} />)
    const line = screen.getByText(description)
    expect(line).toHaveClass('break-words')
    expect(line).not.toHaveClass('truncate')
  })

  it('keeps the whole non-media card as the download link', () => {
    render(<FileCard file={{ filename: 'report.pdf', content_type: 'application/pdf', size: 2048 }} />)
    expect(screen.getByText('report.pdf')).toBeInTheDocument()
    // `fmtBytes` is SI (1000-based) and locale-formatted, so 2048 B is 2 kB and not
    // the old hand-rolled "2.0 KB" — which divided by 1024 while labelling the result
    // KB, i.e. printed kibibytes under a kilobyte label. Matched with a
    // whitespace-insensitive matcher rather than a literal: `fmtUnit` asks CLDR for
    // the `narrow` form and promotes any plain space to U+00A0, and the separator
    // CLDR chooses differs per locale and can change across ICU versions.
    // The meta line joins type · size, so match the size as a segment of that
    // line rather than as the whole element text.
    expect(screen.getByText((content) => /^PDF\s*·\s*2\s*kB$/.test(content.replace(/\u00a0/g, ' ')))).toBeInTheDocument()
    expect(screen.getByTestId('file-card-glyph').dataset.family).toBe('document')
    const link = document.querySelector('a[download]')
    expect(link).toBeInTheDocument()
    expect(link?.getAttribute('href')).toContain('/api/outbox/report.pdf')
    expect(screen.getByText('report.pdf').closest('a[download]')).toBe(link)
    expect(link).toHaveAccessibleName(/Save/)
    expect(document.querySelectorAll('a[download]')).toHaveLength(1)
  })

  it('renders download link when no content_type', () => {
    render(<FileCard file={{ filename: 'data.bin' }} />)
    expect(screen.getByText('data.bin')).toBeInTheDocument()
    expect(screen.getByTestId('file-card-glyph').dataset.family).toBe('unknown')
    expect(document.querySelector('a[download]')).toBeInTheDocument()
    expect(document.querySelector('audio')).not.toBeInTheDocument()
    expect(document.querySelector('video')).not.toBeInTheDocument()
  })

  it('picks the family from the extension when the MIME is opaque', () => {
    render(<FileCard file={{ filename: 'deploy-key.pem', content_type: 'application/octet-stream' }} />)
    expect(screen.getByTestId('file-card-glyph').dataset.family).toBe('key')
    expect(document.querySelector('a[download]')).toBeInTheDocument()
  })

  it('renders audio player for audio/ogg', () => {
    render(<FileCard file={{ filename: 'track.ogg', content_type: 'audio/ogg' }} />)
    expect(document.querySelector('audio')).toBeInTheDocument()
  })

  it('renders video player for video/webm', () => {
    render(<FileCard file={{ filename: 'demo.webm', content_type: 'video/webm' }} />)
    expect(document.querySelector('video')).toBeInTheDocument()
  })

  it('encodes filename in URL', () => {
    render(<FileCard file={{ filename: 'my file (1).mp3', content_type: 'audio/mpeg' }} />)
    const audio = document.querySelector('audio')
    expect(audio?.src).toContain('my%20file%20(1).mp3')
  })

  it('renders inline image for image/png', () => {
    render(<FileCard file={{ filename: 'screenshot.png', content_type: 'image/png', description: 'A screenshot' }} />)
    expect(screen.getByText('screenshot.png')).toBeInTheDocument()
    expect(screen.getByText(/A screenshot/)).toBeInTheDocument()
    const img = document.querySelector('img')
    expect(img).toBeInTheDocument()
    expect(img?.src).toContain('/api/outbox/screenshot.png')
    expect(img?.alt).toBe('A screenshot')
  })
})
