import { useEffect, useId, useRef, useState, useSyncExternalStore } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Check, ChevronRight, Copy, MessageSquarePlus, RotateCw, ScrollText } from 'lucide-react'

import { api } from '../../api/client'
import ErrorNotice, { ErrorNoticeMenuItem } from '../../components/ErrorNotice'
import { useCopyAck } from '../../components/FilePathMenu'
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
} from '../../components/ui/dropdown-menu'
import { useIsTouchDevice } from '../../hooks/useIsTouchDevice'
import { i18nT } from '../../i18n/t'
import { useAppDispatch, useAppSelector, useAppStore } from '../../store'
import { slotRowByKey } from '../../store/slotIndex'
import { errMessage } from '../../utils/thunkError'
import { loadInitialPrompt } from './initialPrompt'
import { startFromInitialPrompt } from './startFromInitialPrompt'

/**
 * The session header menu's "Initial prompt" submenu (#25977): the first
 * message the person sent in this session, a Copy for it, and "Draft a new
 * session from this prompt", which opens a fresh session on the same agent,
 * project and sidebar folder with that prompt in the composer, unsent, ready
 * to edit.
 *
 * The prompt is read lazily, when the submenu opens (the MCP-servers submenu
 * beside it does the same), from the START of the transcript: the open pane only
 * holds the newest page of a long session. A failed read offers Retry in place.
 * The new session is made by `startFromInitialPrompt`, and the prompt rides the
 * create as `prefill`, so it lands in the new session's composer and nowhere
 * else, and a session the user opened while the create ran keeps the view.
 */
/** Below this window width the flyout cannot sit beside the parent menu: the
 *  menu (at least 180px) plus the flyout (at least 200px) plus their edges do
 *  not fit on either side of a menu opened mid-header. */
const NO_ROOM_FOR_FLYOUT_QUERY = '(max-width: 767px)'

function subscribeNoRoom(cb: () => void) {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {}
  const mql = window.matchMedia(NO_ROOM_FOR_FLYOUT_QUERY)
  mql.addEventListener?.('change', cb)
  return () => mql.removeEventListener?.('change', cb)
}
const noRoomNow = () =>
  typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia(NO_ROOM_FOR_FLYOUT_QUERY).matches

/** The window is too narrow for the flyout. A pure width question, so unlike
 *  `useIsMobile` it has no `/embed/` carve-out: a narrow embedded chat has no
 *  more room for a flyout than any other window. */
function useNoRoomForFlyout(): boolean {
  return useSyncExternalStore(subscribeNoRoom, noRoomNow, () => false)
}

export default function InitialPromptSubmenu({ slotKey, mode, onStarted }: {
  slotKey: string
  mode?: string
  /** Called once the new session took the view, so the host can close its menu. */
  onStarted?: () => void
}) {
  const dispatch = useAppDispatch()
  const store = useAppStore()
  const slot = useAppSelector(s => slotRowByKey(s.dashboard.slots, slotKey))
  const [open, setOpen] = useState(false)
  // On a touch device, and in a window too narrow for a flyout beside the
  // parent menu (a 320px desktop window has no room for one on either side),
  // the submenu renders inline inside the parent menu (PhoneSubContentDiv), so
  // it takes the parent's width; the flyout width below is for a wide window.
  const isTouch = useIsTouchDevice()
  const isNarrow = useNoRoomForFlyout()
  const isPhone = isTouch || isNarrow
  const { data: prompt = '', isPending, isError, isFetching, refetch } = useQuery<string>({
    queryKey: ['initial-prompt', slotKey],
    queryFn: () => loadInitialPrompt(slotKey, (key, limit, before) => api.chatSlotDetail(key, limit, before)),
    enabled: open,
    // Re-read on every open. The shared client never marks data stale, so an
    // empty answer cached before the first send would otherwise keep saying
    // "no prompt yet" after the prompt exists.
    staleTime: 0,
    retry: false,
  })
  // A retry after a failed read keeps `isError` until it settles, so it reads
  // as loading while it runs: the notice must not stay up over a live retry.
  const loading = open && isFetching && (isPending || isError)

  const { copyStatus, copyPath: copyPrompt } = useCopyAck(prompt)
  // A refused copy stays said until a copy succeeds (SessionActionsMenu's
  // Copy session ID rule): the transient label alone is gone in 1.5s.
  const [copyFailed, setCopyFailed] = useState(false)
  useEffect(() => {
    if (copyStatus === 'failed') setCopyFailed(true)
    else if (copyStatus === 'copied') setCopyFailed(false)
  }, [copyStatus])
  // The clipboard's fallback layer (plain-HTTP gateways, a refused permission)
  // copies through a hidden textarea it focuses for a moment. A submenu closes
  // when focus leaves it, so without this the copy would shut the submenu and
  // take its own acknowledgment or failure notice off screen. Held only while
  // a copy this submenu started is in flight.
  const copyingRef = useRef(false)
  const copyNow = () => {
    copyingRef.current = true
    void copyPrompt().finally(() => { copyingRef.current = false })
  }

  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)
  const copyErrorId = useId()
  const loadErrorId = useId()
  const startErrorId = useId()

  const startFromPrompt = async () => {
    if (!prompt || starting) return
    setStarting(true)
    setStartError(null)
    try {
      // The rules (project carry, origin guard, prefill fallback) live in
      // startFromInitialPrompt so they can be pinned without the menu.
      if (await startFromInitialPrompt(dispatch, store.getState, { prompt, source: slot, mode })) onStarted?.()
    } catch (err) {
      setStartError(errMessage(err) || i18nT('pages.chat.initialPrompt.new_session_failed_detail'))
    } finally {
      setStarting(false)
    }
  }

  const copyLabel = copyStatus === 'copied'
    ? i18nT('pages.chat.initialPrompt.copied')
    : i18nT('pages.chat.initialPrompt.copy_prompt')

  return (
    <DropdownMenuSub onOpenChange={setOpen} inline={isPhone}>
      <DropdownMenuSubTrigger data-testid="initial-prompt-trigger">
        <ScrollText size={13} className="shrink-0 text-muted" />
        <span className="flex-1">{i18nT('pages.chat.initialPrompt.initial_prompt')}</span>
        <ChevronRight size={12} className="text-muted" />
      </DropdownMenuSubTrigger>
      <DropdownMenuSubContent
        // Desktop flyout: never wider than the room beside the parent menu, since
        // in a narrow window neither side fits 320px. Inline on a phone, it sits
        // inside the parent menu, indented: capped below the screen width minus
        // the menu's edge, padding and that indent, so a long prompt line can
        // never stretch the menu past the screen.
        className={isPhone
          ? 'min-w-0 max-w-[min(320px,calc(100vw-64px))]'
          : 'w-[min(320px,var(--radix-dropdown-menu-content-available-width))] min-w-[200px]'}
        data-testid="initial-prompt-panel"
        onFocusOutside={(e: Event) => { if (copyingRef.current) e.preventDefault() }}
      >
        {loading ? (
          <div className="px-2 py-1.5 text-[12px] text-muted" role="status">{i18nT('pages.chat.initialPrompt.loading')}</div>
        ) : isError ? (
          <div className="px-2 py-1.5">
            <ErrorNotice
              id={loadErrorId}
              variant="inline"
              className="flex-wrap"
              title={i18nT('pages.chat.initialPrompt.load_failed')}
              message={i18nT('pages.chat.initialPrompt.load_failed_detail')}
              messagePlacement="below"
              testId="initial-prompt-load-error"
            />
          </div>
        ) : null}
        {/* The notices' hand-off rows sit beside them as menu items, the rule's
            in-menu form, so roving focus reaches each one. */}
        {!loading && isError && (
          <DropdownMenuItem
            data-testid="initial-prompt-retry"
            aria-describedby={loadErrorId}
            // Stays open: the retry's own loading line, then the prompt or the
            // notice again, renders where it was asked for.
            onSelect={(e: Event) => { e.preventDefault(); void refetch() }}
          >
            <RotateCw size={13} className="shrink-0 text-muted" aria-hidden="true" />
            {i18nT('pages.chat.initialPrompt.retry')}
          </DropdownMenuItem>
        )}
        {!loading && isError && (
          <ErrorNoticeMenuItem
            Item={DropdownMenuItem}
            message={i18nT('pages.chat.initialPrompt.load_failed_detail')}
            describedBy={loadErrorId}
          />
        )}
        {loading || isError ? null : !prompt ? (
          <div className="px-2 py-1.5 text-[12px] text-muted" data-testid="initial-prompt-empty">{i18nT('pages.chat.initialPrompt.no_prompt_yet')}</div>
        ) : (
          <>
            {/* The prompt as sent, selectable so a part of it can be copied by
                hand; long prompts scroll inside the box instead of growing the menu. */}
            <div
              data-testid="initial-prompt-text"
              className="mx-1 my-1 max-h-[200px] overflow-y-auto whitespace-pre-wrap break-words rounded-md border border-border bg-bg px-2 py-1.5 text-[12px] leading-[1.5] text-text select-text"
            >
              {prompt}
            </div>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              data-testid="initial-prompt-copy"
              // Keep the menu open so the acknowledgment (or the failure notice)
              // is seen where it was earned.
              onSelect={(e: Event) => { e.preventDefault(); copyNow() }}
              aria-describedby={copyFailed ? copyErrorId : undefined}
            >
              {copyStatus === 'copied'
                ? <Check size={13} className="shrink-0 text-ok" aria-hidden="true" />
                : <Copy size={13} className="shrink-0 text-muted" />}
              <span aria-live="polite" data-testid="initial-prompt-copy-label">{copyLabel}</span>
            </DropdownMenuItem>
            {copyFailed && (
              <div className="px-2 py-1.5">
                <ErrorNotice
                  id={copyErrorId}
                  variant="inline"
                  className="flex-wrap"
                  title={i18nT('pages.chat.initialPrompt.copy_failed')}
                  message={i18nT('pages.chat.initialPrompt.copy_failed_detail')}
                  messagePlacement="below"
                  testId="initial-prompt-copy-error"
                />
              </div>
            )}
            {copyFailed && (
              <ErrorNoticeMenuItem
                Item={DropdownMenuItem}
                message={i18nT('pages.chat.initialPrompt.copy_failed_detail')}
                describedBy={copyErrorId}
              />
            )}
            <DropdownMenuItem
              data-testid="initial-prompt-new-session"
              disabled={starting}
              // Stays open while the create runs, so a failure renders here; a
              // create that took the view closes the menu through onStarted.
              onSelect={(e: Event) => { e.preventDefault(); void startFromPrompt() }}
              aria-describedby={startError ? startErrorId : undefined}
            >
              <MessageSquarePlus size={13} className="shrink-0 text-muted" />
              {starting ? i18nT('pages.chat.initialPrompt.starting') : i18nT('pages.chat.initialPrompt.new_session_from_prompt')}
            </DropdownMenuItem>
            {startError && (
              <div className="px-2 py-1.5">
                <ErrorNotice
                  id={startErrorId}
                  variant="inline"
                  className="flex-wrap"
                  title={i18nT('pages.chat.initialPrompt.new_session_failed')}
                  message={startError}
                  messagePlacement="below"
                  testId="initial-prompt-new-session-error"
                />
              </div>
            )}
            {startError && (
              <ErrorNoticeMenuItem
                Item={DropdownMenuItem}
                message={startError}
                describedBy={startErrorId}
              />
            )}
          </>
        )}
      </DropdownMenuSubContent>
    </DropdownMenuSub>
  )
}
