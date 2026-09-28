import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { detectFileType, MediaPlayer } from '../components/FileRenderers'
import { fileStreamUrl } from '../utils/fileReadUrl'

vi.mock('../i18n/t', () => ({ i18nT: (k: string) => k }))

describe('detectFileType media routing', () => {
  it('routes video containers to video', () => {
    expect(detectFileType('demo.mp4')).toBe('video')
    expect(detectFileType('/outbox/feature-demo.webm')).toBe('video')
    expect(detectFileType('clip.m4v')).toBe('video')
    expect(detectFileType('capture.mov')).toBe('video')
    expect(detectFileType('rip.mkv')).toBe('video')
    expect(detectFileType('anim.ogv')).toBe('video')
    expect(detectFileType('DEMO.MP4')).toBe('video')
  })

  it('routes audio containers to audio', () => {
    expect(detectFileType('note.mp3')).toBe('audio')
    expect(detectFileType('voice.wav')).toBe('audio')
    expect(detectFileType('track.m4a')).toBe('audio')
    expect(detectFileType('lossless.flac')).toBe('audio')
    expect(detectFileType('stream.ogg')).toBe('audio')
    expect(detectFileType('alt.oga')).toBe('audio')
  })

  it('does not disturb neighboring types', () => {
    expect(detectFileType('movie-notes.md')).toBe('markdown')
    expect(detectFileType('report.docx')).toBe('office')
    expect(detectFileType('workbook.xlsx')).toBe('sheet')
    expect(detectFileType('image.webp')).toBe('image')
    expect(detectFileType('script.py')).toBe('code')
  })
})

describe('fileStreamUrl', () => {
  it('encodes the path against the stream endpoint', () => {
    expect(fileStreamUrl('/outbox/my demo.mp4')).toBe(
      '/api/file-stream?path=' + encodeURIComponent('/outbox/my demo.mp4')
    )
  })

  it('appends resolve=1 for relative paths, matching the sibling builders', () => {
    expect(fileStreamUrl('outbox/demo.mp4')).toBe(
      '/api/file-stream?path=' + encodeURIComponent('outbox/demo.mp4') + '&resolve=1'
    )
    expect(fileStreamUrl('~/outbox/demo.mp4')).toBe(
      '/api/file-stream?path=' + encodeURIComponent('~/outbox/demo.mp4')
    )
  })
})

describe('MediaPlayer', () => {
  it('renders a video element pointed at the stream URL', () => {
    const { container } = render(<MediaPlayer filePath="/outbox/demo.mp4" kind="video" />)
    const video = container.querySelector('video')
    expect(video).not.toBeNull()
    expect(video!.getAttribute('src')).toBe(fileStreamUrl('/outbox/demo.mp4'))
    expect(video!.hasAttribute('controls')).toBe(true)
    expect(video!.getAttribute('preload')).toBe('metadata')
  })

  it('renders an audio element with the filename label', () => {
    const { container } = render(<MediaPlayer filePath="/outbox/voice note.mp3" kind="audio" />)
    const audio = container.querySelector('audio')
    expect(audio).not.toBeNull()
    expect(audio!.getAttribute('src')).toBe(fileStreamUrl('/outbox/voice note.mp3'))
    expect(screen.getByText('voice note.mp3')).toBeTruthy()
  })

  it('windows paths surface only the basename', () => {
    render(<MediaPlayer filePath={'C:\\Users\\me\\clip.mp4'} kind="video" />)
    const video = document.querySelector('video')
    expect(video!.getAttribute('aria-label')).toBe('clip.mp4')
  })

  it('falls back to the download card when playback errors', () => {
    const { container } = render(<MediaPlayer filePath="/outbox/broken.mp4" kind="video" />)
    fireEvent.error(container.querySelector('video')!)
    expect(container.querySelector('video')).toBeNull()
    expect(screen.getByText('components.fileRenderers.media_preview_failed')).toBeTruthy()
    const link = container.querySelector('a[download]')
    expect(link).not.toBeNull()
    expect(link!.getAttribute('href')).toContain('/api/file-download')
  })

  it('audio errors degrade to the same fallback', () => {
    const { container } = render(<MediaPlayer filePath="/outbox/broken.ogg" kind="audio" />)
    fireEvent.error(container.querySelector('audio')!)
    expect(container.querySelector('audio')).toBeNull()
    expect(screen.getByText('components.fileRenderers.media_preview_failed')).toBeTruthy()
  })
})
