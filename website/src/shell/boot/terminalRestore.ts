import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { confirmRestoredTabs, reconcileRestoredTabs } from '../../hooks/useBottomTerminal'
import { RUN_IN_TERMINAL_OPENING_GRACE_MS } from '../../utils/fenceShell'
import { confirmRestoredPanelTerminals, reconcileRestoredPanelTerminals } from '../../hooks/usePanelTabs'
import { withDeadline } from '../../lib/withDeadline'
import { setTerminalEnabledFlag } from '../../utils/terminalRegistry'

/** Deadline on each look at `GET /api/terminal/sessions` during hydrate. The
 *  restored terminal tabs stay gated until both looks have settled, so a probe
 *  that never answers must be made to answer: past this bound it reads as a
 *  failed probe, which keeps every tab. Well above the route's normal
 *  round-trip (it reads an in-memory registry) and far below any wait a user
 *  would sit through for an empty panel. */
const TERMINAL_PROBE_TIMEOUT_MS = 10_000

/**
 * The shell's terminal probe: whether the terminal is enabled at all, and the
 * two-look ruling on the terminal tabs restored from storage (the docked panel's
 * and the side panel's). The ruling itself is the terminal owners'
 * (`useBottomTerminal`, `usePanelTabs`); this only feeds them the two looks.
 */
export function useTerminalRestoreProbe(): boolean {
  const { data: terminalConfig, isError: terminalProbeFailed } = useQuery({
    queryKey: ['terminal-enabled'],
    // Bounded because the restored terminal tabs below stay gated until this
    // query settles one way or the other: a request that hangs would otherwise
    // hold a blank panel open for as long as the socket did. Same bound as the
    // confirm look further down.
    queryFn: async ({ signal }) => {
      const r = await withDeadline(TERMINAL_PROBE_TIMEOUT_MS, signal, s =>
        fetch('/api/terminal/sessions', { signal: s }))
      // Default-on: the terminal is enabled unless the server explicitly says
      // otherwise. A transient/auth-timing failure of this probe must NOT hide
      // an enabled terminal by falling back to {enabled:false}, which with
      // staleTime would keep the panel hidden for 60s.
      if (!r.ok) return { enabled: true }
      return r.json()
    },
    staleTime: 60_000,
  })
  // Hide only on an explicit opt-out (dashboard.terminal.enabled=false).
  // While the probe is loading (terminalConfig undefined) the terminal shows,
  // so there is no hidden-until-fetch-resolves flash.
  const terminalEnabled = terminalConfig?.enabled !== false
  useEffect(() => { setTerminalEnabledFlag(terminalEnabled) }, [terminalEnabled])
  // The same answer weighs the terminal tabs restored from storage (#10977): a
  // tab whose session the list omits, or reports dead, is a suspect. Absent is
  // not yet gone — the route skips a session another window is still opening —
  // so suspects are confirmed by one uncached re-probe after the same opening
  // grace the run-in-terminal deadline uses, and only the ones still missing
  // are dropped, before any view reconnects to them. A probe that never answers
  // must still settle — the hosts draw no terminal until it does — so a failure
  // in either look, including a request that runs past its deadline, hands
  // over null, which keeps every tab. Both calls are once-per-load no-ops after
  // that, so the query's later refetches change nothing.
  useEffect(() => {
    if (terminalConfig === undefined && !terminalProbeFailed) return
    const first = terminalProbeFailed ? null : terminalConfig
    // The side-panel strip's restored terminals take the same two looks.
    const suspects = [...reconcileRestoredTabs(first), ...reconcileRestoredPanelTerminals(first)]
    if (suspects.length === 0) return
    void (async () => {
      await new Promise(resolve => setTimeout(resolve, RUN_IN_TERMINAL_OPENING_GRACE_MS))
      let second: unknown = null
      try {
        const r = await withDeadline(TERMINAL_PROBE_TIMEOUT_MS, undefined, s =>
          fetch('/api/terminal/sessions', { signal: s }))
        if (r.ok) second = await r.json()
      } catch { /* null: the confirm look could not rule, so every suspect stays */ }
      confirmRestoredTabs(second)
      confirmRestoredPanelTerminals(second)
    })()
  }, [terminalConfig, terminalProbeFailed])
  return terminalEnabled
}
