/**
 * Runtime coverage for the HOST↔OVERLAY postMessage bridge in DesignTweakPage.
 *
 * The bridge is a trust boundary: the framed document is an arbitrary user
 * project, so messages from untrusted origins or unknown sources must be
 * silently dropped. This file covers acceptance, rejection, and the handler
 * branches for each recognised OverlayMessage kind.
 *
 * happy-dom disables iframe page loading, so iframe.contentWindow is null.
 * To exercise the acceptance path we patch the iframe's contentWindow property
 * with a mock Window after render — the component's source gate compares
 * e.source against it, so using the same object as MessageEvent.source passes.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent, act } from '@testing-library/react'
import DesignTweak from '../apps/design-tweak/DesignTweakPage'
import { renderWithProviders } from './helpers'

// Mock every api function the page calls on mount (useQuery hooks).
vi.mock('../apps/design-tweak/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../apps/design-tweak/api')>()),
  fetchProjects: vi.fn(),
  fetchQueue: vi.fn(),
  fetchHistory: vi.fn(),
  fetchHealth: vi.fn(),
  submitComment: vi.fn(),
}))

import {
  fetchProjects, fetchQueue, fetchHistory, fetchHealth, submitComment,
} from '../apps/design-tweak/api'

const mockedFetchProjects = fetchProjects as ReturnType<typeof vi.fn>
const mockedFetchQueue = fetchQueue as ReturnType<typeof vi.fn>
const mockedFetchHistory = fetchHistory as ReturnType<typeof vi.fn>
const mockedFetchHealth = fetchHealth as ReturnType<typeof vi.fn>
const mockedSubmitComment = submitComment as ReturnType<typeof vi.fn>

// The preview origin the component derives from the project's previewUrl.
// Port separates it from the dashboard origin — that is what makes the origin
// check meaningful.
const PREVIEW_ORIGIN = 'http://127.0.0.1:54321'
const PREVIEW_URL = `${PREVIEW_ORIGIN}/`

// Minimal project fixture that activates the preview iframe path.
const TEST_PROJECT = {
  id: 'proj-1',
  path: '/tmp/my-app',
  name: 'My App',
  previewUrl: PREVIEW_URL,
}

function setupMocks() {
  mockedFetchProjects.mockResolvedValue({
    projects: [TEST_PROJECT],
    activeId: 'proj-1',
    serving: true,
  })
  mockedFetchQueue.mockResolvedValue({ pending: [] })
  mockedFetchHistory.mockResolvedValue({ history: [] })
  mockedFetchHealth.mockResolvedValue({ status: 'ok', dataDir: '/tmp/data' })
  mockedSubmitComment.mockResolvedValue({
    ok: true,
    cid: 'cid-123',
    id: 'req-1',
    label: '1.1',
    commentCount: 1,
    number: 1,
  })
}

/**
 * happy-dom disables iframe page loading so contentWindow is always null.
 * The component's source gate reads `iframeRef.current?.contentWindow`, so we
 * patch the property on the DOM element to return a mock object. The same
 * object is then used as `source` in MessageEvent to pass the identity check.
 */
const mockContentWindow = {} as Window

function patchIframeContentWindow(): HTMLIFrameElement | null {
  const iframe = document.querySelector('iframe')
  if (iframe) {
    Object.defineProperty(iframe, 'contentWindow', {
      get: () => mockContentWindow,
      configurable: true,
    })
  }
  return iframe
}

describe('DesignTweak overlay message bridge', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupMocks()
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  /**
   * Render and wait for the component to settle (queries resolve, preview
   * iframe mounts). Patches the iframe's contentWindow for source-gate testing.
   */
  async function renderAndSettle() {
    const result = renderWithProviders(<DesignTweak />)
    // Wait for useQuery data to populate — the projects query drives the UI.
    await waitFor(() => {
      expect(mockedFetchProjects).toHaveBeenCalled()
    })
    // Flush the timer-driven effects (React Query uses setTimeout internally).
    await act(async () => { vi.advanceTimersByTime(100) })
    // Patch the iframe so contentWindow is not null.
    patchIframeContentWindow()
    return result
  }

  // ──────────────────────────────────────────────────────────────────────────
  // ORIGIN CHECK — the single most valuable assertion in this surface.
  // The overlay runs inside a previewed project, so an accepted cross-origin
  // message would let arbitrary page content drive this panel.
  // ──────────────────────────────────────────────────────────────────────────

  it('rejects messages from a wrong origin (the critical security boundary)', async () => {
    await renderAndSettle()
    // Even with the correct source field, type, and window identity, a wrong
    // origin is dropped.
    const maliciousPayload = {
      source: 'kiro-select-to-edit',
      type: 'capture',
      payload: {
        type: 'visual_edit_request',
        comment: 'hacked!',
        selection: { mode: 'single', elements: [{ tag: 'div' }] },
      },
    }
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: maliciousPayload,
        origin: 'https://evil.example.com',
        source: mockContentWindow,
      }))
    })
    // submitComment must NOT have been called — the message was rejected.
    expect(mockedSubmitComment).not.toHaveBeenCalled()
  })

  it('rejects messages when e.source does not match the iframe contentWindow', async () => {
    await renderAndSettle()
    // Use null source — simulates a message from a different frame or opener.
    const payload = {
      source: 'kiro-select-to-edit',
      type: 'capture',
      payload: {
        type: 'visual_edit_request',
        comment: 'from wrong source',
        selection: { mode: 'single', elements: [{ tag: 'p' }] },
      },
    }
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: payload,
        origin: PREVIEW_ORIGIN,
        source: null,
      }))
    })
    expect(mockedSubmitComment).not.toHaveBeenCalled()
  })

  it('rejects messages from a different window object (not the preview iframe)', async () => {
    await renderAndSettle()
    // A different Window-like object — simulates a message from another frame.
    const differentWindow = {} as Window
    const payload = {
      source: 'kiro-select-to-edit',
      type: 'capture',
      payload: {
        type: 'visual_edit_request',
        comment: 'from wrong frame',
        selection: { mode: 'single', elements: [{ tag: 'div' }] },
      },
    }
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: payload,
        origin: PREVIEW_ORIGIN,
        source: differentWindow,
      }))
    })
    expect(mockedSubmitComment).not.toHaveBeenCalled()
  })

  it('rejects messages with wrong data.source field (type allowlist gate)', async () => {
    await renderAndSettle()
    // Correct source window and origin, but wrong data.source identifier.
    const payload = {
      source: 'some-other-widget',
      type: 'capture',
      payload: { comment: 'sneaky' },
    }
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: payload,
        origin: PREVIEW_ORIGIN,
        source: mockContentWindow,
      }))
    })
    expect(mockedSubmitComment).not.toHaveBeenCalled()
  })

  it('rejects messages with an unrecognised type (neither capture nor dispatch)', async () => {
    await renderAndSettle()
    const payload = {
      source: 'kiro-select-to-edit',
      type: 'unknown-type',
      payload: { comment: 'nope' },
    }
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: payload,
        origin: PREVIEW_ORIGIN,
        source: mockContentWindow,
      }))
    })
    expect(mockedSubmitComment).not.toHaveBeenCalled()
  })

  // ──────────────────────────────────────────────────────────────────────────
  // ACCEPTED MESSAGES — capture (element selection) and dispatch (follow-up)
  // ──────────────────────────────────────────────────────────────────────────

  it('accepts a "capture" message from the correct origin and calls submitComment', async () => {
    await renderAndSettle()
    const capturePayload = {
      source: 'kiro-select-to-edit',
      type: 'capture',
      clientRef: 'ref-abc',
      payload: {
        type: 'visual_edit_request',
        comment: 'Make this button blue',
        selection: {
          mode: 'single',
          elements: [{ tag: 'button', id: 'cta', classes: ['primary'], locator: 'button#cta' }],
        },
      },
    }
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: capturePayload,
        origin: PREVIEW_ORIGIN,
        source: mockContentWindow,
      }))
    })
    // The handler calls submitComment with the payload enriched with projectId.
    await waitFor(() => {
      expect(mockedSubmitComment).toHaveBeenCalledTimes(1)
    })
    const call = mockedSubmitComment.mock.calls[0][0]
    expect(call.comment).toBe('Make this button blue')
    // The handler stamps the currently-previewed project id onto the payload.
    expect(call.projectId).toBe('proj-1')
    expect(call.selection.elements[0].tag).toBe('button')
  })

  it('handles a "capture" message when submitComment returns an error', async () => {
    await renderAndSettle()
    // Set the error mock AFTER render settles — queries on mount consume earlier mocks.
    mockedSubmitComment.mockClear()
    mockedSubmitComment.mockResolvedValueOnce({ ok: false, error: 'backend down' })
    const capturePayload = {
      source: 'kiro-select-to-edit',
      type: 'capture',
      payload: {
        type: 'visual_edit_request',
        comment: 'Will fail',
        selection: { mode: 'single', elements: [{ tag: 'span' }] },
      },
    }
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: capturePayload,
        origin: PREVIEW_ORIGIN,
        source: mockContentWindow,
      }))
    })
    // Should still call submitComment — the error path is in how the response
    // is handled, not whether it is called.
    await waitFor(() => {
      expect(mockedSubmitComment).toHaveBeenCalledTimes(1)
    })
  })

  it('handles a "capture" message when submitComment throws', async () => {
    await renderAndSettle()
    // Set the rejection mock AFTER render settles.
    mockedSubmitComment.mockClear()
    mockedSubmitComment.mockRejectedValueOnce(new Error('network failure'))
    const payload = {
      source: 'kiro-select-to-edit',
      type: 'capture',
      payload: {
        type: 'visual_edit_request',
        comment: 'Will throw',
        selection: { mode: 'single', elements: [{ tag: 'div' }] },
      },
    }
    // Should not throw unhandled — the component catches it.
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: payload,
        origin: PREVIEW_ORIGIN,
        source: mockContentWindow,
      }))
    })
    await waitFor(() => {
      expect(mockedSubmitComment).toHaveBeenCalledTimes(1)
    })
  })

  it('accepts a "dispatch" message (follow-up reply to an existing comment)', async () => {
    // Set up a pending request with a comment so the commentIndex has an entry.
    const existingComment = {
      cid: 'cid-existing',
      index: 1,
      status: 'sent',
      comment: 'Original comment',
      locator: 'div.hero',
      previewUrl: PREVIEW_URL,
      projectId: 'proj-1',
    }
    const existingRequest = {
      id: 'req-exist',
      number: 1,
      status: 'sent',
      state: 'sent',
      projectId: 'proj-1',
      projectRoot: '/tmp/my-app',
      comments: [existingComment],
    }
    mockedFetchQueue.mockResolvedValue({ pending: [existingRequest] })

    await renderAndSettle()

    const dispatchPayload = {
      source: 'kiro-select-to-edit',
      type: 'dispatch',
      id: 'cid-existing',  // references the existing comment's cid
      text: 'Actually, make it green instead',
    }
    // Clear mock from any mount-time calls, then set the follow-up response.
    mockedSubmitComment.mockClear()
    mockedSubmitComment.mockResolvedValueOnce({
      ok: true,
      cid: 'cid-followup',
      id: 'req-new',
      label: '2.1',
      commentCount: 1,
      number: 2,
    })
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: dispatchPayload,
        origin: PREVIEW_ORIGIN,
        source: mockContentWindow,
      }))
    })
    await waitFor(() => {
      expect(mockedSubmitComment).toHaveBeenCalledTimes(1)
    })
    const call = mockedSubmitComment.mock.calls[0][0]
    expect(call.comment).toBe('Actually, make it green instead')
    expect(call.followUpTo).toBe('cid-existing')
    expect(call.projectId).toBe('proj-1')
  })

  it('handles "dispatch" when the referenced comment id is not found', async () => {
    // No pending requests with matching cid, so commentIndex lookup fails.
    mockedFetchQueue.mockResolvedValue({ pending: [] })
    await renderAndSettle()
    // Clear any calls from render, then dispatch.
    mockedSubmitComment.mockClear()

    const dispatchPayload = {
      source: 'kiro-select-to-edit',
      type: 'dispatch',
      id: 'nonexistent-cid',
      text: 'This has no origin',
    }
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: dispatchPayload,
        origin: PREVIEW_ORIGIN,
        source: mockContentWindow,
      }))
    })
    // submitComment should NOT be called when the origin comment is missing.
    expect(mockedSubmitComment).not.toHaveBeenCalled()
  })

  it('handles "dispatch" when submitComment returns an error for the follow-up', async () => {
    const existingComment = {
      cid: 'cid-err',
      index: 1,
      status: 'done',
      comment: 'Done comment',
      locator: 'h1.title',
      previewUrl: PREVIEW_URL,
      projectId: 'proj-1',
    }
    mockedFetchQueue.mockResolvedValue({
      pending: [{
        id: 'req-e',
        number: 3,
        status: 'done',
        state: 'done',
        projectId: 'proj-1',
        projectRoot: '/tmp/my-app',
        comments: [existingComment],
      }],
    })

    await renderAndSettle()
    // Set error mock AFTER settle so mount queries don't consume it.
    mockedSubmitComment.mockClear()
    mockedSubmitComment.mockResolvedValueOnce({ ok: false, error: 'save failed' })

    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: {
          source: 'kiro-select-to-edit',
          type: 'dispatch',
          id: 'cid-err',
          text: 'Follow-up that fails',
        },
        origin: PREVIEW_ORIGIN,
        source: mockContentWindow,
      }))
    })
    await waitFor(() => {
      expect(mockedSubmitComment).toHaveBeenCalledTimes(1)
    })
  })

  // ──────────────────────────────────────────────────────────────────────────
  // HOST → OVERLAY direction (postToOverlay) — mode change push
  // ──────────────────────────────────────────────────────────────────────────

  it('pushes edit mode state to the overlay when the mode toggle is clicked', async () => {
    await renderAndSettle()
    // Spy on the mock contentWindow's postMessage to verify host→overlay push.
    // postToOverlay calls iframeRef.current?.contentWindow?.postMessage(...)
    const postMessageSpy = vi.fn()
    ;(mockContentWindow as unknown as Record<string, unknown>).postMessage = postMessageSpy

    // Click the Edit mode button. The component calls setEditMode('edit') which
    // invokes postToOverlay with type:'state' and editMode:true.
    const editBtn = screen.getAllByRole('button').find(
      (b) => b.textContent?.includes('edit') || b.textContent?.includes('Edit')
        || b.textContent?.includes('apps.designTweak.modes.edit'),
    )
    expect(editBtn).toBeDefined()
    await act(async () => { fireEvent.click(editBtn!) })
    // Verify host→overlay message was posted with correct target origin.
    expect(postMessageSpy).toHaveBeenCalled()
    const [msg, targetOrigin] = postMessageSpy.mock.calls[0]
    expect(msg.source).toBe('kiro-ste-host')
    expect(msg.type).toBe('state')
    expect(msg.editMode).toBe(true)
    // The host posts to the preview origin, never '*'.
    expect(targetOrigin).toBe(PREVIEW_ORIGIN)
  })

  // ──────────────────────────────────────────────────────────────────────────
  // LISTENER CLEANUP — the useEffect returns a removeEventListener
  // ──────────────────────────────────────────────────────────────────────────

  it('removes the message listener on unmount (no leaked handlers)', async () => {
    const removeSpy = vi.spyOn(window, 'removeEventListener')
    const { unmount } = await renderAndSettle()
    unmount()
    // The component's useEffect cleanup calls window.removeEventListener('message', onMsg).
    const messageCalls = removeSpy.mock.calls.filter(([event]) => event === 'message')
    expect(messageCalls.length).toBeGreaterThan(0)
    removeSpy.mockRestore()
  })
})
