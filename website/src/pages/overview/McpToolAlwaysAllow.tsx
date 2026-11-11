import { useId } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import type { McpToolGrants } from '../../api/client/mcp'
import ErrorNotice from '../../components/ErrorNotice'
import { Toggle } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { refusalText } from '../../utils/toolGrantRefusal'
import { reportForError } from '../../utils/errorReport'
import { useMcpHandoffHint, useMcpHandoffWouldLoseEdits } from '../connections/mcpStagedEditsContext'

/** Per-tool "Always allow" on a server's tool list. The same `@server/tool` grant
 *  an approval card's "Always allow this tool" records, so both show and revoke
 *  one list. The server decides eligibility: its `refusal` code disables every
 *  toggle of that server and names why, in the note above the list. */

/** Server and tool names a grant can name exactly: no wildcard, no `/`. */
export function toolGrantRef(server: string, tool: string): string {
  const exact = (name: string) => !!name && !['*', '?', '[', '/'].some(ch => name.includes(ch)) && name.trim() === name
  return exact(server) && exact(tool) ? `@${server}/${tool}` : ''
}

export const toolGrantsKey = (server: string) => ['mcp-tool-grants', server]

export function useToolGrants(server: string) {
  return useQuery({ queryKey: toolGrantsKey(server), queryFn: () => api.mcpToolGrants(server) })
}

/** The one line above a server's tools when its tools cannot be always allowed. */
export function McpToolGrantsNote({ server }: { server: string }) {
  const { data, error } = useToolGrants(server)
  const handoffWouldLoseEdits = useMcpHandoffWouldLoseEdits()
  const handoffHint = useMcpHandoffHint()
  const text = refusalText(data?.refusal)
  if (error) return (
    // No hand-off: navigating away would discard the MCP list's staged scope/tool edits.
    <ErrorNotice variant="inline" message={error.message} report={reportForError(error)} askAgent={!handoffWouldLoseEdits} handoffHint={handoffHint} />
  )
  if (!text) return null
  return <div className="text-[12px] text-muted mb-1" data-testid="mcp-always-allow-refusal">{text}</div>
}

export default function McpToolAlwaysAllow({ server, tool }: { server: string; tool: string }) {
  const qc = useQueryClient()
  const descriptionId = useId()
  const { data } = useToolGrants(server)
  const handoffWouldLoseEdits = useMcpHandoffWouldLoseEdits()
  const handoffHint = useMcpHandoffHint()
  const ref = toolGrantRef(server, tool)
  const on = !!ref && !!data?.grants.some(g => g.ref.toLowerCase() === ref.toLowerCase())
  const refusal = refusalText(data?.refusal)
  const set = useMutation({
    mutationFn: (next: boolean) => api.mcpToolGrantSet(ref, next),
    onSuccess: (snap: McpToolGrants) => { qc.setQueryData(toolGrantsKey(server), snap) },
    onError: () => { qc.invalidateQueries({ queryKey: toolGrantsKey(server) }) },
  })
  if (!ref) return null
  // Existing base grants stay removable; an overlay-owned list cannot be changed here.
  const refused = !!refusal && (!on || data?.refusal === 'set_elsewhere')
  const description = refused ? refusal : i18nT(on ? 'pages.overview.mcpTab.always_allow_undo' : 'pages.overview.mcpTab.always_allow_hint')
  return (
    <span className="inline-flex flex-col items-start gap-1">
      <span className="inline-flex items-center gap-2">
        <Toggle
          checked={on}
          disabled={!data || refused}
          busy={set.isPending}
          label={i18nT('pages.overview.mcpTab.always_allow')}
          describedBy={descriptionId}
          onChange={next => set.mutate(next)}
        />
        <span className="text-[12px] text-muted">{i18nT('pages.overview.mcpTab.always_allow')}</span>
      </span>
      <span id={descriptionId} className={on && !refused ? 'text-[12px] text-muted' : 'sr-only'}>{description}</span>
      {/* No hand-off: navigating away would discard the MCP list's staged scope/tool edits. */}
      {set.isError ? <ErrorNotice variant="inline" message={i18nT('pages.overview.mcpTab.always_allow_failed')} report={reportForError(set.error)} askAgent={!handoffWouldLoseEdits} handoffHint={handoffHint} /> : null}
    </span>
  )
}
