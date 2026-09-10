/**
 * openMergedParent — the "Open parent" action from a merged fork's read-only
 * bar (#3816 UX review).
 *
 * A merged fork can be resumed from History with its PARENT never re-opened as
 * a live slot. `switchSlot(parentKey)` then 404s on its detail fetch and
 * unwinds silently — a button that visibly does nothing. This helper resumes a
 * closed parent FIRST via the same path the History surface uses
 * (`resumeFromHistory` → POST /api/chat/slots/{key}/resume), keyed by the
 * fork's `forked_from` (the colon spelling the resume body carries, e.g.
 * "dashboard:<slot>"), then switches. A failed resume is surfaced through the
 * caller's `onError`, never swallowed.
 *
 * Extracted from ChatPage as a pure decision so the live-parent vs
 * closed-parent branch is unit-testable without the 8k-line page harness.
 */
/**
 * Fold a fork's raw `forked_from` session key to the LIVE slot-table key —
 * mirroring the server's `_normalize_slot_key` (state.py): strip the
 * `dashboard:`/`dashboard_` transport prefix, then fold every character
 * outside the persisted-filename charset (`[^\w.-]`, ASCII) to `_`. Stripping
 * only the `dashboard:` prefix left a CHANNEL parent (e.g. `slack:<ts>`)
 * colon-spelled (GPT round 8): the live-slot lookup missed, the resume then
 * created the slot under the folded spelling, and `switchTo` targeted the
 * unfolded one — so "Open parent" failed exactly for channel parents. The
 * RAW `forked_from` remains the correct RESUME key (the transcript's own
 * spelling); only lookup + switch use this fold.
 */
export function parentSlotKeyFromForkedFrom(forkedFrom: string): string {
  let key = forkedFrom.replace(/^dashboard:/, '')
  while (key.startsWith('dashboard_')) key = key.slice('dashboard_'.length)
  return key.replace(/[^A-Za-z0-9_.-]/g, '_')
}

export interface OpenMergedParentDeps {
  /** Bare parent slot key (forked_from with the `dashboard:` prefix stripped) — the live-slot key and switch target. */
  parentKey: string
  /** The colon-spelled resume key the History surface uses (the fork's raw `forked_from`). */
  resumeKey: string
  /** Live slot keys currently open. */
  liveSlotKeys: string[]
  /** Resume a closed History session; resolves to `{ ok }`. Throws on transport failure. */
  resume: (key: string) => Promise<{ ok: boolean }>
  /** Switch the active slot to an already-live (or just-resumed) parent. */
  switchTo: (key: string) => void
  /** Surface a resume failure (the file's existing error-notice mechanism). */
  onError: (err: unknown) => void
}

/**
 * Resolve "Open parent": switch straight to a live parent; otherwise resume the
 * closed parent, then switch. On a resume failure (refused or thrown) call
 * `onError` and switch nowhere.
 */
export async function openMergedParent(deps: OpenMergedParentDeps): Promise<void> {
  const { parentKey, resumeKey, liveSlotKeys, resume, switchTo, onError } = deps
  if (!liveSlotKeys.includes(parentKey)) {
    try {
      const res = await resume(resumeKey)
      if (!res?.ok) {
        onError(new Error('resume_failed'))
        return
      }
    } catch (e) {
      onError(e)
      return
    }
  }
  switchTo(parentKey)
}
