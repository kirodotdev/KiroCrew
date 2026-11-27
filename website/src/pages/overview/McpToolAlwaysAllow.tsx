import { useId } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import type { McpToolGrants } from '../../api/client/mcp'
import ErrorNotice from '../../components/ErrorNotice'
import { Toggle } from '../../components/ui'
import { fmtNumber } from '../../i18n/format'
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

/** How many of the listed tools are always allowed, so the open list says it
 *  without reading every switch. It counts only tools that show a switch (a
 *  name that forms an exact grant), and says nothing until the grants have
 *  loaded, or when a refusal disables every switch and none is on. */
export function McpToolGrantsCount({ server, tools }: { server: string; tools: string[] }) {
  const { data } = useToolGrants(server)
  if (!data) return null
  const granted = new Set(data.grants.map(g => g.ref.toLowerCase()))
  const refs = tools.map(t => toolGrantRef(server, t)).filter(Boolean)
  const on = refs.filter(ref => granted.has(ref.toLowerCase())).length
  if (!refs.length || (refusalText(data.refusal) && on === 0)) return null
  return (
    <div className="text-[12px] text-muted mb-1" data-testid="mcp-always-allow-count">
      {i18nT('pages.overview.mcpTab.always_allow_on_count', { on: fmtNumber(on), total: fmtNumber(refs.length) })}
    </div>
  )
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
  // `contents`: the switch sits in the tool's own grid cell beside the tool name,
  // and the hint and error take the cell's full width under both.
  return (
    <span className="contents">
      {/* Hover names an off switch, which shows no words. */}
      <span className="inline-flex items-center gap-2" title={on ? undefined : i18nT('pages.overview.mcpTab.always_allow')}>
        <Toggle
          checked={on}
          disabled={!data || refused}
          busy={set.isPending}
          label={i18nT('pages.overview.mcpTab.always_allow')}
          describedBy={descriptionId}
          onChange={next => set.mutate(next)}
        />
        {/* The words show only beside a switch that is on, so on and off read
            apart without comparing knobs; the switch keeps its name either way.
            Their slot is kept when off, so turning one on never moves it. */}
        <span className={`text-[12px] text-text ${on ? '' : 'invisible'}`}>{i18nT('pages.overview.mcpTab.always_allow')}</span>
      </span>
      <span id={descriptionId} className={on && !refused ? 'col-span-2 text-[12px] text-muted' : 'sr-only'}>{description}</span>
      {/* No hand-off: navigating away would discard the MCP list's staged scope/tool edits. */}
      {set.isError ? <span className="col-span-2"><ErrorNotice variant="inline" message={i18nT('pages.overview.mcpTab.always_allow_failed')} report={reportForError(set.error)} askAgent={!handoffWouldLoseEdits} handoffHint={handoffHint} /></span> : null}
    </span>
  )
}
