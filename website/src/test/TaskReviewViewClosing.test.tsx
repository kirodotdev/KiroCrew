// The review view's close control while the stop it asked for is in flight.
//
// End flushes the held transcript finals before its request goes out, so there
// is a window in which the click was heard but nothing has reached the server.
// The control must read as pending through that window and refuse a repeat,
// and it must come back if the stop fails, so the user can try again, with the
// reason said beside it.

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import TaskReviewView from '../apps/meetings/TaskReviewView'
import { i18nT } from '../i18n/t'

afterEach(cleanup)

function mount(closing: boolean, closeError: string | null = null) {
  const onClose = vi.fn()
  const props: React.ComponentProps<typeof TaskReviewView> = {
    tasks: [],
    transcript: [],
    partialTranscript: '',
    transcriptFull: false,
    provider: '',
    filing: null,
    closing,
    closeError,
    onBack: vi.fn(),
    onClose,
    onFile: vi.fn(),
    onArchive: vi.fn(),
    onUnarchive: vi.fn(),
  }
  const view = render(<TaskReviewView {...props} />)
  const name = closing ? i18nT('apps.meetings.review.closing') : i18nT('apps.meetings.review.closeMeeting')
  const close = screen.getByRole('button', { name })
  return { ...view, onClose, close, props }
}

describe('TaskReviewView — the close control while the stop is in flight', () => {
  it('is disabled and marked busy while closing, so a repeat cannot queue a second stop', () => {
    const { close, onClose } = mount(true)
    expect(close).toBeDisabled()
    expect(close).toHaveAttribute('aria-busy', 'true')
    fireEvent.click(close)
    expect(onClose).not.toHaveBeenCalled()
  })

  it('is clickable again once the stop has settled', () => {
    const { close, onClose, rerender, props } = mount(true)
    rerender(<TaskReviewView {...props} closing={false} />)
    expect(close).toBeEnabled()
    expect(close).not.toHaveAttribute('aria-busy', 'true')
    fireEvent.click(close)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('says what it is waiting for while closing', () => {
    const { close, rerender, props } = mount(true)
    expect(close).toHaveAccessibleName('Saving the transcript…')
    expect(close).toHaveTextContent('Saving the transcript…')
    rerender(<TaskReviewView {...props} closing={false} />)
    expect(close).toHaveAccessibleName('Close the meeting')
    expect(close).toHaveTextContent('Close the meeting')
  })

  it('says why the close failed beside the control, until End is tried again', () => {
    const message = i18nT('apps.meetings.session.stopFailed')
    const { close, rerender, props } = mount(false, message)
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent(message)
    expect(close.parentElement).toContainElement(alert)
    expect(close).toBeEnabled()
    rerender(<TaskReviewView {...props} closeError={null} />)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('disables Back to the meeting while the close is pending, so the two exits cannot race', () => {
    const { props } = mount(true)
    const back = screen.getByRole('button', { name: i18nT('apps.meetings.review.backToMeeting') })
    expect(back).toBeDisabled()
    fireEvent.click(back)
    expect(props.onBack).not.toHaveBeenCalled()
  })

  it('hides the last failure while a retry is in flight', () => {
    const message = i18nT('apps.meetings.session.stopFailed')
    mount(true, message)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByRole('button', { name: i18nT('apps.meetings.review.closing') })).toBeDisabled()
  })
})
