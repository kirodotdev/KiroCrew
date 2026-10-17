/**
 * The composer must OFFER media the upload boundary accepts.
 *
 * Video uses MIME hints so mobile photo pickers surface recordings. Audio uses
 * exact extensions so the dialog does not offer neighboring formats that the
 * server's content-signature gate cannot verify.
 */
import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'

import { VIDEO_EXT } from '../utils/fileTokens'

vi.mock('../hooks/useScreenSnip', () => ({ isScreenSnipSupported: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => true }))
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => true }))
vi.mock('../api/client', () => ({ api: new Proxy({}, { get: () => vi.fn() }) }))

import ChatInput from '../components/ChatInput'

const base = { value: '', onChange: vi.fn(), onSend: vi.fn(), onUploadFiles: vi.fn() }

const fileInput = () =>
  screen.getByLabelText('Attach files', { selector: 'input[type="file"]' })

describe('composer accept list — video', () => {
  it('offers the video containers the server accepts', () => {
    renderWithProviders(<ChatInput {...base} />)
    const accept = fileInput().getAttribute('accept') || ''
    // MIME form, not extensions: iOS filters the photo library by type, and an
    // extension-only hint leaves videos invisible even though they are legal.
    expect(accept).toContain('video/quicktime') // .mov — macOS/iOS screen recording
    expect(accept).toContain('video/mp4')
    // `.m4v` needs its own type: a picker filtering on `video/mp4` alone hides it.
    expect(accept).toContain('video/x-m4v')
    expect(accept).toContain('video/webm')
  })

  it('keeps the existing image and document hints intact', () => {
    renderWithProviders(<ChatInput {...base} />)
    const accept = fileInput().getAttribute('accept') || ''
    expect(accept).toContain('image/png')
    expect(accept).toContain('.pdf')
    expect(accept).toContain('.zip')
  })
})

describe('composer accept list — audio', () => {
  it('offers exactly the audio containers the server accepts', () => {
    renderWithProviders(<ChatInput {...base} />)
    const accept = new Set((fileInput().getAttribute('accept') || '').split(','))
    for (const ext of ['.mp3', '.m4a', '.wav', '.ogg', '.oga', '.opus', '.flac']) {
      expect(accept).toContain(ext)
    }
    expect(accept).not.toContain('.aac')
    expect(accept).not.toContain('.wma')
  })
})

describe('VIDEO_EXT', () => {
  it('matches every container the accept list offers', () => {
    for (const name of ['clip.mp4', 'clip.m4v', 'Screen Recording.mov', 'cap.webm']) {
      expect(VIDEO_EXT.test(name)).toBe(true)
    }
  })

  it('is case-insensitive — a camera roll yields .MOV and .MP4', () => {
    expect(VIDEO_EXT.test('IMG_0042.MOV')).toBe(true)
    expect(VIDEO_EXT.test('CLIP.MP4')).toBe(true)
  })

  it('does not match documents, images, or the excluded .mkv', () => {
    for (const name of ['notes.md', 'shot.png', 'deck.pptx', 'capture.mkv']) {
      expect(VIDEO_EXT.test(name)).toBe(false)
    }
  })

  it('anchors at the end so a video name inside a document name does not match', () => {
    // `mp4-notes.md` and `about-mov.txt` are documents; matching them would
    // exempt a real document from the 50 MB guard.
    expect(VIDEO_EXT.test('mp4-notes.md')).toBe(false)
    expect(VIDEO_EXT.test('about-mov.txt')).toBe(false)
  })
})
