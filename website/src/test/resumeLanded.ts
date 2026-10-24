/**
 * Lands a `resumeFromHistory` the way the real thunk does, for fixtures that
 * seed a resumed session without a network round trip: the `pending` action
 * first, then `fulfilled` carrying the origin the thunk records when it
 * starts. The fulfilled reducer opens the session only for the latest resume
 * and only when the user has not moved since (`forkMayOpen`), so a bare
 * fulfilled action with no pending and no origin no longer stands for a resume
 * that took the view.
 */
import type { UnknownAction } from '@reduxjs/toolkit'
import { captureForkOrigin, resumeFromHistory, type ChatState } from '../store/chatSlice'

type Arg = { key: string; title: string }

/** The pending and fulfilled actions for a resume started from `chat`. */
export function resumeLandedActions(chat: ChatState, payload: unknown, requestId: string, arg: Arg): [UnknownAction, UnknownAction] {
  const origin = captureForkOrigin({ chat })
  return [
    // The fulfilled action follows at once, so the entry's age never matters.
    resumeFromHistory.pending(requestId, arg, { startedAt: 0 }),
    resumeFromHistory.fulfilled(payload as never, requestId, arg, {
      originActiveSlot: origin.activeSlot,
      originActiveSlotMoves: origin.moves,
      originActiveSlotAutoMoves: origin.autoMoves,
    }),
  ]
}

/** Dispatches a landed resume into a store. */
export function landResume(
  store: { dispatch: (a: UnknownAction) => unknown; getState: () => { chat: ChatState } },
  payload: unknown,
  requestId: string,
  arg: Arg,
): void {
  for (const a of resumeLandedActions(store.getState().chat, payload, requestId, arg)) store.dispatch(a)
}

/** Applies a landed resume to a bare slice state. */
export function reduceResume<S extends ChatState>(reducer: (s: S, a: UnknownAction) => S, state: S, payload: unknown, requestId: string, arg: Arg): S {
  return resumeLandedActions(state, payload, requestId, arg).reduce(reducer, state)
}
