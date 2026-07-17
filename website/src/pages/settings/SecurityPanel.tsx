import { useQuery } from '@tanstack/react-query'
import { ShieldCheck, ShieldAlert, Lock, Eye, EyeOff, FileWarning, Terminal, Globe, Fingerprint, KeyRound, ScanLine, Layers, AlertTriangle, CheckCircle2, ExternalLink } from 'lucide-react'
import { useAppSelector } from '../../store'
import { Badge } from '../../components/ui'
import { SettingsSection, SettingsCard } from '../../components/settings'
import { api } from '../../api/client'

/* ── Security feature registry (static — derived from security-deep-dive.md) ── */

interface SecurityFeature {
  icon: React.ReactNode
  label: string
  description: string
  layer: string
}

const FEATURES: SecurityFeature[] = [
  { icon: <Lock size={14} />, label: 'OS-Level Sandbox', description: 'User + mount namespace isolation (Linux) / Seatbelt sandbox (macOS) hides credential paths from agent subprocesses', layer: 'Layer 0' },
  { icon: <FileWarning size={14} />, label: 'Sensitive Path Blocking', description: '13 credential directories blocked at the hook layer before tool execution', layer: 'Layer 1' },
  { icon: <Terminal size={14} />, label: 'Denied Commands', description: '91+ regex patterns blocking destructive and credential-exfiltrating CLI operations', layer: 'Layer 2' },
  { icon: <AlertTriangle size={14} />, label: 'Suspicious Bash Patterns', description: '42 patterns detecting deletion, exfiltration, and pipe-execution attacks', layer: 'Layer 2' },
  { icon: <ScanLine size={14} />, label: 'MCP Input Validation', description: 'Type-safe schemas, unicode normalization, length limits, and unknown field rejection on all 12 tool handlers', layer: 'Layer 3' },
  { icon: <KeyRound size={14} />, label: 'Credential Redaction', description: 'Scans all 5 output paths for plaintext and base64-encoded AWS keys, private keys, and Slack tokens', layer: 'Layer 4' },
  { icon: <Globe size={14} />, label: 'URL Exfiltration Detection', description: 'Domain-agnostic scanning for suspicious query strings, base64 blobs, and credential patterns in URLs', layer: 'Layer 4' },
  { icon: <Eye size={14} />, label: 'SEL Audit Logging', description: 'Immutable security event trail across 8 surfaces with credential redaction before forwarding', layer: 'Layer 5' },
  { icon: <Fingerprint size={14} />, label: 'Dashboard Token Auth', description: 'HMAC-SHA256 signed, IP-pinned, single-use tokens with dual expiry (5min link + 6h session)', layer: 'Auth' },
  { icon: <ShieldCheck size={14} />, label: 'CSRF Protection', description: 'Origin/Referer validation on all POST/PUT/DELETE requests and WebSocket connections', layer: 'Auth' },
  { icon: <Layers size={14} />, label: 'Enterprise Grid Validation', description: 'Two-layer defense against data exfiltration to personal/external Slack workspaces', layer: 'Auth' },
  { icon: <EyeOff size={14} />, label: 'Observe Mode Isolation', description: 'Only owner/allowlisted messages recorded in shared channels — prevents context poisoning', layer: 'Auth' },
]

const CODE_BASE = 'https://github.com/kirodotdev/KiroCrew/blob/main'

/* ── Layer color mapping ── */
function layerColor(layer: string): 'ok' | 'aim' | 'warn' {
  if (layer.startsWith('Layer 0') || layer.startsWith('Layer 1')) return 'ok'
  if (layer === 'Auth') return 'aim'
  return 'warn'
}

/* ── Live status row ── */
function StatusRow({ icon, label, value, variant, href }: { icon: React.ReactNode; label: string; value: string; variant: 'ok' | 'err' | 'warn'; href?: string }) {
  const content = (
    <div className={`flex items-center justify-between py-2 group ${href ? 'cursor-pointer' : ''}`}>
      <div className="flex items-center gap-2.5 min-w-0">
        <span className="text-muted shrink-0">{icon}</span>
        <span className="text-[13px] font-semibold text-text group-hover:text-text-strong transition-colors">{label}</span>
      </div>
      <div className="flex items-center gap-1.5">
        <Badge variant={variant}>{value}</Badge>
        {href && <ExternalLink size={11} className="text-muted opacity-0 group-hover:opacity-100 transition-opacity" />}
      </div>
    </div>
  )
  return href
    ? <a href={href} target="_blank" rel="noopener noreferrer" className="block no-underline">{content}</a>
    : content
}

/* ── Feature row ── */
function FeatureRow({ feature }: { feature: SecurityFeature }) {
  return (
    <div className="flex items-start gap-3 py-2.5 group">
      <div className="mt-0.5 shrink-0 w-7 h-7 rounded-md bg-accent-subtle flex items-center justify-center text-accent">
        {feature.icon}
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-[13px] font-semibold text-text group-hover:text-text-strong transition-colors">{feature.label}</span>
          <Badge variant={layerColor(feature.layer)}>{feature.layer}</Badge>
        </div>
        <div className="text-[12px] text-muted mt-0.5 leading-relaxed">{feature.description}</div>
      </div>
      <CheckCircle2 size={14} className="text-ok shrink-0 mt-1" />
    </div>
  )
}

export function SecurityPanel() {
  const status = useAppSelector(s => s.dashboard.status)
  const yolo = status?.yolo ?? false
  const { data: stats } = useQuery({ queryKey: ['security-stats'], queryFn: api.securityStats, staleTime: 60_000 })

  return (
    <>
      {/* ── Data Classification Warning ── */}
      <div className="mb-5 bg-warn-subtle/50 border border-warn/30 rounded-lg p-4 flex items-start gap-3 animate-rise">
        <AlertTriangle size={18} className="text-warn shrink-0 mt-0.5" />
        <div>
          <div className="text-[13px] font-semibold text-text-strong">Data Classification Notice</div>
          <div className="text-[12px] text-muted mt-1 leading-relaxed">
            Do not enter highly sensitive or restricted data into KiroCrew. Follow your organization's data handling policy when deciding what content to share with the agent.
          </div>
        </div>
      </div>

      {/* ── Live Security Posture ── */}
      <SettingsSection title="Live Security Posture">
        <SettingsCard>
          <StatusRow icon={<Lock size={14} />} label="Process Sandbox" value="Standard" variant="ok"
            href={`${CODE_BASE}/src/kiro_crew/sandbox.py`} />
          <StatusRow
            icon={yolo ? <ShieldAlert size={14} /> : <ShieldCheck size={14} />}
            label="Tool Approval"
            value={yolo ? 'YOLO (auto-approve)' : 'Interactive'}
            variant={yolo ? 'err' : 'ok'}
          />
          <StatusRow icon={<Fingerprint size={14} />} label="Dashboard Auth" value="Token + IP-pinned" variant="ok"
            href={`${CODE_BASE}/src/kiro_crew/dashboard/token_auth.py`} />
          <StatusRow icon={<Terminal size={14} />}
            label="Denied Commands"
            value={stats ? `${stats.denied_commands + stats.suspicious_patterns} patterns` : '...'}
            variant="ok"
            href={`${CODE_BASE}/agents/defaults.json`}
          />
          <StatusRow icon={<ScanLine size={14} />}
            label="Input Validation"
            value={stats ? `${stats.tool_schemas} tool schemas` : '...'}
            variant="ok"
            href={`${CODE_BASE}/src/kiro_crew/validation.py`}
          />
          <StatusRow icon={<KeyRound size={14} />}
            label="Output Redaction"
            value={stats ? `${stats.redaction_paths} output paths` : '...'}
            variant="ok"
            href={`${CODE_BASE}/src/kiro_crew/security.py`}
          />
        </SettingsCard>
      </SettingsSection>

      {/* ── Defense-in-Depth Layers ── */}
      <SettingsSection title="Defense-in-Depth Architecture">
        <SettingsCard>
          <div className="text-[12px] text-muted mb-3 leading-relaxed">
            KiroCrew implements 6 security layers. Each layer operates independently — an attacker must bypass all layers simultaneously to succeed.
          </div>
          <div className="divide-y divide-border">
            {FEATURES.map(f => <FeatureRow key={f.label} feature={f} />)}
          </div>
        </SettingsCard>
      </SettingsSection>

      {/* ── Documentation Links ── */}
      <SettingsSection title="Documentation">
        <SettingsCard>
          <div className="flex flex-col gap-2">
            {[
              { label: 'Security Deep Dive', href: `${CODE_BASE}/docs/security-deep-dive.md` },
              { label: 'Security Module Spec', href: `${CODE_BASE}/docs/system-specs/modules/security.md` },
            ].map(link => (
              <a key={link.label} href={link.href} target="_blank" rel="noopener noreferrer" className="flex items-center gap-2 text-[13px] text-accent hover:underline py-1">
                <ExternalLink size={12} />
                {link.label}
              </a>
            ))}
          </div>
        </SettingsCard>
      </SettingsSection>
    </>
  )
}
