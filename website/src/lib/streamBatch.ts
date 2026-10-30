/** Deferred store notification for stream traffic of sessions that are not open.
 *
 *  Every dispatch notifies every store subscriber, and each mounted
 *  `useAppSelector` re-runs its selector on that notification: a few hundred
 *  selectors per dispatch with the sidebar and an open transcript mounted.
 *  Background sessions stream chunks, status changes, tool calls and recency
 *  bumps many times a second each, so most of that work is spent on sessions
 *  nobody is looking at.
 *
 *  The store's `autoBatchEnhancer` (on by default in `configureStore`) holds
 *  back the notification for an action carrying `meta[SHOULD_AUTOBATCH]` and
 *  sends one notification at the next animation frame for all of them. The
 *  reducer still runs at dispatch, so `getState()` is current at once; only
 *  subscribers hear about it later, at most one frame on. Any untagged
 *  dispatch in between notifies immediately and covers the held ones too.
 *
 *  The open session's traffic stays untagged, so its transcript renders on the
 *  frame its text arrives, as before. */
import { SHOULD_AUTOBATCH, type UnknownAction } from '@reduxjs/toolkit'
import { store } from '../store'

/** `action` with its notification deferred to the next frame. */
export function autoBatched<A extends UnknownAction>(action: A): A {
  const meta = action.meta && typeof action.meta === 'object' ? action.meta : {}
  return { ...action, meta: { ...meta, [SHOULD_AUTOBATCH]: true } }
}

/** `action` deferred when `slot` names a session other than the open one.
 *  `activeSlot` may be passed by a caller that already read it for the same
 *  batch; otherwise it is read from the store. */
export function batchedUnlessOpen<A extends UnknownAction>(
  slot: string | undefined,
  action: A,
  activeSlot: string | null = store.getState().chat.activeSlot,
): A {
  return slot && slot !== activeSlot ? autoBatched(action) : action
}
