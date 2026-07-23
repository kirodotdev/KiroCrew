import { useEffect, useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ShieldCheck, ShieldAlert, Lock, Eye, EyeOff, FileWarning, Terminal, Globe, Fingerprint, KeyRound, ScanLine, Layers, AlertTriangle, CheckCircle2, ExternalLink, ChevronRight, ChevronDown, Plus, Trash2 } from 'lucide-react'
import { useAppSelector } from '../../store'
import { Badge, Btn, Input, Toggle, Checkbox } from '../../components/ui'
import { SettingsSection, SettingsCard } from '../../components/settings'
import Modal from '../../components/Modal'
import InfoTip from '../../components/InfoTip'
import { api, type DeniedCommandsData, type DeniedCommandRule, type DeniedUserRule } from '../../api/client'

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
  { icon: <Terminal size={14} />, label: 'Denied Commands', description: 'Built-in regex patterns blocking destructive and credential-exfiltrating CLI operations (configurable below)', layer: 'Layer 2' },
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

const PINNED_TOOLTIP = "Enforced by your organization's security policy"

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
          <span className="text-[11px] text-warn">off</span>
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
          placeholder="Add a custom deny pattern (regex), e.g. rm -rf /tmp/mine"
          aria-label="Custom deny pattern"
        />
        <Btn primary onClick={submit} disabled={busy || !value.trim()}>
          <Plus size={14} />
          Add
        </Btn>
      </div>
      {error && <div className="text-[12px] text-danger mt-1.5">{error}</div>}
    </div>
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
  const { data: stats } = useQuery({ queryKey: ['security-stats'], queryFn: api.securityStats, staleTime: 60_000 })
  const { data: dc } = useQuery<DeniedCommandsData>({ queryKey: ['denied-commands'], queryFn: api.deniedCommands })

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
    qc.invalidateQueries({ queryKey: ['security-stats'] })
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
            value={dc ? `${dc.effective_count} active` : '...'}
            variant="ok"
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

      {/* ── Denied Commands ── */}
      <SettingsSection title="Denied Commands">
        {/* Card A — Built-in denies */}
        <SettingsCard>
          <div className="flex items-center justify-between py-1.5">
            <div className="flex-1 min-w-0 mr-4">
              <div className="flex items-center gap-1.5">
                <span className="text-[13px] font-semibold text-text">Disable all built-in denies</span>
                {governanceLocked && <Lock size={13} className="text-muted" />}
              </div>
              <div className="text-[12px] text-muted mt-0.5 leading-relaxed">
                Turn off every built-in denied-command rule at once. Independent defense-in-depth controls (sensitive paths, IMDS, git-publish) stay enforced{governanceLocked ? ', and rules pinned by your organization’s policy remain on.' : '.'}
              </div>
            </div>
            {/* Disable-all stays available even when governance-locked: the
                backend keeps policy-pinned rules enforced under disable_all
                (compute_effective_denied), so a pin on one rule must not block
                opting every OTHER (unpinned) rule out. When locked, show the
                pinned-policy tooltip alongside the still-functional toggle. */}
            <span className="flex items-center gap-1.5 shrink-0">
              {governanceLocked && <InfoTip text={PINNED_TOOLTIP} />}
              <Toggle checked={disableAll} onChange={onDisableAllToggle} disabled={!dc} label="Disable all built-in denies" />
            </span>
          </div>

          <div className="text-[12px] text-muted mt-1 mb-2 leading-relaxed">
            Disabling a rule that overlaps an always-on control (sensitive-file reads, IMDS, git-publish) does not fully unblock it — defense-in-depth keeps it blocked.
          </div>

          {!dc ? (
            <div className="text-[12px] text-muted py-2">Loading built-in rules…</div>
          ) : (
            <>
              <div className="flex items-center justify-between mt-1 mb-0.5">
                <span className="text-[11px] text-muted">{Object.keys(grouped).length} categories · {dc.builtins.length} rules</span>
                <div className="flex items-center gap-3">
                  <button
                    type="button"
                    className="text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer p-0 transition-colors"
                    onClick={() => setExpandedCats(new Set(Object.keys(grouped)))}
                  >
                    Expand all
                  </button>
                  <button
                    type="button"
                    className="text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer p-0 transition-colors"
                    onClick={() => setExpandedCats(new Set())}
                  >
                    Collapse all
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
          <div className="text-[13px] font-semibold text-text">Your custom denies</div>
          <div className="text-[12px] text-muted mt-0.5 mb-1 leading-relaxed">
            Add your own deny patterns (Python-compatible regex). These are enforced at KiroCrew's PreToolUse gate alongside the built-in rules.
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

      {/* ── Confirm modal (disable a built-in rule / disable all) ── */}
      <Modal
        open={confirm !== null}
        onClose={() => setConfirm(null)}
        title={confirm?.kind === 'disable-all' ? 'Disable all built-in denies?' : 'Disable this denied command?'}
        maxWidth={480}
        footer={
          <>
            <Btn onClick={() => setConfirm(null)}>Cancel</Btn>
            <Btn danger disabled={!ack} onClick={runConfirm}>Disable</Btn>
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
          <span className="text-[13px] text-text">I understand this weakens KiroCrew's protection.</span>
        </label>
      </Modal>
    </>
  )
}
