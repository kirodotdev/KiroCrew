import { memo, useState, useSyncExternalStore } from 'react'

import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { savePrefExplicitly } from '../../lib/uiPrefs'
import type { ChatMessage } from '../../types'
import { safeGetItem } from '../../utils/safeStorage'
import NoticeCard from './NoticeCard'

/**
 * Set once the user dismisses the session-start notice that says nothing runs
 * the agent spec's hooks on this backend. Global, like the composer's
 * list-indent hint: one Dismiss hides the notice for every agent and backend,
 * whatever agent and events a row names. The gateway leaves the tag off a row
 * that names a `preToolUse` hook, so that row has no link and keeps showing.
 * Written only by the click, through `savePrefExplicitly`, and backed up to the
 * host (`lib/uiPrefs.ts`).
 */
export const SPEC_HOOKS_RUN_NOWHERE_DISMISSED_KEY = 'kirocrew:spec-hooks-run-nowhere-notice-dismissed'

/**
 * The row's tags, mirroring `SPEC_HOOKS_NOTICE_KIND` and
 * `SPEC_HOOKS_RUN_NOWHERE_NOTICE` in `src/kiro_crew/dashboard/chat_utils_injected_rows.py`
 * (`test/test_crew_fired_spec_hooks.py` pins both sides).
 */
const SPEC_HOOKS_KIND = 'spec_hooks'
const RUN_NOWHERE = 'run_nowhere'

/**
 * The notice row this Dismiss belongs to, matched on its tags and never on its
 * words. Only this one value matches: the other `spec_hooks` notices (project
 * hooks withheld, hooks that could not be read, too-long hook commands) keep
 * showing after a dismiss.
 */
export function isSpecHooksRunNowhereNotice(m: ChatMessage): boolean {
  if (m.role !== 'notice') return false
  const meta = m.meta as { kind?: unknown; spec_hooks_notice?: unknown } | undefined
  return meta?.kind === SPEC_HOOKS_KIND && meta?.spec_hooks_notice === RUN_NOWHERE
}

/** Whether the user dismissed the notice, on this origin or a restored one. */
export function specHooksRunNowhereDismissed(): boolean {
  return safeGetItem(SPEC_HOOKS_RUN_NOWHERE_DISMISSED_KEY) === '1'
}


const listeners = new Set<() => void>()

/** Every drawn copy hides together: a long transcript holds one per new
 *  session, and another tab's dismiss arrives as a storage event. */
function subscribe(onChange: () => void): () => void {
  listeners.add(onChange)
  const onStorage = (e: StorageEvent) => {
    if (e.key === null || e.key === SPEC_HOOKS_RUN_NOWHERE_DISMISSED_KEY) onChange()
  }
  window.addEventListener('storage', onStorage)
  return () => {
    listeners.delete(onChange)
    window.removeEventListener('storage', onStorage)
  }
}

function dismiss(): boolean {
  if (!savePrefExplicitly(SPEC_HOOKS_RUN_NOWHERE_DISMISSED_KEY, '1')) return false
  for (const onChange of [...listeners]) onChange()
  return true
}

/** Re-renders its caller when the notice is dismissed here or in another tab. */
export function useSpecHooksRunNowhereDismissed(): boolean {
  return useSyncExternalStore(subscribe, specHooksRunNowhereDismissed)
}

/**
 * The "hooks have no effect" notice with a Dismiss link after its copy.
 * Dismissing changes only what the dashboard draws: the gateway still appends
 * the row, and which hooks run is decided by the backend's capabilities, not
 * here. No setting brings it back, as with the list-indent hint.
 */
export default memo(function SpecHooksRunNowhereNotice({
  content,
  askAgent,
}: {
  content: string
  /** Whether the save-failure notice may offer the agent hand-off. The host
   *  turns it off when navigating would drop its unsaved composer draft. */
  askAgent: boolean
}) {
  useLanguageGeneration()
  const dismissed = useSpecHooksRunNowhereDismissed()
  const [saveError, setSaveError] = useState<string | null>(null)
  if (dismissed) return null
  return (
    <>
      <NoticeCard
        content={content}
        action={
          <button
            type="button"
            data-testid="spec-hooks-notice-dismiss"
            className="whitespace-nowrap underline underline-offset-2 hover:text-text focus-visible:text-text"
            onClick={() => {
              if (!dismiss()) {
                setSaveError(i18nT('components.shortcutsModal.save_refused'))
                return
              }
              setSaveError(null)
            }}
          >
            {i18nT('app.dismiss')}
          </button>
        }
      />
      {/* No hand-off when the host passes askAgent={false}: the navigation would
          drop the host's own unsaved composer draft. */}
      <ErrorNotice
        variant="inline"
        message={saveError}
        askAgent={askAgent}
        testId="spec-hooks-notice-save-error"
        className="mt-1"
      />
    </>
  )
})
