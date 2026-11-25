/**
 * Isolated capture entry for the details panel of a KAS sub-agent dispatch's
 * approval card, and of a bare historical tool row.
 *
 * Mounts the REAL ToolDetails and ApprovalSubagentTask against the real
 * stylesheet, theme tokens and i18n catalog. No gateway, no store.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { Lock } from 'lucide-react'

import { initI18n } from '../src/i18n/all'
import { ToolDetails } from '../src/pages/chat/ToolDetails'
import { ApprovalSubagentTask } from '../src/components/chat-input/ApprovalSubagent'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n('en')

const label = 'Sub-agent: uno-tam'

createRoot(document.getElementById('root')!).render(
  <div data-capture-root style={{ width: 760, margin: '16px auto', background: 'var(--bg)', padding: 16, display: 'flex', flexDirection: 'column', gap: 16 }}>
    <div className="rounded-xl border border-[color-mix(in_srgb,var(--warn)_40%,transparent)] bg-bg-elevated" data-testid="live-card">
      <div className="px-3.5 pt-2.5 pb-1">
        <div className="inline-flex items-start gap-1 text-[13px] font-mono px-2 py-0.5">
          <Lock size={12} className="text-warn shrink-0" style={{ marginTop: '3px' }} />
          <span className="text-muted">{label}</span>
        </div>
        <ToolDetails purpose="" pillLabel={label} toolName={label} input="" output="" auto={false} pending ts={0} hasEntry={false}
          fmtTime={() => ''} barColor="color-mix(in srgb, var(--warn) 70%, transparent)" layoutId="live" compact />
      </div>
      <ApprovalSubagentTask task="Look up the open support cases for the account and summarize them." />
      <div className="h-3" />
    </div>
    <div className="rounded-xl border border-border bg-bg-elevated px-3.5 py-2.5" data-testid="historical-row">
      <div className="text-[13px] font-mono text-muted pb-1">Fetch URL</div>
      <ToolDetails purpose="" pillLabel="Fetch URL" toolName="Fetch URL" input="" output="" auto={false} pending={false} ts={0} hasEntry={false}
        fmtTime={() => ''} barColor="color-mix(in srgb, var(--muted) 50%, transparent)" layoutId="old" />
    </div>
  </div>,
)
