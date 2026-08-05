/**
 * IncidentChat — watch the agent work an incident, and talk to it.
 *
 * The dispatch heartbeat spawns one chat slot per incident and starts the
 * investigation inside it. Without this panel that conversation is invisible from
 * the board: you can see an incident sitting in `investigating` but not what the
 * agent has actually found, and you cannot answer it when it asks. This mounts
 * the dashboard's real chat renderer against that slot, so tool activity,
 * streaming, and markdown all render exactly as they do in the main chat.
 *
 * Two wiring requirements, both easy to get silently wrong:
 *
 * 1. `ChatEmbed` reads `useAppApi()`, so it MUST have an `AppApiProvider`
 *    ancestor. A builtin page has none (AppHost only wraps installed apps), so
 *    this component mounts its own.
 * 2. The provider is permission-scoped: fetches outside `allowedApiPaths` throw.
 *    `/api/chat*` is what the embed polls and posts; `/api/approvals*` is what
 *    the Approve/Trust buttons on a tool card call — omit it and those buttons
 *    fail with no visible error. Both are declared in the app manifest.
 *
 * The slot key must match what the dispatch SOP names, or the panel renders an
 * empty conversation next to a live one.
 */
import { useCallback } from 'react'
import { AppApiProvider } from '../../app-sdk'
import ChatEmbed from '../../app-sdk/ChatEmbed'

import { i18nT } from '../../i18n/t'
/** Slot key convention shared with the dispatch SOP: one slot per incident. */
export function incidentSlotKey(incidentId: string): string {
  return `ops-mission-control-${incidentId}`
}

const ALLOWED_API = [
  '/api/apps/ops-mission-control',
  '/api/apps/ops-mission-control/*',
  '/api/chat',
  '/api/chat/*',
  '/api/approvals',
  '/api/approvals/*',
]

const ALLOWED_EVENTS = ['slots', 'notification']

export default function IncidentChat({
  incidentId,
  title,
}: {
  incidentId: string
  title?: string
}) {
  // The embed only needs these to satisfy the provider contract. Events are
  // unused here (the board polls), so subscribe is a no-op that returns its
  // unsubscribe — returning undefined would break the provider's cleanup.
  const subscribeFn = useCallback(() => () => {}, [])
  const navigateFn = useCallback((path: string) => {
    window.location.assign(path)
  }, [])
  const notifyFn = useCallback(() => {}, [])

  return (
    // Fixed-height flex column, and `min-h-0` on the growing child.
    //
    // ChatEmbed scrolls via `h-full` + an inner `flex-1 overflow-y-auto`, so it
    // only scrolls when an ANCESTOR bounds its height. Nesting it under an
    // auto-height div breaks that chain: the transcript grows without limit, the
    // input row is pushed off the bottom of the incident row, and a long
    // investigation becomes unreadable AND unanswerable. `min-h-0` is required
    // too — a flex child's default `min-height: auto` refuses to shrink below its
    // content, which silently defeats the overflow.
    <div className="mt-2 border-t border-border pt-2 flex flex-col h-[420px]">
      <p className="text-[12px] text-muted mb-2 shrink-0">
        {i18nT('apps.opsMissionControl.incidentChat.live_investigation_header', {
          incident: incidentId,
          title: title ? ` — ${title}` : '',
        })}
      </p>
      <div className="flex-1 min-h-0">
        <AppApiProvider
          appName="ops-mission-control"
          allowedApiPaths={ALLOWED_API}
          allowedEvents={ALLOWED_EVENTS}
          subscribeFn={subscribeFn}
          navigateFn={navigateFn}
          notifyFn={notifyFn}
        >
          <ChatEmbed
            slotKey={incidentSlotKey(incidentId)}
            placeholder={i18nT('apps.opsMissionControl.incidentChat.ask_about_incident', { incidentId })}
          />
        </AppApiProvider>
      </div>
    </div>
  )
}
