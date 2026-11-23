import { memo, useMemo, useState, useSyncExternalStore } from 'react'

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
 * The marks of the "this project has hook files in .kiro/hooks that do not run"
 * notices the user dismissed, as a JSON array. Per folder, not global: a mark
 * hashes the folder and its hook file names, so the notice shows again in
 * another folder, or once a file there is added, removed or renamed, where a new
 * file may be a hook meant to refuse a tool call. Names only, so a file whose
 * content changes stays hidden. Written only by the click, through
 * `savePrefExplicitly`, and backed up to the host (`lib/uiPrefs.ts`).
 */
export const PROJECT_HOOK_FILES_DISMISSED_KEY = 'kirocrew:project-hook-files-notice-dismissed'

/**
 * The rows' tags, mirroring `SPEC_HOOKS_NOTICE_KIND`,
 * `SPEC_HOOKS_RUN_NOWHERE_NOTICE` and `SPEC_HOOKS_PROJECT_HOOK_FILES_NOTICE` in
 * `src/kiro_crew/dashboard/chat_utils_injected_rows.py`
 * (`test/test_crew_fired_spec_hooks.py` pins both sides).
 */
const SPEC_HOOKS_KIND = 'spec_hooks'
const RUN_NOWHERE = 'run_nowhere'
const PROJECT_HOOK_FILES = 'project_hook_files'
/** The shape of the gateway's mark: 16 lowercase hex digits. */
const MARK_SHAPE = /^[0-9a-f]{16}$/

type SpecHooksMeta = { kind?: unknown; spec_hooks_notice?: unknown; project_hook_files_mark?: unknown } | undefined

/**
 * The notice row this Dismiss belongs to, matched on its tags and never on its
 * words. Only this one value matches: the other `spec_hooks` notices (project
 * hooks withheld, hooks that could not be read, too-long hook commands, a
 * folder's hook files) keep showing after a dismiss.
 */
export function isSpecHooksRunNowhereNotice(m: ChatMessage): boolean {
  if (m.role !== 'notice') return false
  const meta = m.meta as SpecHooksMeta
  return meta?.kind === SPEC_HOOKS_KIND && meta?.spec_hooks_notice === RUN_NOWHERE
}

/** Whether the user dismissed the notice, on this origin or a restored one. */
export function specHooksRunNowhereDismissed(): boolean {
  return safeGetItem(SPEC_HOOKS_RUN_NOWHERE_DISMISSED_KEY) === '1'
}

/**
 * The folder mark of a hook-files notice row, or null when *m* is not one: a
 * row written before the tag (meta `{kind: 'spec_hooks'}` only) or with a mark
 * of another shape keeps its plain card, with no link.
 */
function projectHookFilesMark(m: ChatMessage): string | null {
  if (m.role !== 'notice') return null
  const meta = m.meta as SpecHooksMeta
  const tagged = meta?.kind === SPEC_HOOKS_KIND && meta?.spec_hooks_notice === PROJECT_HOOK_FILES
  if (!tagged) return null
  const mark = meta?.project_hook_files_mark
  return typeof mark === 'string' && MARK_SHAPE.test(mark) ? mark : null
}

/** The hook-files notice row, matched on its tags and a well-formed mark. */
export function isProjectHookFilesNotice(m: ChatMessage): boolean {
  return projectHookFilesMark(m) !== null
}

/** The dismissed marks in a stored value; anything unparsable reads as none. */
function dismissedMarks(raw: string | null): string[] {
  try {
    const parsed: unknown = JSON.parse(raw ?? '[]')
    return Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === 'string') : []
  } catch {
    return []
  }
}

function projectHookFilesDismissedIn(raw: string | null, m: ChatMessage): boolean {
  const mark = projectHookFilesMark(m)
  return mark !== null && dismissedMarks(raw).includes(mark)
}

/** Whether the user dismissed this row's folder mark, here or on a restored origin. */
export function projectHookFilesNoticeDismissed(m: ChatMessage): boolean {
  return projectHookFilesDismissedIn(safeGetItem(PROJECT_HOOK_FILES_DISMISSED_KEY), m)
}

const DISMISS_KEYS: ReadonlySet<string> = new Set([SPEC_HOOKS_RUN_NOWHERE_DISMISSED_KEY, PROJECT_HOOK_FILES_DISMISSED_KEY])
const listeners = new Set<() => void>()

/** Every drawn copy hides together: a long transcript holds one per new
 *  session, and another tab's dismiss arrives as a storage event. */
function subscribe(onChange: () => void): () => void {
  listeners.add(onChange)
  const onStorage = (e: StorageEvent) => {
    if (e.key === null || DISMISS_KEYS.has(e.key)) onChange()
  }
  window.addEventListener('storage', onStorage)
  return () => {
    listeners.delete(onChange)
    window.removeEventListener('storage', onStorage)
  }
}

function saveDismissal(key: string, value: string): boolean {
  if (!savePrefExplicitly(key, value)) return false
  for (const onChange of [...listeners]) onChange()
  return true
}

function dismissRunNowhere(): boolean {
  return saveDismissal(SPEC_HOOKS_RUN_NOWHERE_DISMISSED_KEY, '1')
}

/** Adds *mark* to the list as stored NOW, never a copy read earlier, so a mark
 *  another tab dismissed in between is kept. */
function dismissProjectHookFiles(mark: string): boolean {
  const marks = dismissedMarks(safeGetItem(PROJECT_HOOK_FILES_DISMISSED_KEY))
  if (marks.includes(mark)) return true
  return saveDismissal(PROJECT_HOOK_FILES_DISMISSED_KEY, JSON.stringify([...marks, mark]))
}

/** Re-renders its caller when the notice is dismissed here or in another tab. */
export function useSpecHooksRunNowhereDismissed(): boolean {
  return useSyncExternalStore(subscribe, specHooksRunNowhereDismissed)
}

/** The stored marks as their raw string: a snapshot must be a primitive, or
 *  `useSyncExternalStore` sees a new value on every read and loops. */
function readProjectHookFilesDismissed(): string | null {
  return safeGetItem(PROJECT_HOOK_FILES_DISMISSED_KEY)
}

/**
 * Whether a row is a notice the user dismissed, so it draws nothing: the
 * run-nowhere notice once dismissed anywhere, a hook-files notice once its
 * folder mark is. Re-renders its caller on a dismiss here or in another tab.
 */
export function useDismissedSpecHooksNotice(): (m: ChatMessage) => boolean {
  const runNowhere = useSpecHooksRunNowhereDismissed()
  const hookFiles = useSyncExternalStore(subscribe, readProjectHookFilesDismissed)
  return useMemo(
    () => (m: ChatMessage) => (runNowhere && isSpecHooksRunNowhereNotice(m)) || projectHookFilesDismissedIn(hookFiles, m),
    [runNowhere, hookFiles],
  )
}

/** A notice card with a Dismiss link after its copy, and the inline error a
 *  refused save shows. */
function DismissibleNotice({
  content,
  askAgent,
  onDismiss,
  testIdPrefix,
}: {
  content: string
  askAgent: boolean
  /** Stores the dismissal; false when the browser refused the save. */
  onDismiss: () => boolean
  testIdPrefix: string
}) {
  const [saveError, setSaveError] = useState<string | null>(null)
  return (
    <>
      <NoticeCard
        content={content}
        action={
          <button
            type="button"
            data-testid={`${testIdPrefix}-dismiss`}
            className="whitespace-nowrap underline underline-offset-2 hover:text-text focus-visible:text-text"
            onClick={() => {
              if (!onDismiss()) {
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
        testId={`${testIdPrefix}-save-error`}
        className="mt-1"
      />
    </>
  )
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
  if (dismissed) return null
  return <DismissibleNotice content={content} askAgent={askAgent} onDismiss={dismissRunNowhere} testIdPrefix="spec-hooks-notice" />
})

/**
 * The "this project has hook files in .kiro/hooks that do not run" notice with
 * a Dismiss link that hides it for this folder and its current hook file names,
 * in every chat and tab. Like the run-nowhere Dismiss it changes only what the
 * dashboard draws: the gateway still appends the row once per chat, and no hook
 * runs or stops running because of it. No setting brings it back.
 */
export const ProjectHookFilesNotice = memo(function ProjectHookFilesNotice({
  message,
  askAgent,
}: {
  message: ChatMessage
  /** As on the run-nowhere notice. */
  askAgent: boolean
}) {
  useLanguageGeneration()
  const raw = useSyncExternalStore(subscribe, readProjectHookFilesDismissed)
  const mark = projectHookFilesMark(message)
  if (mark === null || projectHookFilesDismissedIn(raw, message)) return null
  return (
    <DismissibleNotice
      content={message.content}
      askAgent={askAgent}
      onDismiss={() => dismissProjectHookFiles(mark)}
      testIdPrefix="project-hook-files-notice"
    />
  )
})
