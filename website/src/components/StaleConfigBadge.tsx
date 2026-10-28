import { useCallback, useEffect, useRef, useState } from 'react'
import { CircleAlert, RotateCw } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { useReloadBlocked } from '../hooks/useReloadBlocked'
import { reloadFailureKey, useSessionActions } from '../hooks/useSessionActions'
import type { ChatSlot } from '../types'
import ErrorNotice from './ErrorNotice'
import { Btn } from './ui'

/** The tooltip: what changed (display-safe labels from the gateway) and the remedy. */
export function staleConfigTooltip(inputs: string | undefined): string {
  return inputs
    ? i18nT('components.staleConfigBadge.tooltip', { inputs })
    : i18nT('components.staleConfigBadge.tooltip_generic')
}

/**
 * The sidebar row's mark for a chat whose agent process runs on config changed
 * since it started (`slot.config_stale`, computed by the gateway). An alert
 * glyph rather than a refresh arrow, which reads as "syncing". A click on the
 * row opens the chat, whose header carries the Reload button.
 */
export default function StaleConfigBadge({
  slot,
}: {
  slot: Pick<ChatSlot, 'config_stale' | 'config_stale_inputs'> | undefined | null
}) {
  if (!slot?.config_stale) return null
  const tooltip = staleConfigTooltip(slot.config_stale_inputs)
  return (
    <span className="text-warn shrink-0" title={tooltip} data-testid="stale-config-badge">
      <CircleAlert size={10} aria-label={tooltip} />
    </span>
  )
}

/**
 * The phone bar's cue for a chat running on changed config, carried INSIDE the
 * session title/menu trigger: a glyph and "Config · Reload", not a control of
 * its own (the bar's centre cell already holds two). A tap on the trigger
 * opens the session menu, whose Reload row applies the change.
 */
export function StaleConfigCue({
  slot,
}: {
  slot: Pick<ChatSlot, 'config_stale' | 'config_stale_inputs'> | undefined | null
}) {
  if (!slot?.config_stale) return null
  return (
    <span className="mr-1 inline-flex items-center gap-0.5 text-[11px] font-medium text-warn" data-testid="stale-config-cue">
      <CircleAlert size={13} className="lucide-inline shrink-0 text-warn" aria-label={staleConfigTooltip(slot.config_stale_inputs)} />
      <span aria-hidden="true">{i18nT('components.staleConfigBadge.reload_action_short')}</span>
    </span>
  )
}

/**
 * The chat header's one action for a chat running on changed config: a button
 * right after the session title that reloads the session in a click. It sits in
 * the same place at every width; a narrow screen drops only the "Config
 * changed" words, and the accessible name keeps the full text. Disabled while
 * the backend would refuse the reload (a turn or sub-agents running) and while
 * its own request is in flight. A failed request goes to `onError` when the
 * host has an error region of its own (the chat header's action-error notice,
 * so the row gains no control); without one, an ErrorNotice renders right
 * after the button. Either way the button stays enabled for a retry.
 */
export function StaleConfigReloadButton({
  slot,
  mode,
  onError,
  hidden = false,
}: {
  slot: Pick<ChatSlot, 'key' | 'title' | 'config_stale' | 'config_stale_inputs'>
  mode?: string
  /** Reports a failed reload (the user-facing message) in place of the inline notice. */
  /** Shows a failed reload's message in the host's notice. A failure that lands
   *  after this button unmounted (the user switched chats mid-request) arrives
   *  with the originating chat's title, so the notice names which chat it is about. */
  onError?: (message: string, title?: string) => void
  /** Render no control but stay mounted, so a reload already in flight still
   *  reports its result to the host when the control comes back (the header
   *  hides the button while the title's Undo offer holds its place). */
  hidden?: boolean
}) {
  const slotKey = slot.key
  const { reloadBlocked } = useReloadBlocked(slotKey)
  const { reload } = useSessionActions(mode)
  // The failure is shown beside the button, not as an alert: the hook raises
  // none for a caller that takes ``onError``. Held with its slot, so a notice
  // never carries over to another chat this header shows. The host keys this
  // component by slot, so moving to another chat unmounts it; a request that
  // settles after that reports nowhere (``mounted``), since the header it would
  // report into now belongs to a different chat.
  const [failure, setFailure] = useState<{ slotKey: string; message: string } | null>(null)
  const [pending, setPending] = useState(false)
  // The ref answers a second press in the same frame, before the state commits.
  const inFlight = useRef(false)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  const onReload = useCallback(() => {
    if (inFlight.current) return
    inFlight.current = true
    setFailure(null)
    setPending(true)
    const settle = (message: string | null) => {
      inFlight.current = false
      if (!mounted.current) return
      setPending(false)
      setFailure(message ? { slotKey, message } : null)
    }
    reload(slotKey, {
      onSuccess: () => settle(null),
      // A failed request can be tried again: the button stays enabled.
      onError: err => {
        const message = i18nT(reloadFailureKey(err))
        if (!mounted.current) {
          // The chat was switched while the request ran. The host's notice
          // outlives this button, so the failure still shows there, titled with
          // the chat it belongs to rather than read as the current chat's.
          if (onError) onError(message, slot.title || slotKey)
          return
        }
        if (onError) {
          settle(null)
          onError(message)
        } else {
          settle(message)
        }
      },
    })
  }, [reload, slotKey, slot.title, onError])
  if (!slot.config_stale || hidden) return null
  const label = i18nT('components.staleConfigBadge.reload_action')
  return (
    <>
      <Btn
        type="button"
        className="shrink-0 pointer-events-auto gap-1 px-2 py-0.5 text-[12px] border-warn bg-warn-subtle text-warn-text hover:border-warn hover:bg-warn-subtle"
        disabled={reloadBlocked || pending}
        aria-busy={pending || undefined}
        title={staleConfigTooltip(slot.config_stale_inputs)}
        aria-label={label}
        data-testid="stale-config-reload"
        onClick={onReload}
      >
        {/* The arrow turns while the reload is in flight, so a disabled button
         *  working on the press reads apart from one blocked by a running turn. */}
        <RotateCw size={12} aria-hidden="true" className={`hidden sm:inline${pending ? ' animate-spin' : ''}`} />
        <span className="hidden sm:inline">{label}</span>
        {/* Phone form: the alert glyph the sidebar mark uses, so a touch user
         *  with no tooltip still sees why the button appeared. */}
        <span className="inline-flex items-center gap-1 sm:hidden" data-testid="stale-config-reload-short">
          {pending ? (
            <RotateCw size={12} aria-hidden="true" className="animate-spin" />
          ) : (
            <CircleAlert size={12} aria-hidden="true" />
          )}
          {i18nT('components.staleConfigBadge.reload_action_short')}
        </span>
      </Btn>
      {/* The agent can explain a failed reload (sub-agents still running, a
          gateway that did not answer), and the header holds no draft. */}
      <ErrorNotice
        variant="inline"
        message={failure?.slotKey === slotKey ? failure.message : null}
        askAgent
        className="min-w-0 pointer-events-auto"
        testId="stale-config-reload-error"
      />
    </>
  )
}
