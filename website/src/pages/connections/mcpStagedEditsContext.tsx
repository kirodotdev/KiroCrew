import { createContext, useContext, useMemo, useState, type ReactNode } from 'react'

/**
 * Shared "the MCP Servers list has unapplied scope/tool edits" signal for the
 * Customize → Connections · MCP Servers panel (#24920).
 *
 * MCP Management and the MCP Servers table (`McpTab`) are SIBLINGS in that panel.
 * McpTab holds staged edits (`pending`/`pendingTools`) in its own state until the
 * user presses Apply. MCP Management's error notices offer an "Ask the agent"
 * hand-off that navigates to /chat, which unmounts the whole panel — and with it
 * McpTab's unsaved edits. Nothing else couples the two, so the fact that edits
 * are staged is published here and read there.
 *
 * Why a context rather than a leave guard: a guard that blocked the navigation
 * would also block every OTHER exit (rail links, Back) while edits are staged —
 * behaviour McpTab never had — and could only prompt with a native confirm, which
 * this renderer's lint forbids for new call sites. Suppressing just the hand-off
 * is the narrow fix the reviewers asked for: the "Ask the agent" affordance is not
 * offered while edits are unapplied, exactly as McpTab already does for its OWN
 * notices (`askAgent={pendingCount === 0}`).
 *
 * Default `false` (and a no-op setter) so MCP Management rendered anywhere else —
 * a test, or any future standalone mount — keeps offering the hand-off.
 */
type McpStagedEdits = {
  /** True while the sibling MCP Servers list holds unapplied edits. */
  staged: boolean
  /** Called by the MCP Servers list as its staged-edit count crosses 0. */
  setStaged: (staged: boolean) => void
}

const McpStagedEditsContext = createContext<McpStagedEdits>({ staged: false, setStaged: () => {} })

/** Wraps the MCP Servers panel so its table and MCP Management share one signal. */
export function McpStagedEditsProvider({ children }: { children: ReactNode }) {
  const [staged, setStaged] = useState(false)
  const value = useMemo(() => ({ staged, setStaged }), [staged])
  return <McpStagedEditsContext.Provider value={value}>{children}</McpStagedEditsContext.Provider>
}

/**
 * Whether a chat hand-off here would drop the sibling list's unapplied edits.
 * MCP Management reads this and withholds its "Ask the agent" affordance while it
 * is true; outside the provider it is always `false`, so nothing else changes.
 */
export function useMcpHandoffWouldLoseEdits(): boolean {
  return useContext(McpStagedEditsContext).staged
}

/** The setter the MCP Servers list uses to publish its staged-edit state. */
export function useSetMcpStagedEdits(): (staged: boolean) => void {
  return useContext(McpStagedEditsContext).setStaged
}
