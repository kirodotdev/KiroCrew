/**
 * The app detail page's "MCP Servers" card: each server the manifest declares, with
 * its URL or command and the tools it auto-approves. Shown only when there is one.
 */
import { Card, CardTitle } from '../components/ui'
import { i18nT } from '../i18n/t'
import type { McpServerConfig } from './appDetailPageTypes'

export default function AppDetailPageMcpServers({ servers }: { servers?: Record<string, McpServerConfig> }) {
  if (!servers || Object.keys(servers).length === 0) return null
  return (
    <Card>
      <CardTitle>{i18nT('pages.appDetailPage.mcp_servers')}</CardTitle>
      <div className="grid gap-2 mt-2 text-[13px]">
        {Object.entries(servers).map(([sName, sConfig]) => (
          <div key={sName} className="bg-bg-elevated border border-border rounded-md px-2.5 py-2">
            <div className="font-mono font-medium text-text text-[12px]">{sName}</div>
            {sConfig.url && <div className="text-muted text-[11px] mt-0.5">{sConfig.url}</div>}
            {sConfig.command && <div className="text-muted text-[11px] mt-0.5">{sConfig.command}</div>}
            {(sConfig.autoApprove || []).length > 0 && (
              <div className="flex flex-wrap gap-1 mt-1.5">
                {(sConfig.autoApprove || []).map((t: string) => (
                  <span key={t} className="bg-ok-subtle border border-ok/20 px-1 py-0 rounded text-[10px] text-ok-text">{t}</span>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </Card>
  )
}
