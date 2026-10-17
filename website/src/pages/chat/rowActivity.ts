import { createContext, useContext, useLayoutEffect } from 'react'

/**
 * The channel a turn row's content uses to say "I am live". TurnBlock provides
 * one reporter per row; calling it marks the row active and returns the undo.
 * An active row never folds (see TurnBlock's fold rule).
 *
 * The row reports for itself because only its renderer knows: a tool pill
 * reads its run state, a pending approval, an MCP App payload and its own open
 * state from the store or from local state, and TurnBlock is store-free (the
 * embed SDK mounts it with no Provider). A report from a host-side map would
 * miss every pill that opened from local state, as one mounted after a reload
 * with its approval already pending does.
 */
export type RowActivityReporter = () => () => void

export const RowActivityContext = createContext<RowActivityReporter | null>(null)

/**
 * Mark the enclosing turn row active while `active` is true. A layout effect,
 * so a row that mounts active is unfolded before the first paint, never shown
 * folded for a frame. Outside a TurnBlock row this does nothing.
 */
export function useReportRowActive(active: boolean): void {
  const report = useContext(RowActivityContext)
  useLayoutEffect(() => {
    if (!active || !report) return
    return report()
  }, [active, report])
}
