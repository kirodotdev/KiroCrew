import { useEffect, useRef } from 'react'

/** Whether a live control other than <body> holds focus. */
function focusIsLive(): boolean {
  const active = document.activeElement
  return Boolean(active && active !== document.body && active.isConnected)
}

/**
 * Focus the open control of row *ts* in *root*, else the first row's, else
 * *fallback*. Returns false, and moves nothing, when focus already sits on a
 * live control: a user who moved focus somewhere keeps it there.
 */
function focusFeedRow(
  root: HTMLElement | null,
  ts: string | null,
  fallback: () => HTMLElement | null,
): boolean {
  if (focusIsLive()) return false
  const row = (ts ? root?.querySelector<HTMLElement>(`[data-ts="${CSS.escape(ts)}"] [role="button"]`) : null)
    ?? root?.querySelector<HTMLElement>('[data-notif-row] [role="button"]')
  const target = row ?? fallback()
  target?.focus({ preventScroll: true })
  return Boolean(target)
}

/**
 * Keep focus in a notification feed when the note detail it opened closes.
 *
 * The detail unmounts while it holds focus (its Close button, Escape, Back,
 * an Approve or Reject that removes the note), and focus would fall to
 * <body>. When *shownTs* goes from a note to null, focus goes to that note's
 * row, else the first row, else *fallback*. An Approve or Reject closes the
 * detail before its delete lands, so the row it returned to is watched: when
 * that row then leaves *liveTs* while focus is on <body>, the first row (else
 * *fallback*) takes it. Nothing moves while *enabled* is false or a live
 * control already has focus.
 */
export function useFeedFocusOnDetailClose(
  shownTs: string | null,
  liveTs: readonly string[],
  rootRef: { readonly current: HTMLElement | null },
  fallback: () => HTMLElement | null,
  enabled: () => boolean,
): void {
  const openedTs = useRef<string | null>(null)
  const returnedTo = useRef<string | null>(null)
  const fallbackRef = useRef(fallback)
  const enabledRef = useRef(enabled)
  fallbackRef.current = fallback
  enabledRef.current = enabled

  useEffect(() => {
    if (shownTs) {
      openedTs.current = shownTs
      returnedTo.current = null
      return
    }
    const ts = openedTs.current
    openedTs.current = null
    if (!ts || !enabledRef.current()) return
    focusFeedRow(rootRef.current, ts, () => fallbackRef.current())
    returnedTo.current = ts
  }, [shownTs, rootRef])

  useEffect(() => {
    const ts = returnedTo.current
    if (!ts || liveTs.includes(ts)) return
    returnedTo.current = null
    if (!enabledRef.current()) return
    focusFeedRow(rootRef.current, null, () => fallbackRef.current())
  }, [liveTs, rootRef])
}
