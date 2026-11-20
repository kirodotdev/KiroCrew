/**
 * Tell the gateway what became of a Run in terminal click, so the crewmate whose
 * chat the command came from hears it (`POST /api/chat/slots/{slot}/terminal-run`,
 * `dashboard/handlers/mate_terminal_run.py`). The gateway decides whether `slot` is
 * a crewmate's own thread; any other chat is told nothing.
 *
 * Fire and forget: it never navigates, never shows anything, and a failed request
 * is dropped. The run itself has already happened (or not) by the time this is sent.
 */

export type TerminalRunOutcome = 'ran' | 'copied' | 'failed'
export type TerminalRunReason = 'not_ready' | 'remote_chat' | 'no_tab' | 'not_sent' | 'copy_failed'
export type TerminalRunSource = 'run_yourself' | 'code_block'

/** Path of the note route, with the slot key encoded the way the request sends it. */
export const terminalRunNotePath = (slot: string): string =>
  '/api/chat/slots/' + encodeURIComponent(slot) + '/terminal-run'

export function noteTerminalRun(
  slot: string,
  note: { command: string; outcome: TerminalRunOutcome; reason?: TerminalRunReason; source: TerminalRunSource },
): void {
  if (!slot || !note.command) return
  try {
    void fetch(terminalRunNotePath(slot), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: note.command, outcome: note.outcome, reason: note.reason ?? '', source: note.source }),
    }).catch(() => undefined)
  } catch {
    // No fetch in this environment: nothing to tell.
  }
}
