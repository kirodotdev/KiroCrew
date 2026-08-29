/**
 * A rejected `deleteSlot` must reach the user.
 *
 * The close funnel and both mode-switch routes each retire a tab by creating a
 * replacement and deleting the original. When that DELETE is refused the original
 * survives, so discarding the rejection leaves the user holding two sessions with
 * nothing on screen saying so — the failure mode these tests pin.
 */
import { readFileSync } from 'fs'
import { join } from 'path'
import { describe, expect, it } from 'vitest'

const HOOK = readFileSync(join(__dirname, '..', 'hooks', 'useSessionActions.ts'), 'utf-8')
const CHAT_PAGE = readFileSync(join(__dirname, '..', 'pages', 'ChatPage.tsx'), 'utf-8')
const SESSION_CTRL = readFileSync(join(__dirname, '..', 'pages', 'chat', 'useChatPageSessionController.ts'), 'utf-8')
const EN = JSON.parse(readFileSync(join(__dirname, '..', 'i18n', 'locales', 'en.manual.json'), 'utf-8'))

describe('a refused session delete is surfaced, not swallowed', () => {
  it('does not fire the close funnel deleteSlot without reading its outcome', () => {
    expect(HOOK).not.toContain('dispatch(deleteSlot(slotKey))\n')
    expect(HOOK).toContain('await dispatch(deleteSlot(slotKey)).unwrap()')
  })

  it('routes the funnel failure to a caller sink, falling back to the surviving session', () => {
    expect(HOOK).toContain('if (opts?.onError) opts.onError(message)')
    expect(HOOK).toContain('appendSlotMessage({ slot: slotKey')
    // The fallback names the slot that SURVIVED; sending it to the replacement
    // would put the notice in a tab the user was not told about.
    const fallback = HOOK.slice(HOOK.indexOf('dispatch(appendSlotMessage'))
    expect(fallback.slice(0, 120)).toContain('slot: slotKey')
    // Guarded, because a host whose store cannot hold a row must not turn a failed
    // delete into a thrown reducer on top of it.
    expect(HOOK).toContain('close failed and its notice could not be shown')  })

  it('offers the sink on both the interface and the implementation', () => {
    expect(HOOK.match(/onError\?: \(message: string\) => void/g)?.length).toBe(2)
  })

  it('surfaces the resume-route delete failure through ErrorNotice', () => {
    // The resume route lives in useChatPageSessionController now; the mode-switch
     // routes below still live in ChatPage, which is why only this one moved.
    expect(SESSION_CTRL).not.toContain('dispatch(deleteSlot(activeSlot)).unwrap().catch(() => {})')
    expect(SESSION_CTRL).toContain("showActionError(err instanceof Error && err.message ? err.message : i18nT('pages.chatPage.close_old_session_failed'))")
  })

  it('surfaces the mode-switch delete failure, keeping the succession retraction', () => {
    // One route (memory mode) remains: the clean-mode toggle that shared this shape
    // was removed upstream. The reporting requirement is unchanged for the survivor.
    const raises = CHAT_PAGE.match(/showActionError\(err instanceof Error[^\n]*close_old_session_failed_title'\)\)/g)
    expect(raises?.length).toBe(1)
    expect(CHAT_PAGE.match(/forgetSlotSuccession\(activeSlot\)/g)?.length).toBe(2)
    expect(CHAT_PAGE).not.toContain('} catch {\n                      // The slot survives')
  })

  it('carries the copy the surfaces render', () => {
    expect(EN.hooks.useSessionActions.close_failed).toBeTruthy()
    expect(EN.pages.chatPage.close_old_session_failed).toBeTruthy()
    expect(EN.pages.chatPage.close_old_session_failed_title).toBeTruthy()
    // The mode-switch message must name the CONSEQUENCE, not just the failure:
    // "it did not close" leaves the duplicate for the user to discover.
    expect(EN.pages.chatPage.close_old_session_failed).toMatch(/two/i)
  })

  it('keeps the failure out of the dependency-free render path', () => {
    // showActionError is a useCallback, so a consumer omitting it from its deps
    // captures a stale setter and the notice never repaints.
    expect(SESSION_CTRL).toContain('saveDrafts, showActionError])')
  })

  it('drops the resumed-away drafts only AFTER the archive succeeds', () => {
    const CTRL = readFileSync(
      join(__dirname, '..', 'pages', 'chat', 'useChatPageSessionController.ts'), 'utf-8')
    const lines = CTRL.split('\n')
    const del = lines.findIndex(l => l.includes('await dispatch(deleteSlot(activeSlot)).unwrap()'))
    expect(del).toBeGreaterThan(-1)
    const drop = lines.findIndex(l => l.includes('delete drafts.current[activeSlot]'))
    expect(drop).toBeGreaterThan(-1)
    // Dropping FIRST destroyed the draft on a failed cleanup: the tab stayed alive and
    // empty, holding work the user consented to move away from, not to lose.
    expect(drop).toBeGreaterThan(del)
  })

  it('gives the resume cleanup its own catch, so the outer swallow cannot hide it', () => {
    const CTRL = readFileSync(
      join(__dirname, '..', 'pages', 'chat', 'useChatPageSessionController.ts'), 'utf-8')
    expect(CTRL).toContain('} catch (err: unknown) {')
    expect(CTRL).toContain("i18nT('pages.chatPage.close_old_session_failed')")
  })
})
