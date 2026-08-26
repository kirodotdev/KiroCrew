/**
 * The composer must OFFER video, not just tolerate it.
 *
 * On a phone the `<input accept>` list is what the system photo picker filters
 * the library by, so an accept list of image MIME types plus document
 * extensions is exactly what made "attach" show photos and hide every
 * recording — the bug this covers. The picker is the only surface that carries
 * this hint (`openPicker` rewrites `accept` for the image-only entry), so it is
 * asserted on the rendered input rather than on the constant.
 *
 * VIDEO_EXT is pinned alongside it because the two must agree: the accept list
 * decides what a user can CHOOSE, VIDEO_EXT decides which of those choices skip
 * the 50 MB client-side pre-check, and a mismatch means either a file the
 * picker offers and the client then silently drops, or a document sent past a
 * guard that exists to catch it.
 */
import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'

import { VIDEO_EXT, VIDEO_MAX_BYTES } from '../utils/fileTokens'

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

describe('VIDEO_MAX_BYTES', () => {
  it('is the ceiling ChatPane pre-checks against, and clears the document cap', () => {
    // NOT a cross-language pin: a vitest cannot read `_MAX_VIDEO_UPLOAD_BYTES`,
    // so this only fixes the value the client uses. The two-sided check lives in
    // test/test_handlers_files_video_upload.py
    // (test_client_video_cap_matches_the_server_ceiling), which can see both
    // numbers — a server cap raised without the mirror leaves ChatPane, which
    // renders nothing on failure, silently refusing legal recordings.
    expect(VIDEO_MAX_BYTES).toBe(512 * 1024 * 1024)
    expect(VIDEO_MAX_BYTES).toBeGreaterThan(50 * 1024 * 1024)
  })
})
