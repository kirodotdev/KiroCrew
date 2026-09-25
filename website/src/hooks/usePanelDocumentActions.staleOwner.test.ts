/**
 * The side panel's file save is an owner-gated direct fetch: `/api/file-write`
 * runs `require_owner_dashboard_request`, whose denial for a session minted
 * before `KIROCREW_OWNER_ID` was configured is `401 stale_session_reauth`.
 * Like the other direct-fetch owner-gated surfaces, the save must raise the
 * installed re-auth prompt on that signal — and only that signal — while still
 * rejecting, so the editor keeps its unsaved buffer and the user's draft.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'

import { STALE_OWNER_SESSION_CODE, __resetAuthRecoveryStateForTests } from '../api/client'
import { usePanelDocumentActions } from './usePanelDocumentActions'
import { clearInlineDraft, getInlineDraft, setInlineDraft, type usePanelTabs } from './usePanelTabs'

const PATH = '/repo/main/notes.md'
const SLOT = 'chat-1'

const jsonResponse = (status: number, body: unknown): Response =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

const bannerEl = (): HTMLElement | null => document.getElementById('mc-session-expired')

function renderSave() {
  const patchTab = vi.fn()
  const tabsCtl = { patchTab } as unknown as ReturnType<typeof usePanelTabs>
  const { result } = renderHook(() => usePanelDocumentActions({
    tabsCtl,
    slotRef: { current: SLOT },
    queryClient: new QueryClient(),
    showActionError: vi.fn(),
  }))
  return { saveFile: result.current.saveFile, patchTab }
}

describe('usePanelDocumentActions.saveFile — stale pre-owner session', () => {
  let fetchMock: ReturnType<typeof vi.fn>
  let originalFetch: typeof fetch

  beforeEach(() => {
    __resetAuthRecoveryStateForTests()
    fetchMock = vi.fn()
    originalFetch = globalThis.fetch
    globalThis.fetch = fetchMock as unknown as typeof fetch
    // The user kept typing after the save was issued: the draft is NEWER than
    // the bytes being written, and must survive any failed save.
    setInlineDraft(SLOT, PATH, 'newer draft')
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
    clearInlineDraft(SLOT, PATH)
    __resetAuthRecoveryStateForTests()
  })

  it('raises the re-auth prompt on 401 stale_session_reauth and still rejects', async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, {
      error: 'this session predates the configured owner; sign in again',
      code: STALE_OWNER_SESSION_CODE,
    }))
    const { saveFile, patchTab } = renderSave()

    await expect(saveFile(PATH, 'saved bytes')).rejects.toThrow('Save failed: 401')

    expect(bannerEl()).not.toBeNull()
    expect(patchTab).not.toHaveBeenCalled()
    expect(getInlineDraft(SLOT, PATH)).toBe('newer draft')
  })

  it('leaves a generic 401 to the save error alone', async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { error: 'unauthorized', code: 'auth_required' }))
    const { saveFile, patchTab } = renderSave()

    await expect(saveFile(PATH, 'saved bytes')).rejects.toThrow('Save failed: 401')

    expect(bannerEl()).toBeNull()
    expect(patchTab).not.toHaveBeenCalled()
    expect(getInlineDraft(SLOT, PATH)).toBe('newer draft')
  })

  it('leaves a 403 owner_only denial to the save error alone', async () => {
    fetchMock.mockResolvedValue(jsonResponse(403, { error: 'owner authorization required', code: 'owner_only' }))
    const { saveFile } = renderSave()

    await expect(saveFile(PATH, 'saved bytes')).rejects.toThrow('Save failed: 403')

    expect(bannerEl()).toBeNull()
    expect(getInlineDraft(SLOT, PATH)).toBe('newer draft')
  })

  it('keeps the save error when a 401 body cannot be read', async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 401, text: () => Promise.reject(new Error('body stream aborted')) })
    const { saveFile } = renderSave()

    await expect(saveFile(PATH, 'saved bytes')).rejects.toThrow('Save failed: 401')

    expect(bannerEl()).toBeNull()
  })

  it('a successful save restamps the tab baseline without prompting', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { ok: true }))
    const { saveFile, patchTab } = renderSave()

    await saveFile(PATH, 'saved bytes')

    expect(bannerEl()).toBeNull()
    expect(patchTab).toHaveBeenCalledWith(`file:${PATH}`, { savedContent: 'saved bytes' })
    // The draft differs from what was saved, so it is newer work and is kept.
    expect(getInlineDraft(SLOT, PATH)).toBe('newer draft')
  })
})
