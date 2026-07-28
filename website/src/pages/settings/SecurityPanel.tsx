import { useEffect, useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ShieldCheck, ShieldAlert, Lock, Eye, EyeOff, FileWarning, Terminal, Globe, Fingerprint, KeyRound, ScanLine, Layers, AlertTriangle, CheckCircle2, ExternalLink, ChevronRight, ChevronDown, Plus, Trash2, Gavel, Building2, Gauge, ToggleRight, MessageSquare, ListChecks } from 'lucide-react'
import { useAppSelector } from '../../store'
import { Badge, Btn, Input, Toggle, Checkbox } from '../../components/ui'
import { SettingsSection, SettingsCard } from '../../components/settings'
import Modal from '../../components/Modal'
import InfoTip from '../../components/InfoTip'
import { api, type DeniedCommandsData, type DeniedCommandRule, type DeniedUserRule, type GovernancePolicyData, type GovernanceScope, type GovernanceScopeDetail, type SecurityPostureData } from '../../api/client'
import { PostureDisclosureRow, CODE_BASE as POSTURE_CODE_BASE } from './PostureDisclosure'

import { i18nT } from '../../i18n/t'
/* ── Security feature registry ──
 *
 * Qualitative layer descriptions ONLY. Every control whose posture is a COUNT
 * (sensitive paths, denied commands, suspicious patterns, tool schemas,
 * redaction paths, credential families, exfil heuristics, audit surfaces, token
 * auth) is rendered from the live `GET /api/security/posture` registry instead —
 * see `PostureDisclosureRow`. Hardcoded counts here had silently gone stale by
 * several-fold — sensitive paths, bash patterns, redaction paths, and tool
 * schemas were ALL understated — so this list must stay count-free: if a description
 * needs a number, the control belongs in the posture registry.
 */

interface SecurityFeature {
  icon: React.ReactNode
  label: string
  description: string
  layer: string
}

const FEATURES: SecurityFeature[] = [
  { icon: <Lock size={14} />, label: 'OS-Level Sandbox', description: 'User + mount namespace isolation (Linux) / Seatbelt sandbox (macOS) hides credential paths from agent subprocesses', layer: 'Layer 0' },
  { icon: <FileWarning size={14} />, label: 'Sensitive Path Blocking', description: 'Credential directories and KiroCrew trust roots blocked at the hook layer before tool execution', layer: 'Layer 1' },
  { icon: <Terminal size={14} />, label: 'Denied Commands', description: 'Built-in regex patterns blocking destructive and credential-exfiltrating CLI operations (configurable below)', layer: 'Layer 2' },
  { icon: <AlertTriangle size={14} />, label: 'Suspicious Bash Patterns', description: 'Detects deletion, exfiltration, and pipe-execution attack shapes in shell commands', layer: 'Layer 2' },
  { icon: <ScanLine size={14} />, label: 'MCP Input Validation', description: 'Type-safe schemas, unicode normalization, length limits, and unknown field rejection on every tool handler', layer: 'Layer 3' },
  { icon: <KeyRound size={14} />, label: 'Credential Redaction', description: 'Scans every output path for plaintext and base64-encoded AWS keys, private keys, and provider tokens', layer: 'Layer 4' },
  { icon: <Globe size={14} />, label: 'URL Exfiltration Detection', description: 'Domain-agnostic scanning for suspicious query strings, base64 blobs, and credential patterns in URLs', layer: 'Layer 4' },
  { icon: <Eye size={14} />, label: 'SEL Audit Logging', description: 'Immutable, HMAC-chained security event trail with credential redaction before forwarding', layer: 'Layer 5' },
  { icon: <Fingerprint size={14} />, label: 'Dashboard Token Auth', description: 'HMAC-SHA256 signed, IP-pinned, single-use tokens with dual expiry on the link and the session', layer: 'Auth' },
  { icon: <ShieldCheck size={14} />, label: 'CSRF Protection', description: 'Origin/Referer validation on all POST/PUT/DELETE requests and WebSocket connections', layer: 'Auth' },
  { icon: <Layers size={14} />, label: 'Enterprise Grid Validation', description: 'Two-layer defense against data exfiltration to personal/external Slack workspaces', layer: 'Auth' },
  { icon: <EyeOff size={14} />, label: 'Observe Mode Isolation', description: 'Only owner/allowlisted messages recorded in shared channels — prevents context poisoning', layer: 'Auth' },
]

// Shared with PostureDisclosure so the repo URL lives in exactly one place.
const CODE_BASE = POSTURE_CODE_BASE

const PINNED_TOOLTIP = "Enforced by your organization's security policy"

/** Icon per posture-control key. A control the server registers that has no entry
 *  here still renders — with a generic shield — so a new backend control is never
 *  silently dropped from the panel just because the frontend hasn't been updated. */
const POSTURE_ICONS: Record<string, React.ReactNode> = {
  sensitive_paths: <FileWarning size={14} />,
  write_protected_paths: <Lock size={14} />,
  denied_commands: <Terminal size={14} />,
  suspicious_patterns: <AlertTriangle size={14} />,
  tool_schemas: <ScanLine size={14} />,
  redaction_paths: <KeyRound size={14} />,
  credential_families: <Fingerprint size={14} />,
  exfil_heuristics: <Globe size={14} />,
  audit_surfaces: <Eye size={14} />,
  token_auth: <Fingerprint size={14} />,
}

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
        {/* Slot is always rendered so linked and unlinked rows keep their badges
         *  on the same right edge — otherwise only the linked rows get pushed
         *  left by the icon's width. */}
        <span className="w-[11px] shrink-0" aria-hidden="true">
          {href && <ExternalLink size={11} className="text-muted opacity-0 group-hover:opacity-100 transition-opacity" />}
        </span>
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

/* ── Denied Commands ── */

/** Human-readable category header, e.g. "aws-destructive" → "Aws Destructive". */
function categoryLabel(category: string): string {
  return category
    .split('-')
    .map(w => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(' ')
}

/** A single built-in denied-command rule row (Card A). */
function BuiltinDenyRow({ rule, dimmed, onToggle }: { rule: DeniedCommandRule; dimmed: boolean; onToggle: (next: boolean) => void }) {
  const [open, setOpen] = useState(false)
  const Chevron = open ? ChevronDown : ChevronRight
  return (
    <div className="py-2">
      <div className="flex items-center gap-2.5">
        <button
          type="button"
          className="shrink-0 text-muted hover:text-text transition-colors bg-transparent border-none cursor-pointer p-0"
          onClick={() => setOpen(o => !o)}
          aria-label={open ? 'Hide pattern' : 'Show pattern'}
          aria-expanded={open}
        >
          <Chevron size={14} />
        </button>
        <span className="flex-1 min-w-0 text-[13px] text-text">{rule.description}</span>
        {rule.pinned ? (
          <span className="flex items-center gap-1.5 shrink-0">
            <Lock size={13} className="text-muted" />
            <InfoTip text={PINNED_TOOLTIP} />
            <Toggle checked disabled onChange={() => { /* pinned — forced on */ }} label={rule.description} />
          </span>
        ) : (
          <span className={`shrink-0 ${dimmed ? 'opacity-50' : ''}`}>
            <Toggle checked={rule.enabled} onChange={onToggle} label={rule.description} />
          </span>
        )}
      </div>
      {open && (
        <pre className="mt-1.5 ml-6 overflow-x-auto rounded-md bg-bg-elevated border border-border px-2.5 py-1.5 text-[12px] font-mono text-muted whitespace-pre-wrap break-all">{rule.pattern}</pre>
      )}
    </div>
  )
}

/** A collapsible category group (Card A) — folds its rules under a header that
 *  shows the category name, an enabled/total count, and a pinned-lock hint.
 *  Collapsed by default to keep the 137-rule panel scannable. */
function CategoryGroup({
  category,
  rules,
  open,
  onToggleOpen,
  disableAll,
  onRuleToggle,
}: {
  category: string
  rules: DeniedCommandRule[]
  open: boolean
  onToggleOpen: () => void
  disableAll: boolean
  onRuleToggle: (rule: DeniedCommandRule, next: boolean) => void
}) {
  const Chevron = open ? ChevronDown : ChevronRight
  const enabled = rules.filter(r => r.enabled).length
  const pinned = rules.some(r => r.pinned)
  // "off" when every non-pinned rule in the group is disabled.
  const allOff = enabled === 0
  return (
    <div className="border-t border-border first:border-t-0">
      <button
        type="button"
        className="w-full flex items-center gap-2 py-2.5 bg-transparent border-none cursor-pointer text-left group"
        onClick={onToggleOpen}
        aria-expanded={open}
        aria-label={`${open ? 'Collapse' : 'Expand'} ${categoryLabel(category)} rules`}
      >
        <Chevron size={14} className="shrink-0 text-muted group-hover:text-text transition-colors" />
        <span className="text-[11px] font-semibold uppercase tracking-[.04em] text-muted group-hover:text-text transition-colors">
          {categoryLabel(category)}
        </span>
        {pinned && <Lock size={12} className="shrink-0 text-muted" />}
        <span className="flex-1" />
        {allOff && !pinned && (
          <span className="text-[11px] text-warn">{i18nT('pages.settings.securityPanel.off')}</span>
        )}
        <Badge variant="muted" className="tabular-nums">{enabled}/{rules.length}</Badge>
      </button>
      {open && (
        <div className="divide-y divide-border pb-1.5 pl-6">
          {rules.map(rule => (
            <BuiltinDenyRow
              key={rule.id}
              rule={rule}
              dimmed={disableAll && !rule.pinned}
              onToggle={next => onRuleToggle(rule, next)}
            />
          ))}
        </div>
      )}
    </div>
  )
}

/** A single user-authored denied-command row (Card B). */
function CustomDenyRow({ rule, onToggle, onDelete }: { rule: DeniedUserRule; onToggle: (next: boolean) => void; onDelete: () => void }) {
  return (
    <div className="flex items-center gap-2.5 py-2">
      <code className="flex-1 min-w-0 overflow-x-auto text-[12px] font-mono text-text whitespace-pre-wrap break-all">{rule.pattern}</code>
      <Toggle checked={rule.enabled} onChange={onToggle} label={rule.pattern} />
      <button
        type="button"
        className="shrink-0 text-muted hover:text-danger transition-colors bg-transparent border-none cursor-pointer p-1"
        onClick={onDelete}
        aria-label={`Delete pattern ${rule.pattern}`}
      >
        <Trash2 size={14} />
      </button>
    </div>
  )
}

/** Add-a-custom-pattern input with client-side RegExp validation (Card B). */
function AddDenyInput({ onAdd, busy }: { onAdd: (pattern: string) => void; busy: boolean }) {
  const [value, setValue] = useState('')
  const [error, setError] = useState('')

  const submit = () => {
    const pattern = value.trim()
    if (!pattern) return
    try {
      new RegExp(pattern)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Invalid regular expression')
      return
    }
    setError('')
    onAdd(pattern)
    setValue('')
  }

  return (
    <div className="pt-1.5">
      <div className="flex items-center gap-2">
        <Input
          value={value}
          onChange={e => { setValue(e.target.value); if (error) setError('') }}
          onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); submit() } }}
          placeholder={i18nT('pages.settings.securityPanel.add_a_custom_deny_pattern_regex_e_g_rm_rf_tmp_mi')}
          aria-label={i18nT('pages.settings.securityPanel.custom_deny_pattern')}
        />
        <Btn primary onClick={submit} disabled={busy || !value.trim()}>
          <Plus size={14} />
          {i18nT('pages.settings.securityPanel.add')}
        </Btn>
      </div>
      {error && <div className="text-[12px] text-danger mt-1.5">{error}</div>}
    </div>
  )
}

/* ── Governance Policy viewer (read-only effective ceiling) ── */

/** Human-readable scope name, e.g. "capabilities.cron" → "Cron",
 *  "filesystem.read" → "Filesystem read", "sandbox.min_level" → "Sandbox". */
function scopeLabel(scope: string): string {
  const SPECIAL: Record<string, string> = {
    mcp: 'MCP servers',
    'filesystem.read': 'Filesystem read',
    'filesystem.write': 'Filesystem write',
    'network.egress': 'Network egress',
    'sandbox.min_level': 'Sandbox level',
    approval_mode: 'Tool approval',
    'capabilities.memory_writes': 'Memory writes',
    'capabilities.script_hooks': 'Script hooks',
    'capabilities.theme_persona': 'Theme persona',
    'capabilities.theme_install': 'Theme install',
  }
  if (SPECIAL[scope]) return SPECIAL[scope]
  const leaf = scope.includes('.') ? scope.slice(scope.indexOf('.') + 1) : scope
  return leaf.charAt(0).toUpperCase() + leaf.slice(1)
}

/** Pluralize a count with its noun, e.g. 3 → "3 rules", 1 → "1 rule". */
function nRules(n: number): string {
  return `${n} ${n === 1 ? 'rule' : 'rules'}`
}

/** Short human label for one governed ruleset (or a composed intersection).
 *  Works off COUNTS only — the endpoint never sends rule contents to the browser
 *  (they are the security ceiling the agent is fenced from), so the viewer shows
 *  posture: the mode and how many rules are in effect, not which. */
function rulesetLabel(d: GovernanceScopeDetail): string {
  if (d.mode === 'intersect') {
    return (d.components ?? []).map(rulesetLabel).join(' ∩ ')
  }
  if (d.mode === 'allow') {
    return (d.allow_count ?? 0) === 0 ? 'Nothing allowed' : `Allow-list · ${nRules(d.allow_count ?? 0)}`
  }
  if (d.mode === 'deny') {
    return (d.deny_count ?? 0) === 0 ? 'All allowed' : `Block-list · ${nRules(d.deny_count ?? 0)}`
  }
  return ''
}

/** Compact human label for a scope's EFFECTIVE state, by archetype. */
function effectiveLabel(row: GovernanceScope): string {
  if (!row.governed) return 'Not restricted'
  const d = row.detail
  switch (row.archetype) {
    case 'ruleset':
      return rulesetLabel(d)
    case 'ordinal':
      return `Floor: ${d.floor ?? '?'}`
    case 'capability': {
      if (!d.enabled) return 'Disabled by policy'
      const inner = Object.entries(d.inner ?? {})
      if (inner.length === 0) return 'Enabled'
      // Use rulesetLabel (not the allow-count alone) so a deny-mode inner ruleset
      // reads as a block-list, not a misleading "none".
      return `Enabled · ${inner.map(([k, v]) => `${k}: ${rulesetLabel(v)}`).join('; ')}`
    }
    case 'scopedmap': {
      const members = d.members ? rulesetLabel(d.members) : ''
      const postureN = Object.keys(d.posture ?? {}).length
      return postureN > 0 ? `${members} · posture pinned` : members
    }
    default:
      return ''
  }
}

/** Plane grouping for the viewer — a clean split by governed surface. */
interface GovPlane {
  key: string
  title: string
  icon: React.ReactNode
  scopes: string[]
}
const GOV_PLANES: GovPlane[] = [
  { key: 'access', title: 'Tools & Commands', icon: <Terminal size={13} />, scopes: ['tools', 'mcp', 'apps', 'commands'] },
  { key: 'io', title: 'Filesystem & Network', icon: <Globe size={13} />, scopes: ['filesystem.read', 'filesystem.write', 'network.egress'] },
  { key: 'channels', title: 'Messaging Channels', icon: <MessageSquare size={13} />, scopes: ['channels'] },
  { key: 'modes', title: 'Enforcement Modes', icon: <Gauge size={13} />, scopes: ['approval_mode', 'sandbox.min_level'] },
  { key: 'capabilities', title: 'Capabilities', icon: <ToggleRight size={13} />, scopes: [] /* catch-all: every capabilities.* */ },
  // Catch-all: any scope a future release (or the companion) registers that
  // matches none of the planes above and is not a capabilities.* leaf. Without
  // it, such a scope would be silently omitted, so the "all scopes" claim would
  // be false. Empty (hidden) on today's build.
  { key: 'other', title: 'Other governed scopes', icon: <ShieldCheck size={13} />, scopes: [] },
]

/** Short badge naming WHERE a governed scope's ceiling comes from. Rendered for
 *  every governed row (not just the composed case) so the viewer's source-
 *  reporting is complete: policy-only, profile-only, or the intersection. */
function sourceBadgeLabel(source: GovernanceScope['source']): string {
  switch (source) {
    case 'policy+profile':
      return 'policy ∩ profile'
    case 'profile':
      return 'profile'
    case 'policy':
      return 'policy'
    default:
      return source
  }
}

/** A single read-only governance scope row. */
function GovernanceRow({ row }: { row: GovernanceScope }) {
  const label = effectiveLabel(row)
  return (
    <div className="flex items-center justify-between py-2 gap-3">
      <div className="flex items-center gap-2 min-w-0 shrink">
        {row.governed
          ? <Lock size={12} className="lucide-inline shrink-0 text-muted" />
          : <span className="shrink-0 w-3" />}
        <span className={`text-[13px] font-semibold truncate ${row.governed ? 'text-text' : 'text-muted'}`}>{scopeLabel(row.scope)}</span>
        {row.governed && <Badge variant="muted">{sourceBadgeLabel(row.source)}</Badge>}
      </div>
      <div className="flex items-center gap-1.5 min-w-0">
        {row.governed ? (
          <>
            {/* min-w-0 + truncate so a long posture value shrinks/ellipsizes on
                narrow (mobile) widths rather than overflowing; the full value
                stays available via the title tooltip. */}
            <span className="text-[12px] text-text-strong text-right truncate" title={label}>{label}</span>
            <InfoTip text={PINNED_TOOLTIP} />
          </>
        ) : (
          <span className="text-[12px] text-muted italic shrink-0">{i18nT('pages.settings.securityPanel.not_restricted')}</span>
        )}
      </div>
    </div>
  )
}

/** Read-only viewer: the effective governance ceiling across every scope. */
function GovernancePolicyViewer() {
  const { data, isLoading, isError } = useQuery<GovernancePolicyData>({
    queryKey: ['governance-policy'],
    queryFn: api.governancePolicy,
    staleTime: 60_000,
    // The effective ceiling includes the Level-2 host PROFILE, which hot-reloads
    // at runtime — so poll modestly to keep an open Security page from showing a
    // stale ceiling after an operator edits a profile. (Level-1 policy is
    // boot-frozen, but the intersection shown here can still change with a
    // profile edit.)
    refetchInterval: 30_000,
  })
  // A failed fetch (data === undefined) must NOT read as "No enterprise policy in
  // effect" — that would tell an operator their ceiling is off when it may well
  // be on. Treat a query error as the same soft "temporarily unavailable" state
  // the backend returns via `unavailable`. Enforcement is server-side and
  // unaffected either way; this only governs what the viewer claims.
  const unavailable = isError || data?.unavailable

  const byScope = useMemo(() => {
    const m = new Map<string, GovernanceScope>()
    for (const s of data?.scopes ?? []) m.set(s.scope, s)
    return m
  }, [data])

  // Assign each scope to its plane; the Capabilities plane catches every
  // capabilities.* scope, and the "Other governed scopes" plane catches anything
  // matched by no explicit plane (e.g. a companion-registered scope) so the
  // "all scopes" claim can never silently drop a row.
  const planeRows = useMemo(() => {
    const explicit = new Set(GOV_PLANES.flatMap(p => p.scopes))
    const all = data?.scopes ?? []
    return GOV_PLANES.map(plane => {
      let rows: GovernanceScope[]
      if (plane.key === 'capabilities') {
        rows = all.filter(s => s.scope.startsWith('capabilities.'))
      } else if (plane.key === 'other') {
        rows = all.filter(s => !explicit.has(s.scope) && !s.scope.startsWith('capabilities.'))
      } else {
        rows = plane.scopes.map(sc => byScope.get(sc)).filter((s): s is GovernanceScope => !!s)
      }
      return { plane, rows }
    })
  }, [data, byScope])

  return (
    <SettingsSection title={i18nT('pages.settings.securityPanel.governance_policy')}>
      <SettingsCard>
        <div className="flex items-start gap-3 pb-1">
          <div className="mt-0.5 shrink-0 w-7 h-7 rounded-md bg-accent-subtle flex items-center justify-center text-accent">
            <Gavel size={14} className="lucide-inline" />
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-[13px] font-semibold text-text-strong">{i18nT('pages.settings.securityPanel.effective_security_ceiling')}</div>
            <div className="text-[12px] text-muted mt-0.5 leading-relaxed">
              {i18nT('pages.settings.securityPanel.the_strictest_boundary_in_effect_for_each_govern')} <strong>{i18nT('pages.settings.securityPanel.host_surface')}</strong>{i18nT('pages.settings.securityPanel.resolved_as_your_organization_s_policy_intersect')} <code className="font-mono text-[11px]">{i18nT('pages.settings.securityPanel.security_policy_json')}</code> {i18nT('pages.settings.securityPanel.and_cannot_be_changed_here')}
            </div>
          </div>
        </div>

        {isLoading ? (
          <div className="text-[12px] text-muted py-2">{i18nT('pages.settings.securityPanel.loading_governance_policy')}</div>
        ) : unavailable ? (
          <div className="flex items-start gap-2.5 py-2 mt-1">
            <AlertTriangle size={14} className="lucide-inline text-warn shrink-0 mt-0.5" />
            <span className="text-[12px] text-muted leading-relaxed">{i18nT('pages.settings.securityPanel.governance_status_is_temporarily_unavailable_enf')}</span>
          </div>
        ) : !data?.has_policy && !data?.profile ? (
          <div className="flex items-start gap-2.5 py-3 mt-1 rounded-md bg-bg-elevated border border-border px-3">
            <ShieldCheck size={16} className="lucide-inline text-ok shrink-0 mt-0.5" />
            <div>
              <div className="text-[13px] font-semibold text-text">{i18nT('pages.settings.securityPanel.no_enterprise_policy_in_effect')}</div>
              <div className="text-[12px] text-muted mt-0.5 leading-relaxed">{i18nT('pages.settings.securityPanel.no_policy_or_host_profile_restricts_the_host_sur')} <code className="font-mono text-[11px]">{i18nT('pages.settings.securityPanel.kiro_crew_security_policy_json')}</code> {i18nT('pages.settings.securityPanel.and_per_surface')} <code className="font-mono text-[11px]">{i18nT('pages.settings.securityPanel.profiles_json')}</code>.</div>
            </div>
          </div>
        ) : (
          <>
            <div className="flex items-center gap-2 mt-1 mb-1 flex-wrap">
              {data?.has_policy && (
                <Badge variant="aim"><Building2 size={11} className="lucide-inline" /> {i18nT('pages.settings.securityPanel.policy_v')}{data.version ?? '?'}</Badge>
              )}
              {data?.profile && (
                <Badge variant="muted"><ListChecks size={11} className="lucide-inline" /> {i18nT('pages.settings.securityPanel.profile')} {data.profile}</Badge>
              )}
            </div>
            {planeRows.map(({ plane, rows }) => rows.length === 0 ? null : (
              <div key={plane.key} className="border-t border-border first:border-t-0 pt-1.5 mt-1.5 first:mt-0 first:pt-0">
                <div className="flex items-center gap-1.5 py-1">
                  <span className="text-muted">{plane.icon}</span>
                  <span className="text-[11px] font-semibold uppercase tracking-[.04em] text-muted">{plane.title}</span>
                </div>
                <div className="divide-y divide-border">
                  {rows.map(row => <GovernanceRow key={row.scope} row={row} />)}
                </div>
              </div>
            ))}
          </>
        )}
      </SettingsCard>
    </SettingsSection>
  )
}

/* ── Confirm modal target ── */
type ConfirmTarget =
  | { kind: 'builtin'; id: string; description: string }
  | { kind: 'disable-all' }

export function SecurityPanel() {
  const status = useAppSelector(s => s.dashboard.status)
  const yolo = status?.yolo ?? false
  const qc = useQueryClient()
  const { data: dc, isError: dcError } = useQuery<DeniedCommandsData>({ queryKey: ['denied-commands'], queryFn: api.deniedCommands })
  // The posture registry supersedes the old flat `securityStats` counts — it
  // carries the same numbers PLUS the items behind them, so the panel reads one
  // endpoint instead of two. Long staleTime: the controls are code-derived and
  // only change on upgrade (the one runtime-variable count, denied_commands,
  // comes from the `denied-commands` query above and is invalidated on mutation).
  const { data: posture, isLoading: postureLoading, isError: postureError } = useQuery<SecurityPostureData>({
    queryKey: ['security-posture'],
    queryFn: api.securityPosture,
    staleTime: 300_000,
  })
  const controls = posture?.controls ?? []

  const [confirm, setConfirm] = useState<ConfirmTarget | null>(null)
  const [ack, setAck] = useState(false)
  // Category accordion state (Card A). Categories are collapsed by default —
  // an id in this set is EXPANDED. Keeps the 137-rule list scannable.
  const [expandedCats, setExpandedCats] = useState<Set<string>>(() => new Set())

  // The acknowledgment checkbox resets whenever the modal opens or closes.
  useEffect(() => { setAck(false) }, [confirm])

  const applySnapshot = (snap: DeniedCommandsData) => {
    qc.setQueryData(['denied-commands'], snap)
    qc.invalidateQueries({ queryKey: ['denied-commands'] })
  }

  const toggleBuiltin = useMutation({
    mutationFn: (v: { id: string; enabled: boolean }) => api.toggleBuiltinDeniedCommand(v.id, v.enabled),
    onSuccess: applySnapshot,
  })
  const setDisableAll = useMutation({
    mutationFn: (value: boolean) => api.setDeniedCommandsDisableAll(value),
    onSuccess: applySnapshot,
  })
  const addUser = useMutation({
    mutationFn: (pattern: string) => api.addUserDeniedCommand(pattern),
    onSuccess: applySnapshot,
  })
  const toggleUser = useMutation({
    mutationFn: (v: { id: string; enabled: boolean }) => api.toggleUserDeniedCommand(v.id, v.enabled),
    onSuccess: applySnapshot,
  })
  const deleteUser = useMutation({
    mutationFn: (id: string) => api.deleteUserDeniedCommand(id),
    onSuccess: applySnapshot,
  })

  const grouped = useMemo(() => {
    const groups: Record<string, DeniedCommandRule[]> = {}
    for (const rule of dc?.builtins ?? []) {
      (groups[rule.category] ??= []).push(rule)
    }
    return groups
  }, [dc])

  const disableAll = dc?.disable_all ?? false
  const governanceLocked = dc?.governance_locked ?? false
  // Enabled BUILT-INS only. `dc.effective_count` is builtins + user_added, which
  // is the right number for "rules enforced overall" but wrong for the posture
  // row, whose denominator is the built-in table: one custom deny made it read
  // "138 of 137 built-in rules".
  const enabledBuiltins = (dc?.builtins ?? []).filter(r => r.enabled).length

  // Enabling a rule (or re-enabling all built-ins) is immediate; disabling
  // opens a confirm modal. `next` is the toggle's new value.
  const onBuiltinToggle = (rule: DeniedCommandRule, next: boolean) => {
    if (next) toggleBuiltin.mutate({ id: rule.id, enabled: true })
    else setConfirm({ kind: 'builtin', id: rule.id, description: rule.description })
  }
  const onDisableAllToggle = (next: boolean) => {
    if (next) setConfirm({ kind: 'disable-all' })
    else setDisableAll.mutate(false)
  }

  const runConfirm = () => {
    if (!confirm) return
    if (confirm.kind === 'builtin') toggleBuiltin.mutate({ id: confirm.id, enabled: false })
    else setDisableAll.mutate(true)
    setConfirm(null)
  }

  const confirmBody = !confirm ? '' : confirm.kind === 'disable-all'
    ? 'Disabling all built-in denies removes KiroCrew’s protection against destructive '
      + 'and credential-exfiltration commands. Some commands may stay blocked by independent '
      + 'defense-in-depth controls (sensitive paths, IMDS, git-publish).'
    : `Disabling "${confirm.description}" weakens protection against destructive or `
      + 'credential-exfiltration commands. Some commands may stay blocked by independent '
      + 'defense-in-depth controls.'

  return (
    <>
      {/* ── Data Classification Warning ── */}
      <div className="mb-5 bg-bg-elevated border rounded-lg p-4 flex items-start gap-3 animate-rise" style={{ borderColor: 'color-mix(in srgb, var(--warn) 45%, transparent)' }}>
        <AlertTriangle size={18} className="text-warn shrink-0 mt-0.5" />
        <div>
          <div className="text-[13px] font-semibold text-text-strong">{i18nT('pages.settings.securityPanel.data_classification_notice')}</div>
          <div className="text-[12px] text-muted mt-1 leading-relaxed">
            {i18nT('pages.settings.securityPanel.do_not_enter_highly_sensitive_or_restricted_data')}
          </div>
        </div>
      </div>

      {/* ── Live Security Posture ── */}
      <SettingsSection title={i18nT('pages.settings.securityPanel.live_security_posture')}>
        <SettingsCard>
          {/* Non-expandable rows: single-valued modes, not counted sets. */}
          <StatusRow icon={<Lock size={14} />} label={i18nT('pages.settings.securityPanel.process_sandbox')} value="Standard" variant="ok"
            href={`${CODE_BASE}/src/kiro_crew/sandbox.py`} />
          <StatusRow
            icon={yolo ? <ShieldAlert size={14} /> : <ShieldCheck size={14} />}
            label={i18nT('pages.settings.securityPanel.tool_approval')}
            value={yolo ? 'YOLO (auto-approve)' : 'Interactive'}
            variant={yolo ? 'err' : 'ok'}
          />

          {/* Expandable rows, driven entirely by the live posture registry — each
              count is derived server-side from the control it describes, and
              clicking it reveals the concrete list. */}
          <div className="mt-1 pt-1 border-t border-border">
            <div className="text-[12px] text-muted pb-1 leading-relaxed">
              {i18nT('pages.settings.securityPanel.click_any_control_to_see_exactly_what_it_covers')}
            </div>
            {postureError ? (
              <div className="flex items-start gap-2.5 py-2">
                <AlertTriangle size={14} className="lucide-inline text-warn shrink-0 mt-0.5" />
                <span className="text-[12px] text-muted leading-relaxed">
                  {i18nT('pages.settings.securityPanel.security_posture_detail_is_temporarily_unavailab')}
                </span>
              </div>
            ) : postureLoading ? (
              <div className="text-[12px] text-muted py-2">{i18nT('pages.settings.securityPanel.loading_security_posture')}</div>
            ) : (
              controls.map(control => (
                <PostureDisclosureRow
                  key={control.key}
                  control={control}
                  icon={POSTURE_ICONS[control.key] ?? <ShieldCheck size={14} />}
                  // The registry counts the SHIPPED built-in rule table; the live
                  // effective count reflects the user's opt-outs and policy pins,
                  // so the pill must show the latter to match what is enforced.
                  //
                  // Three distinct states, because conflating them misreports the
                  // gate in one direction or the other:
                  //   resolved  → enabledBuiltins (what is actually enforced)
                  //   LOADING   → undefined, i.e. fall back to the server's shipped
                  //               total. Honest while in flight: it is the real rule
                  //               count, just not yet narrowed by opt-outs. Passing
                  //               null here instead would paint "unavailable" over a
                  //               fully-enforced gate — the misleading-security-signal
                  //               failure the governance viewer also guards against.
                  //   ERROR     → null, i.e. "unavailable". We cannot know the opt-out
                  //               state, so claiming the shipped total is enforced
                  //               would over-report — a rule the user disabled would
                  //               be counted as active, indefinitely (the query has
                  //               stopped retrying).
                  //
                  // Counts ENABLED BUILTINS, not `dc.effective_count`: that field is
                  // builtins + user_added, so a single custom deny made this row read
                  // "138 of 137 built-in rules" — a nonsense ratio against a
                  // built-in-only denominator. Custom rules have their own card below.
                  countOverride={control.key !== 'denied_commands'
                    ? undefined
                    : dc ? enabledBuiltins : dcError ? null : undefined}
                  note={control.key === 'denied_commands' && dc
                    ? `${enabledBuiltins} of ${dc.builtins.length} built-in rules are currently enforced, after your opt-outs and any policy pins.`
                      + (dc.user_added.length > 0
                        ? ` Your ${dc.user_added.length} custom ${dc.user_added.length === 1 ? 'pattern' : 'patterns'} are counted separately below.`
                        : '')
                    : undefined}
                />
              ))
            )}
          </div>
        </SettingsCard>
      </SettingsSection>

      {/* ── Governance Policy (read-only effective ceiling) ── */}
      <GovernancePolicyViewer />

      {/* ── Denied Commands ── */}
      <SettingsSection title={i18nT('pages.settings.securityPanel.denied_commands')}>
        {/* Card A — Built-in denies */}
        <SettingsCard>
          <div className="flex items-center justify-between py-1.5">
            <div className="flex-1 min-w-0 mr-4">
              <div className="flex items-center gap-1.5">
                <span className="text-[13px] font-semibold text-text">{i18nT('pages.settings.securityPanel.disable_all_built_in_denies')}</span>
                {governanceLocked && <Lock size={13} className="text-muted" />}
              </div>
              <div className="text-[12px] text-muted mt-0.5 leading-relaxed">
                {i18nT('pages.settings.securityPanel.turn_off_every_built_in_denied_command_rule_at_o')}{governanceLocked ? ', and rules pinned by your organization’s policy remain on.' : '.'}
              </div>
            </div>
            {/* Disable-all stays available even when governance-locked: the
                backend keeps policy-pinned rules enforced under disable_all
                (compute_effective_denied), so a pin on one rule must not block
                opting every OTHER (unpinned) rule out. When locked, show the
                pinned-policy tooltip alongside the still-functional toggle. */}
            <span className="flex items-center gap-1.5 shrink-0">
              {governanceLocked && <InfoTip text={PINNED_TOOLTIP} />}
              <Toggle checked={disableAll} onChange={onDisableAllToggle} disabled={!dc} label={i18nT('pages.settings.securityPanel.disable_all_built_in_denies')} />
            </span>
          </div>

          <div className="text-[12px] text-muted mt-1 mb-2 leading-relaxed">
            {i18nT('pages.settings.securityPanel.disabling_a_rule_that_overlaps_an_always_on_cont')}
          </div>

          {!dc ? (
            <div className="text-[12px] text-muted py-2">{i18nT('pages.settings.securityPanel.loading_built_in_rules')}</div>
          ) : (
            <>
              <div className="flex items-center justify-between mt-1 mb-0.5">
                <span className="text-[11px] text-muted">{Object.keys(grouped).length} {i18nT('pages.settings.securityPanel.categories')} {dc.builtins.length} {i18nT('pages.settings.securityPanel.rules')}</span>
                <div className="flex items-center gap-3">
                  <button
                    type="button"
                    className="text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer p-0 transition-colors"
                    onClick={() => setExpandedCats(new Set(Object.keys(grouped)))}
                  >
                    {i18nT('pages.settings.securityPanel.expand_all')}
                  </button>
                  <button
                    type="button"
                    className="text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer p-0 transition-colors"
                    onClick={() => setExpandedCats(new Set())}
                  >
                    {i18nT('pages.settings.securityPanel.collapse_all')}
                  </button>
                </div>
              </div>
              <div>
                {Object.entries(grouped).map(([category, rules]) => (
                  <CategoryGroup
                    key={category}
                    category={category}
                    rules={rules}
                    open={expandedCats.has(category)}
                    onToggleOpen={() => setExpandedCats(prev => {
                      const next = new Set(prev)
                      if (next.has(category)) next.delete(category)
                      else next.add(category)
                      return next
                    })}
                    disableAll={disableAll}
                    onRuleToggle={onBuiltinToggle}
                  />
                ))}
              </div>
            </>
          )}
        </SettingsCard>

        {/* Card B — Your custom denies */}
        <SettingsCard>
          <div className="text-[13px] font-semibold text-text">{i18nT('pages.settings.securityPanel.your_custom_denies')}</div>
          <div className="text-[12px] text-muted mt-0.5 mb-1 leading-relaxed">
            {i18nT('pages.settings.securityPanel.add_your_own_deny_patterns_python_compatible_reg')}
          </div>
          {dc && dc.user_added.length > 0 && (
            <div className="divide-y divide-border">
              {dc.user_added.map(rule => (
                <CustomDenyRow
                  key={rule.id}
                  rule={rule}
                  onToggle={next => toggleUser.mutate({ id: rule.id, enabled: next })}
                  onDelete={() => deleteUser.mutate(rule.id)}
                />
              ))}
            </div>
          )}
          <AddDenyInput onAdd={pattern => addUser.mutate(pattern)} busy={addUser.isPending} />
        </SettingsCard>
      </SettingsSection>

      {/* ── Defense-in-Depth Layers ── */}
      <SettingsSection title={i18nT('pages.settings.securityPanel.defense_in_depth_architecture')}>
        <SettingsCard>
          <div className="text-[12px] text-muted mb-3 leading-relaxed">
            {i18nT('pages.settings.securityPanel.kirocrew_implements_6_security_layers_each_layer')}
          </div>
          <div className="divide-y divide-border">
            {FEATURES.map(f => <FeatureRow key={f.label} feature={f} />)}
          </div>
        </SettingsCard>
      </SettingsSection>

      {/* ── Documentation Links ── */}
      <SettingsSection title={i18nT('pages.settings.securityPanel.documentation')}>
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

      {/* ── Confirm modal (disable a built-in rule / disable all) ── */}
      <Modal
        open={confirm !== null}
        onClose={() => setConfirm(null)}
        title={confirm?.kind === 'disable-all' ? 'Disable all built-in denies?' : 'Disable this denied command?'}
        maxWidth={480}
        footer={
          <>
            <Btn onClick={() => setConfirm(null)}>{i18nT('pages.settings.securityPanel.cancel')}</Btn>
            <Btn danger disabled={!ack} onClick={runConfirm}>{i18nT('pages.settings.securityPanel.disable')}</Btn>
          </>
        }
      >
        <div className="flex items-start gap-3">
          <AlertTriangle size={18} className="text-warn shrink-0 mt-0.5" />
          <div className="text-[13px] text-text leading-relaxed">{confirmBody}</div>
        </div>
        {/* eslint-disable-next-line jsx-a11y/label-has-associated-control, jsx-a11y/label-has-for -- the Checkbox control is nested inside the label */}
        <label className="flex items-center gap-2.5 mt-4 cursor-pointer">
          <Checkbox checked={ack} onChange={e => setAck(e.target.checked)} />
          <span className="text-[13px] text-text">{i18nT('pages.settings.securityPanel.i_understand_this_weakens_kirocrew_s_protection')}</span>
        </label>
      </Modal>
    </>
  )
}
