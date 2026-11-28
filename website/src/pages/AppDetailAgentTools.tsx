/**
 * The app detail page's agent tools card: a third-party app's approved tools (on) and
 * the ones the owner turned off (off), each with a switch. A tool an install or update
 * holds back is not here: the page's approval notice offers it. Off sends
 * `agentToolsRevoke` on the owner-only enable route, which takes the entry out and
 * keeps the tool off by name across updates. On asks the owner first and then
 * approves only that entry, through the same `grantsConsent` the notice sends. On a
 * disabled app that route would also switch the app on, so a tool can only be turned
 * off there.
 */
import { useId, useState } from 'react'
import { api } from '../api/client'
import type { AgentToolGrant } from '../components/appstore/agentTools'
import { askAppConfirm } from '../lib/appConfirm'
import { Card, CardTitle, Toggle } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import { i18nT } from '../i18n/t'
import { compareText } from '../i18n/format'

function confirmTurnOn(tool: AgentToolGrant): Promise<boolean> {
  return askAppConfirm({
    title: i18nT('pages.appDetailPage.agent_tool_turn_on_title', { tool: tool.name }),
    body: (
      <>
        {tool.description && <p>{tool.description}</p>}
        <p className="mt-2">{i18nT('pages.appDetailPage.agent_tool_turn_on_body')}</p>
      </>
    ),
    confirmLabel: i18nT('pages.appDetailPage.agent_tool_turn_on'),
    danger: false,
    primary: true,
  })
}

export default function AppDetailAgentTools({ appName, enabled, grant, onChanged }: {
  appName: string
  enabled: boolean
  grant?: { approved: AgentToolGrant[]; staged: AgentToolGrant[]; revoked?: AgentToolGrant[] }
  onChanged?: () => void
}) {
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState('')
  const enableFirstId = useId()
  // One order by name, whatever the switch says: a row that flips stays under the pointer.
  const tools = [
    ...(grant?.approved || []).map(tool => ({ tool, on: true })),
    ...(grant?.revoked || []).map(tool => ({ tool, on: false })),
  ].sort((a, b) => compareText(a.tool.name, b.tool.name))
  if (tools.length === 0) return null
  // A revoked tool on a disabled app: turning it on would also switch the app on.
  const heldByDisabledApp = !enabled && tools.some(({ on }) => !on)

  const change = async (tool: AgentToolGrant, on: boolean) => {
    setBusy(tool.entry)
    setError('')
    try {
      if (on) {
        if (!(await confirmTurnOn(tool))) return
        await api.enableApp(appName, false, { api: [], events: [], agentTools: [tool.entry] })
      } else {
        await api.revokeAgentTools(appName, [tool.entry])
      }
      onChanged?.()
    } catch (e) {
      setError(i18nT('pages.appDetailPage.agent_tool_change_failed', { tool: tool.name, error: e instanceof Error ? e.message : String(e) }))
    } finally {
      setBusy(null)
    }
  }

  return (
    <Card data-testid="app-agent-tools">
      <CardTitle>{i18nT('components.appstore.trustAppModal.agent_tools_heading')}</CardTitle>
      <ul className="mt-2 grid gap-2">
        {tools.map(({ tool, on }) => (
          <li key={tool.entry} className="flex items-start gap-3 text-[13px]">
            <div className="min-w-0 flex-1">
              <code className="max-w-full break-all bg-bg-elevated border border-border px-1.5 py-0.5 rounded text-[11px] text-text">{tool.name}</code>
              {tool.description && <span className="text-muted"> {tool.description}</span>}
            </div>
            <Toggle
              checked={on}
              label={tool.name}
              tone="muted"
              disabled={!on && !enabled}
              describedBy={!on && !enabled ? enableFirstId : undefined}
              busy={busy === tool.entry}
              onChange={next => { if (busy === null) void change(tool, next) }}
            />
          </li>
        ))}
      </ul>
      {heldByDisabledApp && (
        <div id={enableFirstId} className="mt-2 text-[12px] text-muted">{i18nT('pages.appDetailPage.agent_tool_enable_app_first')}</div>
      )}
      {error && <ErrorNotice message={error} variant="inline" askAgent />}
    </Card>
  )
}
