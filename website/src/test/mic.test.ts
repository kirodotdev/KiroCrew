import { describe, it, expect, vi, afterEach } from 'vitest'
import { humanizeMicError, reportIfMicDenied } from '../hooks/mic'

type MicWindow = { electronAPI?: { reportMicDenied?: () => void } }

describe('humanizeMicError', () => {
  it('maps permission denial', () => {
    expect(humanizeMicError({ name: 'NotAllowedError' })).toMatch(/permission denied/i)
  })
  it('maps a missing device', () => {
    expect(humanizeMicError({ name: 'NotFoundError' })).toMatch(/no microphone/i)
  })
  it('maps a device already in use', () => {
    expect(humanizeMicError({ name: 'NotReadableError' })).toMatch(/another app/i)
  })
  it('falls back for unknown errors', () => {
    expect(humanizeMicError(new Error('weird'))).toMatch(/could not start/i)
  })
})

// In a browser the toast is the whole story — the user re-grants from the
// omnibox. In the packaged desktop app it is a dead end: macOS's mic prompt is
// one-shot, so once denied the OS never asks again and page JS cannot open
// System Settings. So a denial hands off to the shell, which re-checks the real
// OS status and offers the Privacy pane.
describe('humanizeMicError — desktop recovery hand-off', () => {
  afterEach(() => {
    delete (window as MicWindow).electronAPI
  })

  it('notifies the shell on a permission denial', () => {
    const reportMicDenied = vi.fn()
    ;(window as MicWindow).electronAPI = { reportMicDenied }
    humanizeMicError({ name: 'NotAllowedError' })
    expect(reportMicDenied).toHaveBeenCalledTimes(1)
    humanizeMicError({ name: 'SecurityError' })
    expect(reportMicDenied).toHaveBeenCalledTimes(2)
  })

  it('does NOT notify for failures the OS did not cause', () => {
    // A missing device or a busy mic is not a permission problem; sending the
    // user to the Privacy pane for those would be actively misleading.
    const reportMicDenied = vi.fn()
    ;(window as MicWindow).electronAPI = { reportMicDenied }
    humanizeMicError({ name: 'NotFoundError' })
    humanizeMicError({ name: 'NotReadableError' })
    humanizeMicError({ name: 'OverconstrainedError' })
    humanizeMicError(new Error('weird'))
    expect(reportMicDenied).not.toHaveBeenCalled()
  })

  it('reportIfMicDenied covers paths that show no message', () => {
    // The settings "Allow microphone access" button and meeting transcription
    // don't call humanizeMicError, so they hand off explicitly — otherwise the
    // one affordance for fixing a denial silently does nothing forever.
    const reportMicDenied = vi.fn()
    ;(window as MicWindow).electronAPI = { reportMicDenied }
    reportIfMicDenied({ name: 'NotAllowedError' })
    reportIfMicDenied({ name: 'SecurityError' })
    expect(reportMicDenied).toHaveBeenCalledTimes(2)
    // Not a permission problem -> no misleading trip to the Privacy pane.
    reportIfMicDenied({ name: 'NotFoundError' })
    reportIfMicDenied(new Error('weird'))
    reportIfMicDenied(null)
    expect(reportMicDenied).toHaveBeenCalledTimes(2)
  })

  it('still returns the message with no shell bridge, or a throwing one', () => {
    // Plain browser: no electronAPI at all.
    expect(humanizeMicError({ name: 'NotAllowedError' })).toMatch(/permission denied/i)
    // Telling the user why must never be what throws.
    ;(window as MicWindow).electronAPI = {
      reportMicDenied: () => { throw new Error('ipc gone') },
    }
    expect(humanizeMicError({ name: 'NotAllowedError' })).toMatch(/permission denied/i)
  })
})
