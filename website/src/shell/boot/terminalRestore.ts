import { useEffect } from 'react'
import { confirmRestoredTabs, reconcileRestoredTabs } from '../../hooks/useBottomTerminal'
import { RUN_IN_TERMINAL_OPENING_GRACE_MS } from '../../utils/fenceShell'
import { confirmRestoredPanelTerminals, reconcileRestoredPanelTerminals } from '../../hooks/usePanelTabs'
import { withDeadline } from '../../lib/withDeadline'
import { setTerminalEnabledFlag } from '../../utils/terminalRegistry'
import { TERMINAL_PROBE_TIMEOUT_MS, useTerminalEnabledQuery } from '../../hooks/useTerminalEnabled'

/**
 * The shell's terminal probe: whether the terminal is enabled at all, and the
 * two-look ruling on the terminal tabs restored from storage (the docked panel's
 * and the side panel's). The ruling itself is the terminal owners'
 * (`useBottomTerminal`, `usePanelTabs`); this only feeds them the two looks.
 * The mirror effect below is the registry flag's only writer.
 */
export function useTerminalRestoreProbe(): void {
  const { data: terminalConfig, isError: terminalProbeFailed } = useTerminalEnabledQuery()
  // Hide only on an explicit opt-out (dashboard.terminal.enabled=false).
  // While the probe is loading (terminalConfig undefined) the terminal shows,
  // so there is no hidden-until-fetch-resolves flash. Keyed on the answer
  // object, not the derived boolean, so every cache write re-asserts the flag.
  useEffect(() => { setTerminalEnabledFlag(terminalConfig?.enabled !== false) }, [terminalConfig])
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
}
