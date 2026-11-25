import React from 'react'
import { describe, it, expect, vi, beforeEach, onTestFinished } from 'vitest'
import { act, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { registerComposerUpload, unregisterComposerUpload } from '../utils/composerSendHolds'
import { PREVIEW_STRIP_H, UPLOAD_STRIP_H, setStripHeight, stubStripHeights } from './stripHeights'

// The attachment preview strip now renders for folder references too, but the
// composer's manual-height compensation used to key off `pendingFiles` alone.
// With a manually resized composer and only a folder staged, the strip appeared
// without the FILE_PREVIEW_H allowance, so it ate into the textarea instead of
// expanding the wrapper.

const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
}

// The composer's own drag floor. The strip height is no longer a constant here:
// it is MEASURED off the rendered strip, which `stubStripHeights` gives a real
// box in jsdom. So these assertions read "drag floor + what the strip measured"
// rather than restating an arithmetic sum that lived in two files.
const INPUT_DRAG_MIN_H = 93
const MANUAL_H = 300

const outerOf = () => screen.getByLabelText('Message input').closest('.input-area') as HTMLElement

beforeEach(() => {
  vi.restoreAllMocks()
  stubStripHeights()
  localStorage.clear()
  // A persisted manual height is what activates the minHeight compensation.
  localStorage.setItem('mc-input-height', String(MANUAL_H))
})

describe('ChatInput preview-strip height compensation', () => {
  it('compensates for a dirs-only preview strip', () => {
    renderWithProviders(<ChatInput {...defaultProps} pendingDirs={['/repo/website/docs']} />)
    expect(outerOf().style.minHeight).toBe(`${INPUT_DRAG_MIN_H + PREVIEW_STRIP_H}px`)
  })

  it('compensates for a files-only preview strip (unchanged)', () => {
    renderWithProviders(<ChatInput {...defaultProps} pendingFiles={['/tmp/a.txt']} />)
    expect(outerOf().style.minHeight).toBe(`${INPUT_DRAG_MIN_H + PREVIEW_STRIP_H}px`)
  })

  // A measured height lands one commit AFTER the strip mounts, so the settling
  // 0 -> 81 must read as a BASELINE, not as "a strip just appeared". Without
  // that gate, opening a composer that already has something staged adds the
  // strip's height to the persisted manual height on every single mount, and it
  // compounds. Predicted constants never had this failure mode, so it is the
  // one measurement introduces and the one worth pinning.
  it('does not inflate the persisted height when a strip is present at mount', () => {
    renderWithProviders(<ChatInput {...defaultProps} pendingFiles={['/tmp/a.txt']} />)
    expect(outerOf().style.minHeight).toBe(`${INPUT_DRAG_MIN_H + PREVIEW_STRIP_H}px`)
    const wrapper = screen.getByTestId('input-wrapper')
    // The persisted height itself is untouched: only the floor moved.
    expect(wrapper.style.height).toBe(`${MANUAL_H}px`)
  })

  it.each([
    { pendingDirs: [], manualHeight: INPUT_DRAG_MIN_H, stagedHeight: 0 },
    { pendingDirs: ['/repo/website/docs'], manualHeight: MANUAL_H, stagedHeight: PREVIEW_STRIP_H },
  ])('grows a pinned $manualHeight px composer by the upload height and restores it', ({ pendingDirs, manualHeight, stagedHeight }) => {
    localStorage.setItem('mc-input-height', String(manualHeight))
    const slot = 'strip-height-upload'
    const controller = new AbortController()
    onTestFinished(() => act(() => unregisterComposerUpload(slot, controller)))
    renderWithProviders(<ChatInput {...defaultProps} uploadSlot={slot} pendingDirs={pendingDirs} />)
    const wrapper = screen.getByTestId('input-wrapper')
    expect(wrapper.style.height).toBe(`${manualHeight}px`)

    act(() => registerComposerUpload(slot, controller, 'report.txt'))
    expect(screen.getByTestId('composer-upload-strip')).toBeInTheDocument()
    expect(wrapper.style.height).toBe(`${manualHeight + UPLOAD_STRIP_H}px`)
    expect(outerOf().style.minHeight).toBe(`${INPUT_DRAG_MIN_H + stagedHeight + UPLOAD_STRIP_H}px`)

    act(() => setStripHeight('composer-upload-strip', UPLOAD_STRIP_H + 10))
    expect(wrapper.style.height).toBe(`${manualHeight + UPLOAD_STRIP_H + 10}px`)
    expect(outerOf().style.minHeight).toBe(`${INPUT_DRAG_MIN_H + stagedHeight + UPLOAD_STRIP_H + 10}px`)

    act(() => unregisterComposerUpload(slot, controller))
    expect(screen.queryByTestId('composer-upload-strip')).not.toBeInTheDocument()
    expect(wrapper.style.height).toBe(`${manualHeight}px`)
    expect(outerOf().style.minHeight).toBe(`${INPUT_DRAG_MIN_H + stagedHeight}px`)
    expect(localStorage.getItem('mc-input-height')).toBe(String(manualHeight))
  })

  it('does not inflate the persisted height when an upload is already running at mount', () => {
    const slot = 'strip-height-mounted-upload'
    const controller = new AbortController()
    onTestFinished(() => act(() => unregisterComposerUpload(slot, controller)))
    registerComposerUpload(slot, controller, 'report.txt')
    renderWithProviders(<ChatInput {...defaultProps} uploadSlot={slot} />)
    expect(screen.getByTestId('composer-upload-strip')).toBeInTheDocument()
    expect(screen.getByTestId('input-wrapper').style.height).toBe(`${MANUAL_H}px`)
    expect(outerOf().style.minHeight).toBe(`${INPUT_DRAG_MIN_H + UPLOAD_STRIP_H}px`)
  })

  // Each chip's cancel is the only way to stop that file, so on a phone a
  // second chip must wrap onto a new line, not scroll past the right edge with
  // its cancel hidden. A wrapping strip grows taller, which the measured-height
  // tests above already follow.
  it('wraps upload chips instead of scrolling them off the edge', () => {
    const slot = 'strip-wrap-upload'
    const a = new AbortController()
    const b = new AbortController()
    onTestFinished(() => act(() => { unregisterComposerUpload(slot, a); unregisterComposerUpload(slot, b) }))
    registerComposerUpload(slot, a, 'a-very-long-recording-name-from-the-phone-camera.mov')
    registerComposerUpload(slot, b, 'second-file.txt')
    renderWithProviders(<ChatInput {...defaultProps} uploadSlot={slot} />)
    const strip = screen.getByTestId('composer-upload-strip')
    expect(strip).toHaveClass('flex-wrap')
    expect(strip).not.toHaveClass('overflow-x-auto')
    const chips = screen.getAllByTestId('composer-upload-chip')
    expect(chips).toHaveLength(2)
    for (const chip of chips) {
      expect(chip).toHaveClass('min-w-0')
      expect(chip).not.toHaveClass('shrink-0')
      expect(chip.querySelector('button')).toHaveClass('shrink-0')
    }
  })

  it('caps a full upload batch and scrolls vertically while keeping cancels in the wrapped strip', () => {
    const slot = 'strip-full-upload-batch'
    const controllers = Array.from({ length: 20 }, () => new AbortController())
    onTestFinished(() => act(() => {
      for (const controller of controllers) unregisterComposerUpload(slot, controller)
    }))
    controllers.forEach((controller, i) => registerComposerUpload(slot, controller, `long-phone-recording-${i}.mov`))
    renderWithProviders(<ChatInput {...defaultProps} uploadSlot={slot} />)
    const strip = screen.getByTestId('composer-upload-strip')
    expect(strip).toHaveClass('max-h-[20svh]', 'overflow-y-auto', 'flex-wrap')
    expect(strip).not.toHaveClass('overflow-x-auto')
    expect(screen.getAllByTestId('composer-upload-chip')).toHaveLength(20)
    expect(strip.querySelectorAll('button')).toHaveLength(20)
    // The height hook still measures the strip's rendered box, not its content.
    expect(outerOf().style.minHeight).toBe(`${INPUT_DRAG_MIN_H + UPLOAD_STRIP_H}px`)
  })

  it('does not compensate when nothing is staged', () => {
    renderWithProviders(<ChatInput {...defaultProps} />)
    expect(outerOf().style.minHeight).toBe(`${INPUT_DRAG_MIN_H}px`)
  })
})
