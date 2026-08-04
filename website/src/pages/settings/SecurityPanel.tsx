import React, { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ShieldCheck, ShieldAlert, Lock, Eye, EyeOff, FileWarning, Terminal, Globe, Fingerprint, KeyRound, ScanLine, Layers, AlertTriangle, CheckCircle2, Circle, Clock, ExternalLink, ChevronRight, ChevronDown, Plus, Trash2, Gavel, Building2, Gauge, ToggleRight, MessageSquare, ListChecks, ArrowLeft, Boxes, BookOpen, Network, Copy, Check, Package } from 'lucide-react'
import { useAppSelector } from '../../store'
import { useContainerWidth } from '../../hooks/useContainerWidth'
import { Badge, Btn, Input, Toggle, Checkbox } from '../../components/ui'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import Modal from '../../components/Modal'
import InfoTip from '../../components/InfoTip'
import { api, ApiError, type DeniedCommandsData, type DeniedCommandRule, type DeniedUserRule, type GovernancePolicyData, type GovernanceScope, type GovernanceScopeDetail, type SecurityPostureData, type TailnetStatusData, type TrustedAppsData } from '../../api/client'
import { PostureDisclosureRow, CODE_BASE as POSTURE_CODE_BASE } from './PostureDisclosure'

import { i18nT } from '../../i18n/t'
import { fmtDateFields, fmtList, fmtTime, fmtTimeNumeric, toDate } from '../../i18n/format'
import ErrorNotice from '../../components/ErrorNotice'
/* ── Security feature registry ──
 *
 * Qualitative layer descriptions ONLY. Every control whose posture is a COUNT
 * (sensitive paths, denied commands, suspicious patterns, tool schemas,
 * redaction paths, credential families, exfil heuristics, audit surfaces, token
 * auth) is rendered from the live `GET /api/security/posture` registry instead —
 * see `PostureDisclosureRow`. This list must stay count-free: a hardcoded count
 * here silently goes stale, so if a description needs a number, the control
 * belongs in the posture registry.
 */

/**
 * Which defense-in-depth layer a feature belongs to, as a STABLE ID rather than the
 * badge's display string. The badge text is translated, but `layerColor` still has to
 * compare something language-independent — it used to `startsWith('Layer 0')`, which
 * silently loses its colour mapping the moment the label is localised.
 *
 * `'auth'` is not a numbered layer: it is the request-authentication surface that sits
 * across all of them, which is why it is a separate id and not `6`.
 */
type SecurityLayer = 0 | 1 | 2 | 3 | 4 | 5 | 'auth'

/** Stable id per feature row — also the React key, so it must not be the label. */
type SecurityFeatureKey =
  | 'os_sandbox' | 'sensitive_paths' | 'denied_commands' | 'suspicious_patterns'
  | 'mcp_validation' | 'credential_redaction' | 'url_exfil' | 'sel_audit'
  | 'token_auth' | 'csrf' | 'enterprise_grid' | 'observe_mode'

interface SecurityFeature {
  key: SecurityFeatureKey
  icon: React.ReactNode
  layer: SecurityLayer
}

/**
 * Catalog KEY for each layer row's name and one-line description.
 *
 * Keys, not copy: `FEATURES` is evaluated at module load, so an `i18nT()` call there
 * would freeze the boot language and never re-resolve on a language switch. The lookup
 * happens in `FeatureRow`, which runs per render.
 *
 * Shaped as flat `Record`s of full literal keys, indexed inline at the `i18nT()` call,
 * because that is the form `scripts/check-i18n-keys.mjs` can resolve statically — a key
 * it cannot resolve is a key it cannot verify exists. Same shape as `FILTER_LABEL_KEY`
 * in `pages/ChatSidebar.tsx` and `EFFORT_LABEL_KEY` in `lib/effort.ts`.
 */
export const FEATURE_LABEL_KEY: Record<SecurityFeatureKey, string> = {
  os_sandbox: 'pages.settings.securityPanel.feature_os_sandbox',
  sensitive_paths: 'pages.settings.securityPanel.feature_sensitive_paths',
  // Reuses the Denied Commands SECTION title one card down: same control, same name,
  // same panel. A second key would be a duplicate the translators pay for twice and
  // could answer differently.
  denied_commands: 'pages.settings.securityPanel.denied_commands',
  suspicious_patterns: 'pages.settings.securityPanel.feature_suspicious_patterns',
  mcp_validation: 'pages.settings.securityPanel.feature_mcp_validation',
  credential_redaction: 'pages.settings.securityPanel.feature_credential_redaction',
  url_exfil: 'pages.settings.securityPanel.feature_url_exfil',
  sel_audit: 'pages.settings.securityPanel.feature_sel_audit',
  token_auth: 'pages.settings.securityPanel.feature_token_auth',
  csrf: 'pages.settings.securityPanel.feature_csrf',
  enterprise_grid: 'pages.settings.securityPanel.feature_enterprise_grid',
  observe_mode: 'pages.settings.securityPanel.feature_observe_mode',
}
export const FEATURE_DESCRIPTION_KEY: Record<SecurityFeatureKey, string> = {
  os_sandbox: 'pages.settings.securityPanel.feature_os_sandbox_description',
  sensitive_paths: 'pages.settings.securityPanel.feature_sensitive_paths_description',
  denied_commands: 'pages.settings.securityPanel.feature_denied_commands_description',
  suspicious_patterns: 'pages.settings.securityPanel.feature_suspicious_patterns_description',
  mcp_validation: 'pages.settings.securityPanel.feature_mcp_validation_description',
  credential_redaction: 'pages.settings.securityPanel.feature_credential_redaction_description',
  url_exfil: 'pages.settings.securityPanel.feature_url_exfil_description',
  sel_audit: 'pages.settings.securityPanel.feature_sel_audit_description',
  token_auth: 'pages.settings.securityPanel.feature_token_auth_description',
  csrf: 'pages.settings.securityPanel.feature_csrf_description',
  enterprise_grid: 'pages.settings.securityPanel.feature_enterprise_grid_description',
  observe_mode: 'pages.settings.securityPanel.feature_observe_mode_description',
}

const FEATURES: SecurityFeature[] = [
  { key: 'os_sandbox', icon: <Lock size={14} />, layer: 0 },
  { key: 'sensitive_paths', icon: <FileWarning size={14} />, layer: 1 },
  { key: 'denied_commands', icon: <Terminal size={14} />, layer: 2 },
  { key: 'suspicious_patterns', icon: <AlertTriangle size={14} />, layer: 2 },
  { key: 'mcp_validation', icon: <ScanLine size={14} />, layer: 3 },
  { key: 'credential_redaction', icon: <KeyRound size={14} />, layer: 4 },
  { key: 'url_exfil', icon: <Globe size={14} />, layer: 4 },
  { key: 'sel_audit', icon: <Eye size={14} />, layer: 5 },
  { key: 'token_auth', icon: <Fingerprint size={14} />, layer: 'auth' },
  { key: 'csrf', icon: <ShieldCheck size={14} />, layer: 'auth' },
  { key: 'enterprise_grid', icon: <Layers size={14} />, layer: 'auth' },
  { key: 'observe_mode', icon: <EyeOff size={14} />, layer: 'auth' },
]

// Shared with PostureDisclosure so the repo URL lives in exactly one place.
const CODE_BASE = POSTURE_CODE_BASE

/** Tooltip on every control an enterprise policy pins. A catalog KEY, resolved at each
 *  of its three render sites for the reason above: at module scope `i18nT()` would
 *  resolve once at boot. */
const PINNED_TOOLTIP_KEY = 'pages.settings.securityPanel.pinned_by_policy'

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
function layerColor(layer: SecurityLayer): 'ok' | 'aim' | 'warn' {
  if (layer === 0 || layer === 1) return 'ok'
  if (layer === 'auth') return 'aim'
  return 'warn'
}

/** Badge text for a layer. Two keys rather than seven: the numbered layers differ only
 *  in the number, so `{{n}}` leaves one string per locale to translate and keeps the
 *  numbering itself out of the catalogs, where it could drift. */
function layerLabel(layer: SecurityLayer): string {
  return layer === 'auth'
    ? i18nT('pages.settings.securityPanel.layer_auth')
    : i18nT('pages.settings.securityPanel.layer_n', { n: layer })
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

/** A label:value micro-pill. Two-part on purpose: a bare "Added" pill states a
 *  value with no subject, and the three chips only mean something read against
 *  what they measure. Colour comes from theme variables via `Badge`, so the
 *  chips follow a custom palette instead of pinning a hex. */
function StatusChip({ label, value, variant }: { label: string; value: string; variant: 'ok' | 'warn' | 'muted' }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-[11px] text-muted">
      <span className="uppercase tracking-wider font-medium">{label}</span>
      <Badge variant={variant} className="text-[11px] px-1.5 py-0">{value}</Badge>
    </span>
  )
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
          <span className="text-[13px] font-semibold text-text group-hover:text-text-strong transition-colors">{i18nT(FEATURE_LABEL_KEY[feature.key])}</span>
          <Badge variant={layerColor(feature.layer)}>{layerLabel(feature.layer)}</Badge>
        </div>
        <div className="text-[12px] text-muted mt-0.5 leading-relaxed">{i18nT(FEATURE_DESCRIPTION_KEY[feature.key])}</div>
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
          aria-label={open ? i18nT('pages.settings.securityPanel.hide_pattern') : i18nT('pages.settings.securityPanel.show_pattern')}
          aria-expanded={open}
        >
          <Chevron size={14} />
        </button>
        <span className="flex-1 min-w-0 text-[13px] text-text">{rule.description}</span>
        {rule.pinned ? (
          <span className="flex items-center gap-1.5 shrink-0">
            <Lock size={13} className="text-muted" />
            <InfoTip text={i18nT(PINNED_TOOLTIP_KEY)} />
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
 *  Collapsed by default to keep the 137-rule panel scannable.
 *
 *  `rules` is what renders; `allRules` is the category as SHIPPED and is what the
 *  count badge, the pinned-lock hint and the all-off warning are computed from.
 *  They differ only while a search filter is active, and the distinction is
 *  load-bearing: reporting "2/2" for two search hits inside a 21-rule category
 *  would tell the reader the gate is 19 rules smaller than it is. */
function CategoryGroup({
  category,
  rules,
  allRules,
  open,
  onToggleOpen,
  disableAll,
  onRuleToggle,
  collapsible = true,
}: {
  category: string
  rules: DeniedCommandRule[]
  allRules?: DeniedCommandRule[]
  open: boolean
  onToggleOpen: () => void
  disableAll: boolean
  onRuleToggle: (rule: DeniedCommandRule, next: boolean) => void
  /** False while a search filter is active: matches are force-open, so a
   *  chevron would be a control that visibly does nothing. Render a plain
   *  header instead of an inert button. */
  collapsible?: boolean
}) {
  const Chevron = open ? ChevronDown : ChevronRight
  const counted = allRules ?? rules
  const enabled = counted.filter(r => r.enabled).length
  const pinned = counted.some(r => r.pinned)
  // "off" when every non-pinned rule in the group is disabled.
  const allOff = enabled === 0
  return (
    <div className="border-t border-border first:border-t-0">
      {collapsible ? (
        <button
          type="button"
          className="w-full flex items-center gap-2 py-2.5 bg-transparent border-none cursor-pointer text-left group"
          onClick={onToggleOpen}
          aria-expanded={open}
          aria-label={open
            ? i18nT('pages.settings.securityPanel.collapse_category_rules', { category: categoryLabel(category) })
            : i18nT('pages.settings.securityPanel.expand_category_rules', { category: categoryLabel(category) })}
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
          <Badge variant="muted" className="tabular-nums">{enabled}/{counted.length}</Badge>
        </button>
      ) : (
        <div className="w-full flex items-center gap-2 py-2.5 pl-[22px]">
          <span className="text-[11px] font-semibold uppercase tracking-[.04em] text-muted">
            {categoryLabel(category)}
          </span>
          {pinned && <Lock size={12} className="shrink-0 text-muted" />}
          <span className="flex-1" />
          {allOff && !pinned && (
            <span className="text-[11px] text-warn">{i18nT('pages.settings.securityPanel.off')}</span>
          )}
          <Badge variant="muted" className="tabular-nums">{enabled}/{counted.length}</Badge>
        </div>
      )}
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
        aria-label={i18nT('pages.settings.securityPanel.delete_pattern', { name: rule.pattern })}
      >
        <Trash2 size={14} />
      </button>
    </div>
  )
}

/** Add-a-custom-pattern input with client-side RegExp validation (Card B).
 *
 *  `value` is CONTROLLED from the panel shell rather than held here, because the
 *  rules section unmounts when the reader picks another rail section — local
 *  state would silently discard a half-typed deny pattern. `error` stays local:
 *  it is derived from the value and costs nothing to recompute. */
function AddDenyInput({ value, onChange, onAdd, busy }: { value: string; onChange: (next: string) => void; onAdd: (pattern: string) => void; busy: boolean }) {
  const [error, setError] = useState('')

  const submit = () => {
    const pattern = value.trim()
    if (!pattern) return
    try {
      new RegExp(pattern)
    } catch (e) {
      setError(e instanceof Error ? e.message : i18nT('pages.settings.securityPanel.invalid_regular_expression'))
      return
    }
    setError('')
    onAdd(pattern)
    onChange('')
  }

  return (
    <div className="pt-1.5">
      <div className="flex items-center gap-2">
        <Input
          value={value}
          onChange={e => { onChange(e.target.value); if (error) setError('') }}
          onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); submit() } }}
          placeholder={i18nT('pages.settings.securityPanel.add_a_custom_deny_pattern_regex_e_g_rm_rf_tmp_mi')}
          aria-label={i18nT('pages.settings.securityPanel.custom_deny_pattern')}
        />
        <Btn primary onClick={submit} disabled={busy || !value.trim()}>
          <Plus size={14} />
          {i18nT('pages.settings.securityPanel.add')}
        </Btn>
      </div>
      {/* Invalid-regex feedback on the input the user is still typing — a form
          hint, not a failure to diagnose, so no agent hand-off. */}
      <ErrorNotice message={error} className="mt-1.5" />
    </div>
  )
}

/* ── Governance Policy viewer (read-only effective ceiling) ── */

/**
 * Catalog KEY per governed scope name — `filesystem.read` → "Filesystem read",
 * `capabilities.cron` → "Cron".
 *
 * Keys, not copy, and module-level rather than rebuilt per call: the lookup runs in
 * `scopeLabel()`, which runs per render, so a language switch re-resolves. Every scope
 * the backend registers today has an entry here, which is the point — the title-case
 * fallback below fabricates ENGLISH from a raw scope id, so it renders the same word in
 * all ten locales. It is now reachable only by a scope a future release adds.
 */
export const SCOPE_LABEL_KEY: Record<string, string> = {
  tools: 'pages.settings.securityPanel.gov_scope_tools',
  mcp: 'pages.settings.securityPanel.gov_scope_mcp',
  apps: 'pages.settings.securityPanel.gov_scope_apps',
  commands: 'pages.settings.securityPanel.gov_scope_commands',
  channels: 'pages.settings.securityPanel.gov_scope_channels',
  'filesystem.read': 'pages.settings.securityPanel.gov_scope_filesystem_read',
  'filesystem.write': 'pages.settings.securityPanel.gov_scope_filesystem_write',
  'network.egress': 'pages.settings.securityPanel.gov_scope_network_egress',
  'sandbox.min_level': 'pages.settings.securityPanel.gov_scope_sandbox_level',
  approval_mode: 'pages.settings.securityPanel.gov_scope_approval_mode',
  'capabilities.cron': 'pages.settings.securityPanel.gov_scope_cron',
  'capabilities.spawn': 'pages.settings.securityPanel.gov_scope_spawn',
  'capabilities.messaging': 'pages.settings.securityPanel.gov_scope_messaging',
  'capabilities.memory_writes': 'pages.settings.securityPanel.gov_scope_memory_writes',
  'capabilities.script_hooks': 'pages.settings.securityPanel.gov_scope_script_hooks',
  'capabilities.theme_persona': 'pages.settings.securityPanel.gov_scope_theme_persona',
  'capabilities.theme_install': 'pages.settings.securityPanel.gov_scope_theme_install',
  // Named "Anonymous telemetry", not the leaf's bare "Telemetry": this scope
  // governs ONLY the outbound anonymous heartbeat. The unrelated
  // `telemetry.enabled` config field is local-only OTEL collection, and a row
  // reading just "Telemetry" would imply this ceiling governs that too.
  'capabilities.telemetry': 'pages.settings.securityPanel.gov_scope_telemetry',
}

/** Localised scope name. An unknown scope falls back to its title-cased leaf. */
function scopeLabel(scope: string): string {
  // `hasOwnProperty`, not a truthiness test on the lookup: `scope` arrives from
  // `GET /api/governance-policy`, so a scope named `toString` or `constructor` would
  // otherwise resolve to an inherited Object.prototype member and hand a FUNCTION to
  // i18next and then to JSX. Same hazard as `effortLabel` in `lib/effort.ts`.
  if (Object.prototype.hasOwnProperty.call(SCOPE_LABEL_KEY, scope)) return i18nT(SCOPE_LABEL_KEY[scope])
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
    return (d.allow_count ?? 0) === 0 ? i18nT('pages.settings.securityPanel.nothing_allowed') : i18nT('pages.settings.securityPanel.allow_list_rules', { rules: nRules(d.allow_count ?? 0) })
  }
  if (d.mode === 'deny') {
    return (d.deny_count ?? 0) === 0 ? i18nT('pages.settings.securityPanel.all_allowed') : i18nT('pages.settings.securityPanel.block_list_rules', { rules: nRules(d.deny_count ?? 0) })
  }
  return ''
}

/** Compact human label for a scope's EFFECTIVE state, by archetype. */
function effectiveLabel(row: GovernanceScope): string {
  if (!row.governed) return i18nT('pages.settings.securityPanel.not_restricted')
  const d = row.detail
  switch (row.archetype) {
    case 'ruleset':
      return rulesetLabel(d)
    case 'ordinal':
      return i18nT('pages.settings.securityPanel.floor', { n: d.floor ?? '?' })
    case 'capability': {
      // A host-profile pin is ONE surface's posture, so it must not read as
      // install-wide. The shipped host profile disables cron / messaging / spawn
      // because the host process performs none of them, while the cron and
      // messaging surfaces enable them under their own profiles — "Disabled by
      // policy" on those rows told operators a working feature was off.
      if (!d.enabled) {
        return row.scope_note === 'host_profile'
          ? i18nT('pages.settings.securityPanel.disabled_for_this_surface')
          : i18nT('pages.settings.securityPanel.disabled_by_policy')
      }
      const inner = Object.entries(d.inner ?? {})
      if (inner.length === 0) return i18nT('pages.settings.securityPanel.enabled')
      // Use rulesetLabel (not the allow-count alone) so a deny-mode inner ruleset
      // reads as a block-list, not a misleading "none".
      return i18nT('pages.settings.securityPanel.enabled_2', {
        detail: inner.map(([k, v]) => `${k}: ${rulesetLabel(v)}`).join('; '),
      })
    }
    case 'scopedmap': {
      const members = d.members ? rulesetLabel(d.members) : ''
      const postureN = Object.keys(d.posture ?? {}).length
      return postureN > 0 ? i18nT('pages.settings.securityPanel.posture_pinned', { members }) : members
    }
    default:
      return ''
  }
}

/** Plane grouping for the viewer — a clean split by governed surface. */
type GovPlaneKey = 'access' | 'io' | 'channels' | 'modes' | 'capabilities' | 'other'
interface GovPlane {
  key: GovPlaneKey
  icon: React.ReactNode
}
const GOV_PLANES: GovPlane[] = [
  { key: 'access', icon: <Terminal size={13} /> },
  { key: 'io', icon: <Globe size={13} /> },
  { key: 'channels', icon: <MessageSquare size={13} /> },
  { key: 'modes', icon: <Gauge size={13} /> },
  // Catch-all for every capabilities.* leaf — matched by prefix in `planeRows`,
  // which is why `SCOPE_PLANE` below lists none of them.
  { key: 'capabilities', icon: <ToggleRight size={13} /> },
  // Catch-all: any scope a future release (or the companion) registers that
  // matches none of the planes above and is not a capabilities.* leaf. Without
  // it, such a scope would be silently omitted, so the "all scopes" claim would
  // be false. Empty (hidden) on today's build.
  { key: 'other', icon: <ShieldCheck size={13} /> },
]

/** Catalog KEY per plane header — same resolvable shape as `FEATURE_LABEL_KEY`. */
export const GOV_PLANE_TITLE_KEY: Record<GovPlaneKey, string> = {
  access: 'pages.settings.securityPanel.gov_plane_access',
  io: 'pages.settings.securityPanel.gov_plane_io',
  channels: 'pages.settings.securityPanel.gov_plane_channels',
  modes: 'pages.settings.securityPanel.gov_plane_modes',
  capabilities: 'pages.settings.securityPanel.gov_plane_capabilities',
  other: 'pages.settings.securityPanel.gov_plane_other',
}

/**
 * Plane each explicitly-placed scope belongs to, in display order within the plane
 * (JS preserves string-key insertion order, and `planeRows` reads the keys in order).
 *
 * Written scope → plane, rather than a `scopes: string[]` on each plane, so every scope
 * id sits in property-NAME position. These ids are `GET /api/governance-policy` contract
 * values, never copy, and as bare array elements the i18n lint reports `'approval_mode'`
 * as an untranslated string — correctly, by its own shape rules, since it cannot know
 * the difference. This shape states the intent instead of suppressing the finding.
 *
 * `capabilities.*` is absent by design: that plane is matched by prefix below, so an
 * entry here would place the scope twice.
 */
const SCOPE_PLANE: Record<string, GovPlaneKey> = {
  tools: 'access',
  mcp: 'access',
  apps: 'access',
  commands: 'access',
  'filesystem.read': 'io',
  'filesystem.write': 'io',
  'network.egress': 'io',
  channels: 'channels',
  approval_mode: 'modes',
  'sandbox.min_level': 'modes',
}

/** Short badge naming WHERE a governed scope's ceiling comes from. Rendered for
 *  every governed row (not just the composed case) so the viewer's source-
 *  reporting is complete: policy-only, profile-only, or the intersection. */
function sourceBadgeLabel(source: GovernanceScope['source']): string {
  switch (source) {
    case 'policy+profile':
      return i18nT('pages.settings.securityPanel.policy_profile')
    case 'profile':
      return i18nT('pages.settings.securityPanel.profile_2')
    case 'policy':
      return i18nT('pages.settings.securityPanel.policy')
    default:
      return source
  }
}

/** A single read-only governance scope row. */
function GovernanceRow({ row }: { row: GovernanceScope }) {
  const label = effectiveLabel(row)
  // A host-profile row is one surface's ceiling, so its tooltip must say so
  // rather than the generic install-wide "pinned by policy".
  const tipKey =
    row.scope_note === 'host_profile'
      ? 'pages.settings.securityPanel.pinned_for_the_host_surface'
      : PINNED_TOOLTIP_KEY
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
            <InfoTip text={i18nT(tipKey)} />
          </>
        ) : (
          <span className="text-[12px] text-muted italic shrink-0">{i18nT('pages.settings.securityPanel.not_restricted')}</span>
        )}
      </div>
    </div>
  )
}

/** Read-only viewer: the effective governance ceiling across every scope. */
/* ── Ad-hoc auto-approve duration ── */

interface KirocrewCfgShape { agent?: { yolo_duration?: string; apps_allow_third_party?: unknown } }

const YOLO_DURATION_KEYS = ['30m', '1h', '6h', '12h', '24h', 'until_shutdown'] as const
type YoloDurationKey = (typeof YOLO_DURATION_KEYS)[number]

/** How long auto-approve lasts when it is turned on AD HOC — from the dashboard
 *  picker, Slack, or the API. All of those share this one value; the separate
 *  per-surface timers are gone. `until_shutdown` is disabled + lock-badged when
 *  an enterprise policy forbids it (status.yolo_until_shutdown_permitted ===
 *  false), the same ceiling the backend clamps at the source.
 *
 *  Deliberately does NOT expose the never-expiring DECLARED grant
 *  (agent.dangerouslySkipPermissions) — that stays config-file-only. */
function YoloDurationCard() {
  const qc = useQueryClient()
  const status = useAppSelector(s => s.dashboard.status)
  const untilShutdownPermitted = status?.yolo_until_shutdown_permitted ?? true
  const { data } = useQuery<KirocrewCfgShape>({ queryKey: ['kirocrewConfig'], queryFn: api.kirocrewConfig })
  const configured = data?.agent?.yolo_duration
  const current: YoloDurationKey =
    YOLO_DURATION_KEYS.find(k => k === configured) ?? '6h'
  const save = useMutation({
    mutationFn: (v: string) => api.patchConfig('agent.yolo_duration', v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
  })

  // Live "when does this end" line, so a no-expiry grant is never mistaken for
  // a bounded one.
  let activeNote: string | null = null
  if (status?.yolo) {
    if (status.yolo_until_shutdown) {
      activeNote = i18nT('pages.settings.securityPanel.yolo_active_until_restart')
    } else if (status.yolo_expires_at) {
      activeNote = i18nT('pages.settings.securityPanel.yolo_active_expires_at', {
        time: fmtTimeNumeric(status.yolo_expires_at),
      })
    }
  }

  function optionLabel(k: YoloDurationKey): string {
    switch (k) {
      case '30m': return i18nT('pages.settings.securityPanel.yolo_duration_30m')
      case '1h': return i18nT('pages.settings.securityPanel.yolo_duration_1h')
      case '6h': return i18nT('pages.settings.securityPanel.yolo_duration_6h')
      case '12h': return i18nT('pages.settings.securityPanel.yolo_duration_12h')
      case '24h': return i18nT('pages.settings.securityPanel.yolo_duration_24h')
      default: return i18nT('pages.settings.securityPanel.yolo_duration_until_shutdown')
    }
  }

  return (
    <SettingsCard>
      <div className="text-[13px] font-semibold text-text">{i18nT('pages.settings.securityPanel.yolo_duration_title')}</div>
      <div className="text-[12px] text-muted mt-0.5 mb-2 leading-relaxed">{i18nT('pages.settings.securityPanel.yolo_duration_desc')}</div>
      {activeNote && (
        <div className="text-[12px] text-accent mb-2 flex items-center gap-1">
          <Clock size={12} className="shrink-0" />{activeNote}
        </div>
      )}
      <div className="flex flex-col gap-1.5" role="radiogroup" aria-label={i18nT('pages.settings.securityPanel.yolo_duration_title')}>
        {YOLO_DURATION_KEYS.map(k => {
          const selected = current === k
          const disabled = k === 'until_shutdown' && !untilShutdownPermitted
          return (
            <button
              key={k}
              type="button"
              role="radio"
              aria-checked={selected}
              disabled={disabled || save.isPending}
              onClick={() => { if (!disabled && !selected) save.mutate(k) }}
              className={`flex items-center gap-2.5 text-left rounded-md border px-3 py-2 transition-colors bg-transparent cursor-pointer disabled:cursor-not-allowed disabled:opacity-60 ${selected ? 'border-accent bg-accent-subtle' : 'border-border hover:bg-bg-hover'}`}
            >
              <span className="shrink-0">
                {selected ? <CheckCircle2 size={14} className="text-accent" /> : <Circle size={14} className="text-muted" />}
              </span>
              <span className="text-[12px] text-text flex-1">{optionLabel(k)}</span>
              {disabled && (
                <span className="text-[11px] text-muted flex items-center gap-1">
                  <Lock size={11} className="shrink-0" />
                  {i18nT('pages.settings.securityPanel.yolo_duration_locked')}
                </span>
              )}
            </button>
          )
        })}
      </div>
      <div className="text-[11px] text-muted mt-2">{i18nT('pages.settings.securityPanel.yolo_duration_next_activation_note')}</div>
      {save.isError && (
        <div className="text-[12px] text-danger mt-1.5">{i18nT('pages.settings.securityPanel.failed_to_save_yolo_duration')}</div>
      )}
    </SettingsCard>
  )
}

/**
 * Catalog KEY per tailnet `state`, and the badge tone that goes with it.
 *
 * Two flat `Record`s of plain literals rather than one record of objects, so the
 * key-reference gate can still resolve `i18nT(TAILNET_STATE_KEY[state])`
 * statically — a nested `MAP[state].key` is two hops and falls through to the
 * unresolvable-site count. Module scope for the maps is fine because they hold
 * KEYS, not copy: a module-scope `i18nT()` would freeze the boot language.
 *
 * `pinned` reuses the panel's existing policy wording instead of a second
 * sentence about admin pins.
 */
const TAILNET_STATE_KEY: Record<TailnetStatusData['state'], string> = {
  active: 'pages.settings.securityPanel.tailnet_state_active',
  unresolved: 'pages.settings.securityPanel.tailnet_state_unresolved',
  off: 'pages.settings.securityPanel.tailnet_state_off',
  pinned: 'pages.settings.securityPanel.disabled_by_policy',
}

const TAILNET_STATE_VARIANT: Record<TailnetStatusData['state'], 'ok' | 'warn' | 'muted'> = {
  active: 'ok',
  unresolved: 'warn',
  off: 'muted',
  pinned: 'muted',
}

/* ── Tailnet origin section ─────────────────────────────────────────────────
 *
 * WHY THIS LIVES IN THE SECURITY PANEL, not in a Tailscale/network panel:
 *
 *  1. The setting IS an origin/Host allow-list control. Turning it on appends
 *     this machine's MagicDNS name to the same allowed-origins set that the CSRF
 *     Origin/Referer gate checks on every write and WebSocket upgrade — the
 *     "CSRF Protection" layer listed a few sections down. It is a security
 *     control that happens to be spelled as a Tailscale hostname, not a
 *     networking preference.
 *  2. It belongs beside the security-posture rows, which already report
 *     session-pin state — and the pin caveat below is precisely about that row
 *     stopping being enforceable behind `tailscale serve`.
 *  3. The governance pin for `capabilities.tailnet_origin` shows up in THIS
 *     panel's governance view with no extra wiring, because that view iterates
 *     `SCOPE_CATALOG`. Putting the control anywhere else would split the switch
 *     from the policy row that overrides it.
 *
 * The card renders off `state` and never recomputes it: the backend owns the
 * state machine (`pinned` > `off` > `unresolved` > `active`) so the two layers
 * cannot disagree about what "active" means.
 */
function TailnetOriginCard() {
  const qc = useQueryClient()
  const { data, isLoading, isError } = useQuery<TailnetStatusData>({
    queryKey: ['tailnet-status'],
    queryFn: api.tailnetStatus,
    // The reported host is the STARTUP resolution, so it cannot change while the
    // page is open. Only the config-backed `enabled`/pin can, and both of those
    // invalidate this key on write.
    staleTime: 300_000,
  })
  const save = useMutation({
    // Write path is the generic config PATCH, not a tailnet-specific route: the
    // switch persists `dashboard.tailscale.enabled`, and the status endpoint is
    // read-only because what it reports (the resolved name) is fixed at startup.
    mutationFn: (next: boolean) => api.patchConfig('dashboard.tailscale.enabled', next),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tailnet-status'] }),
  })
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!copied) return
    const id = window.setTimeout(() => setCopied(false), 1500)
    return () => window.clearTimeout(id)
  }, [copied])

  // A FAILED read is not "off" — the same rule ThirdPartyAppsCard follows. If the
  // read failed, the persisted setting may well be on, so collapsing it to off
  // would both hide the pin caveat while the origin is still trusted and make
  // the switch write `true` on click, leaving an active grant unrevokable here.
  if (isError || (!isLoading && data === undefined)) {
    return (
      <SettingsCard>
        <div className="flex items-center justify-between py-1.5">
          <span className="text-[13px] font-semibold text-text">{i18nT('pages.settings.securityPanel.tailnet_title')}</span>
          <span className="text-[12px] text-muted shrink-0">{i18nT('pages.settings.securityPanel.third_party_apps_state_unknown')}</span>
        </div>
        <div className="text-[12px] text-warn mt-1 flex items-start gap-1.5 leading-relaxed">
          <AlertTriangle size={13} className="shrink-0 mt-0.5" />
          <span>{i18nT('pages.settings.securityPanel.tailnet_unavailable')}</span>
        </div>
      </SettingsCard>
    )
  }

  const state = data?.state
  // Read off `state`, never off `enabled`: a governed install can carry
  // `enabled: true` in config while policy forces the capability off, and a
  // switch sitting at ON there would claim an origin that was never allowed.
  const effectiveOn = state === 'active' || state === 'unresolved'
  const pinned = state === 'pinned'

  return (
    <SettingsCard>
      <div className="flex items-start justify-between py-1.5 gap-4">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            <Network size={14} className="lucide-inline text-muted shrink-0" />
            <span className="text-[13px] font-semibold text-text">{i18nT('pages.settings.securityPanel.tailnet_title')}</span>
            <InfoTip text={i18nT('pages.settings.securityPanel.tailnet_tip')} />
            {state && <Badge variant={TAILNET_STATE_VARIANT[state]}>{i18nT(TAILNET_STATE_KEY[state])}</Badge>}
          </div>
          <div className="text-[12px] text-muted mt-1 leading-relaxed">
            {i18nT('pages.settings.securityPanel.tailnet_desc')}
          </div>
        </div>
        <span className="shrink-0 flex items-center gap-1.5">
          {pinned && <Lock size={13} className="lucide-inline text-muted" aria-hidden="true" />}
          <Toggle
            checked={effectiveOn}
            onChange={next => save.mutate(next)}
            disabled={isLoading || pinned || save.isPending}
            label={i18nT('pages.settings.securityPanel.tailnet_title')}
          />
        </span>
      </div>

      {/* Three status chips. Each is a FACT the endpoint reported, not a verdict:
          whether a name went into the allow-list, when that happened, and
          whether the per-device session pin can still bind. Rendered whenever
          the feature is on, including `unresolved` — the negative values are the
          whole point of that state. */}
      {effectiveOn && (
        <div className="flex flex-wrap items-center gap-1.5 mt-2">
          <StatusChip
            label={i18nT('pages.settings.securityPanel.tailnet_chip_allowlist')}
            value={state === 'active'
              ? i18nT('pages.settings.securityPanel.tailnet_chip_allowlist_added')
              : i18nT('pages.settings.securityPanel.tailnet_chip_allowlist_absent')}
            variant={state === 'active' ? 'ok' : 'warn'}
          />
          <StatusChip
            label={i18nT('pages.settings.securityPanel.tailnet_chip_resolved')}
            value={data && data.resolved_at > 0
              ? fmtTimeNumeric(data.resolved_at * 1000)
              : i18nT('pages.settings.securityPanel.tailnet_chip_resolved_never')}
            variant={data && data.resolved_at > 0 ? 'muted' : 'warn'}
          />
          {/* Constant by construction, not a read: no same-host tunnel can make
              the pin bind, so this chip states a property of the deployment
              shape rather than a value the server measured. */}
          <StatusChip
            label={i18nT('pages.settings.securityPanel.tailnet_chip_pin')}
            value={i18nT('pages.settings.securityPanel.tailnet_chip_pin_unbound')}
            variant="warn"
          />
        </div>
      )}

      {/* Copyable origin row. Present only in `active`, because that is the only
          state in which an origin string exists AND is trusted. */}
      {state === 'active' && data && (
        <div className="mt-2.5 rounded-md border border-border bg-bg-elevated px-3 py-2">
          <div className="text-[11px] text-muted uppercase tracking-wider font-medium">
            {i18nT('pages.settings.securityPanel.tailnet_origin_label')}
          </div>
          <div className="flex items-center gap-2 mt-1 flex-wrap">
            <code className="flex-1 min-w-0 truncate text-[13px] font-mono text-text-strong select-all" title={data.origin}>
              {data.origin}
            </code>
            <Btn
              // Acknowledge only on RESOLUTION. Setting "Copied" synchronously
              // claims a write that can still reject (clipboard permission, or
              // no `navigator.clipboard` at all outside a secure context), and a
              // false "Copied" is worse than no feedback: the user pastes stale
              // content believing this one is on the clipboard.
              onClick={() => {
                navigator.clipboard?.writeText(data.origin).then(() => setCopied(true), () => setCopied(false))
              }}
              aria-label={i18nT('pages.settings.securityPanel.tailnet_copy_origin')}
            >
              {copied ? <Check size={12} /> : <Copy size={12} />}
              {copied
                ? i18nT('pages.settings.securityPanel.tailnet_copied')
                : i18nT('pages.settings.securityPanel.tailnet_copy')}
            </Btn>
            {/* An anchor, not a Btn: opening a URL is navigation, so it must be
                middle-clickable and reachable by a screen reader as a link.
                Btn's own classes are reused so the pair still reads as one
                control group. */}
            <a
              href={data.origin}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border border-border bg-transparent text-[13px] text-muted no-underline font-body transition-all hover:text-text hover:border-border-strong hover:bg-bg-hover"
            >
              <ExternalLink size={12} />
              {i18nT('pages.settings.securityPanel.tailnet_open')}
            </a>
          </div>
        </div>
      )}

      {/* `unresolved`: on, but nothing was trusted. Says exactly that and no
          more — the endpoint deliberately ships no daemon-state field, so there
          is nothing here about whether tailscaled is running. */}
      {state === 'unresolved' && (
        <div className="text-[12px] text-warn mt-2 flex items-start gap-1.5 leading-relaxed">
          <AlertTriangle size={13} className="shrink-0 mt-0.5" />
          <span>{i18nT('pages.settings.securityPanel.tailnet_unresolved_note')}</span>
        </div>
      )}

      {/* The pin caveat is a real limitation of the shipped feature, not a
          footnote: behind `tailscale serve` every request arrives from
          127.0.0.1, so the per-device session pin has nothing to bind to and a
          dashboard link becomes a transferable bearer credential for the whole
          tailnet. Stated plainly, in the state where it actually applies. */}
      {state === 'active' && (
        <div className="text-[12px] text-warn mt-2 flex items-start gap-1.5 leading-relaxed">
          <ShieldAlert size={13} className="shrink-0 mt-0.5" />
          <span>
            {i18nT('pages.settings.securityPanel.tailnet_pin_caveat')}{' '}
            <a href={`${CODE_BASE}/issues/1762`} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">
              {i18nT('pages.settings.securityPanel.tailnet_pin_caveat_link')}
            </a>
          </span>
        </div>
      )}

      {pinned && (
        <p className="text-[12px] text-muted mt-2 leading-relaxed">
          {i18nT('privacyDisclosure.governanceOverrideNote')}
        </p>
      )}

      {!pinned && (
        <div className="text-[11px] text-muted mt-2 leading-relaxed">
          {i18nT('pages.settings.securityPanel.tailnet_restart_note')}
        </div>
      )}

      {save.isError && (
        <div className="text-[12px] text-danger mt-1.5">{i18nT('pages.settings.securityPanel.third_party_apps_save_failed')}</div>
      )}
    </SettingsCard>
  )
}

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
    const explicit = new Set(Object.keys(SCOPE_PLANE))
    const all = data?.scopes ?? []
    return GOV_PLANES.map(plane => {
      let rows: GovernanceScope[]
      if (plane.key === 'capabilities') {
        rows = all.filter(s => s.scope.startsWith('capabilities.'))
      } else if (plane.key === 'other') {
        rows = all.filter(s => !explicit.has(s.scope) && !s.scope.startsWith('capabilities.'))
      } else {
        rows = Object.keys(SCOPE_PLANE)
          .filter(sc => SCOPE_PLANE[sc] === plane.key)
          .map(sc => byScope.get(sc))
          .filter((s): s is GovernanceScope => !!s)
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
                  <span className="text-[11px] font-semibold uppercase tracking-[.04em] text-muted">{i18nT(GOV_PLANE_TITLE_KEY[plane.key])}</span>
                </div>
                <div className="divide-y divide-border">
                  {rows.map(row => <GovernanceRow key={row.scope} row={row} />)}
                </div>
              </div>
            ))}
            {/* Names the surfaces that carry their OWN ceiling, so a host row
                reading "Disabled for this surface" is legible: the capability is
                not off everywhere, it is off for the host process. */}
            {(data?.other_bound_surfaces?.length ?? 0) > 0 && (
              <div className="text-[11px] text-muted leading-relaxed border-t border-border pt-2 mt-2">
                {i18nT('pages.settings.securityPanel.other_surfaces_have_their_own_profiles', {
                  // fmtList, not join(', '): this string ships in 10 locales and
                  // zh joins with 、 and no spaces, so a literal separator would
                  // render wrong there.
                  surfaces: fmtList(data?.other_bound_surfaces ?? []),
                })}
              </div>
            )}
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

/* ── Third-party trust confirm target ── */
type TrustConfirmTarget =
  // Turning the BLANKET third-party-app trust flag on. Same acknowledgement gate
  // as a deny opt-out: both widen what un-reviewed code is allowed to do.
  | { kind: 'trust-all' }
  // Revoking one app's grant. Confirmed, unlike the other revoke-ish controls in
  // this panel, because it is the one that STOPS something the user is using: the
  // app is disabled and quits working. The consequence has to be on screen BEFORE
  // the click, not reported after it.
  | { kind: 'revoke-app'; name: string }

/**
 * The operator-facing reason a trust change failed, from an `ApiError`.
 *
 * The backend's prose is preferred over a generic "request failed" because the
 * two 409s here are not retry-and-hope conditions: `trust_setting_overlay_owned`
 * names the FILE and KEY the user has to edit (nothing the UI can do for them,
 * since `config.local.json` is user-owned and never written by Kiro Crew), and
 * `blanket_trust_sweep_incomplete` names the apps still executing after trust was
 * withdrawn. Collapsing either into "something went wrong" hides the only
 * actionable part.
 */
export function trustFailureMessage(err: unknown): string {
  if (err instanceof ApiError) {
    try {
      const parsed = JSON.parse(err.body) as { error?: unknown }
      if (typeof parsed.error === 'string' && parsed.error.trim()) return parsed.error
    } catch {
      // not JSON — fall through to the mapped message
    }
    return err.message
  }
  return err instanceof Error && err.message
    ? err.message
    : i18nT('pages.settings.securityPanel.trustedApps.unknown_error')
}

/* ── Live Security Posture section ── */

/** The two single-valued modes plus the live posture registry.
 *
 *  Reads `denied-commands` on the SAME query key the rules section uses, so the
 *  two share one cache entry and one request rather than racing: the deny gate's
 *  pill has to show the EFFECTIVE count (after opt-outs), which only that
 *  endpoint knows. */
function PostureSection() {
  const status = useAppSelector(s => s.dashboard.status)
  const yolo = status?.yolo ?? false
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
  // Enabled BUILT-INS only. `dc.effective_count` is builtins + user_added, which
  // is the right number for "rules enforced overall" but wrong for the posture
  // row, whose denominator is the built-in table: one custom deny made it read
  // "138 of 137 built-in rules".
  const enabledBuiltins = (dc?.builtins ?? []).filter(r => r.enabled).length

  return (
    <SettingsSection title={i18nT('pages.settings.securityPanel.live_security_posture')}>
      <SettingsCard>
        {/* Non-expandable rows: single-valued modes, not counted sets. */}
        <StatusRow icon={<Lock size={14} />} label={i18nT('pages.settings.securityPanel.process_sandbox')} value={i18nT('pages.settings.securityPanel.standard')} variant="ok"
          href={`${CODE_BASE}/src/kiro_crew/sandbox.py`} />
        <StatusRow
          icon={yolo ? <ShieldAlert size={14} /> : <ShieldCheck size={14} />}
          label={i18nT('pages.settings.securityPanel.tool_approval')}
          value={yolo ? i18nT('pages.settings.securityPanel.yolo_auto_approve') : i18nT('pages.settings.securityPanel.interactive')}
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
                // built-in-only denominator. Custom rules have their own card in
                // the Denied Commands section.
                countOverride={control.key !== 'denied_commands'
                  ? undefined
                  : dc ? enabledBuiltins : dcError ? null : undefined}
                // The custom-pattern sentence carries no count on purpose: a count
                // here would need per-locale plural forms to say "1 pattern" vs
                // "2 patterns" (the previous raw-English version read "1 custom
                // pattern are"), and the number is already on the rail and in the
                // Denied Commands pane. This sentence's job is to explain the
                // DENOMINATOR, not to enumerate.
                note={control.key === 'denied_commands' && dc
                  ? i18nT('pages.settings.securityPanel.built_in_rules_enforced_note', {
                    enabled: enabledBuiltins,
                    total: dc.builtins.length,
                  })
                    + (dc.user_added.length > 0
                      ? ' ' + i18nT('pages.settings.securityPanel.custom_patterns_counted_separately')
                      : '')
                  : undefined}
              />
            ))
          )}
        </div>
      </SettingsCard>
    </SettingsSection>
  )
}

/* ── Denied Commands section ────────────────────────────────────────────────
 *
 * Owns its own query, mutations and confirm modal so the rail can mount it on
 * demand: the built-in rule table is by far the panel's largest surface (137
 * rules across 10 categories) and there is no reason to build it while the
 * reader is looking at something else.
 */
function DeniedCommandsSection({ draft, onDraftChange }: { draft: string; onDraftChange: (next: string) => void }) {
  const qc = useQueryClient()
  const { data: dc } = useQuery<DeniedCommandsData>({ queryKey: ['denied-commands'], queryFn: api.deniedCommands })

  const [confirm, setConfirm] = useState<ConfirmTarget | null>(null)
  const [ack, setAck] = useState(false)
  // Category accordion state. Categories are collapsed by default — an id in
  // this set is EXPANDED. Keeps the 137-rule list scannable.
  const [expandedCats, setExpandedCats] = useState<Set<string>>(() => new Set())
  const [filter, setFilter] = useState('')

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

  const query = filter.trim().toLowerCase()
  const filtering = query.length > 0

  /** Categories reduced to their matching rules. A category whose NAME matches
   *  keeps all of its rules, so searching "credential" reads as a category jump
   *  rather than a partial list. */
  const visibleGroups = useMemo(() => {
    if (!query) return grouped
    const out: Record<string, DeniedCommandRule[]> = {}
    for (const [category, rules] of Object.entries(grouped)) {
      const hits = categoryLabel(category).toLowerCase().includes(query)
        ? rules
        : rules.filter(r =>
          r.description.toLowerCase().includes(query)
          || r.pattern.toLowerCase().includes(query))
      if (hits.length > 0) out[category] = hits
    }
    return out
  }, [grouped, query])

  const visibleUserRules = useMemo(() => {
    const rules = dc?.user_added ?? []
    if (!query) return rules
    return rules.filter(r => r.pattern.toLowerCase().includes(query))
  }, [dc, query])

  const matchedRules = Object.values(visibleGroups).reduce((n, rules) => n + rules.length, 0)
  const nothingMatches = filtering && matchedRules === 0 && visibleUserRules.length === 0

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
    ? i18nT('pages.settings.securityPanel.disabling_all_built_in_denies_removes_kirocrew_s')
    : i18nT('pages.settings.securityPanel.disabling_weakens_protection', { name: confirm.description })

  return (
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
              {governanceLocked
                ? i18nT('pages.settings.securityPanel.turn_off_every_rule_governance_locked')
                : i18nT('pages.settings.securityPanel.turn_off_every_rule')}
            </div>
          </div>
          {/* Disable-all stays available even when governance-locked: the
              backend keeps policy-pinned rules enforced under disable_all
              (compute_effective_denied), so a pin on one rule must not block
              opting every OTHER (unpinned) rule out. When locked, show the
              pinned-policy tooltip alongside the still-functional toggle. */}
          <span className="flex items-center gap-1.5 shrink-0">
            {governanceLocked && <InfoTip text={i18nT(PINNED_TOOLTIP_KEY)} />}
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
            <div className="mb-1.5">
              <Input
                value={filter}
                onChange={e => setFilter(e.target.value)}
                placeholder={i18nT('pages.settings.securityPanel.search_rules_placeholder')}
                aria-label={i18nT('pages.settings.securityPanel.search_rules_placeholder')}
              />
            </div>
            <div className="flex items-center justify-between mt-1 mb-0.5">
              {/* While filtering, report matched-of-total as a RATIO — but keep
                  every category badge on its full enabled/total, so a filter can
                  never make the gate read as smaller than it is. A ratio also
                  sidesteps count grammar, so this needs no plural forms. */}
              <span className="text-[11px] text-muted tabular-nums">
                {filtering
                  ? <>{matchedRules} / {dc.builtins.length} {i18nT('pages.settings.securityPanel.rules')}</>
                  : <>{Object.keys(grouped).length} {i18nT('pages.settings.securityPanel.categories')} {dc.builtins.length} {i18nT('pages.settings.securityPanel.rules')}</>}
              </span>
              {/* Hidden while filtering: matches render open regardless, so both
                  controls would record state the user cannot see take effect. */}
              {!filtering && (
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
              )}
            </div>
            <div>
              {Object.entries(visibleGroups).map(([category, rules]) => (
                <CategoryGroup
                  key={category}
                  category={category}
                  rules={rules}
                  // The badge's denominator is the SHIPPED category, never the
                  // filtered slice: "2/2" on a search hit inside a 21-rule
                  // category would misreport how much of that category is
                  // enforced.
                  allRules={grouped[category]}
                  // A filter that leaves its hits folded away is a filter that
                  // did nothing, so matches render open regardless of the
                  // accordion state the user left behind.
                  open={filtering || expandedCats.has(category)}
                  onToggleOpen={() => setExpandedCats(prev => {
                    const next = new Set(prev)
                    if (next.has(category)) next.delete(category)
                    else next.add(category)
                    return next
                  })}
                  disableAll={disableAll}
                  onRuleToggle={onBuiltinToggle}
                  collapsible={!filtering}
                />
              ))}
            </div>
            {nothingMatches && (
              <div className="text-[12px] text-muted py-2">
                {i18nT('pages.settings.securityPanel.no_rules_match', { query: filter.trim() })}
              </div>
            )}
          </>
        )}
      </SettingsCard>

      {/* Card B — Your custom denies */}
      <SettingsCard>
        <div className="text-[13px] font-semibold text-text">{i18nT('pages.settings.securityPanel.your_custom_denies')}</div>
        <div className="text-[12px] text-muted mt-0.5 mb-1 leading-relaxed">
          {i18nT('pages.settings.securityPanel.add_your_own_deny_patterns_python_compatible_reg')}
        </div>
        {visibleUserRules.length > 0 && (
          <div className="divide-y divide-border">
            {visibleUserRules.map(rule => (
              <CustomDenyRow
                key={rule.id}
                rule={rule}
                onToggle={next => toggleUser.mutate({ id: rule.id, enabled: next })}
                onDelete={() => deleteUser.mutate(rule.id)}
              />
            ))}
          </div>
        )}
        {/* Say so when the filter is what emptied this card, rather than letting
            it read as "you have no custom patterns". */}
        {filtering && visibleUserRules.length === 0 && (dc?.user_added.length ?? 0) > 0 && (
          <div className="text-[12px] text-muted py-1.5">
            {i18nT('pages.settings.securityPanel.custom_patterns_hidden_by_filter')}
          </div>
        )}
        <AddDenyInput value={draft} onChange={onDraftChange} onAdd={pattern => addUser.mutate(pattern)} busy={addUser.isPending} />
      </SettingsCard>

      {/* ── Confirm modal (disable a built-in rule / disable all) ── */}
      <Modal
        open={confirm !== null}
        onClose={() => setConfirm(null)}
        title={confirm?.kind === 'disable-all'
          ? i18nT('pages.settings.securityPanel.disable_all_built_in_denies_2')
          : i18nT('pages.settings.securityPanel.disable_this_denied_command')}
        maxWidth={480}
        footer={
          <>
            <Btn onClick={() => setConfirm(null)}>{i18nT('pages.settings.securityPanel.cancel')}</Btn>
            <Btn danger disabled={!ack} onClick={runConfirm}>
              {i18nT('pages.settings.securityPanel.disable')}
            </Btn>
          </>
        }
      >
        <div className="flex items-start gap-3">
          <AlertTriangle size={18} className="text-warn shrink-0 mt-0.5" />
          <div className="text-[13px] text-text leading-relaxed">{confirmBody}</div>
        </div>
        {/* eslint-disable-next-line jsx-a11y/label-has-for -- the Checkbox control is nested inside the label */}
        <label className="flex items-center gap-2.5 mt-4 cursor-pointer">
          <Checkbox checked={ack} onChange={e => setAck(e.target.checked)} />
          <span className="text-[13px] text-text">{i18nT('pages.settings.securityPanel.i_understand_this_weakens_kirocrew_s_protection')}</span>
        </label>
      </Modal>
    </SettingsSection>
  )
}

/* ── Third-party app execution ── */

/** Per-app trust grants for third-party app code (`agent.apps_trusted`), plus the
 *  blanket `agent.apps_allow_third_party` escape hatch.
 *
 *  Third-party app code does not run unless it is trusted. Two levers: the
 *  per-app grants listed here, and the blanket allow-all flag. The allow-all row
 *  is a `SettingsToggle` rather than a hand-rolled row so it carries
 *  `data-setting-label` (the deep-link target the App Store links to) and is
 *  picked up by scripts/gen-settings-registry.mjs. */
function ThirdPartyAppsCard() {
  const qc = useQueryClient()
  // Separate query from denied-commands: a different endpoint, a different
  // snapshot, and it must keep rendering when either of the two fails.
  const { data: taRaw, isError: taError } = useQuery<TrustedAppsData>({
    queryKey: ['trusted-apps'],
    queryFn: api.listTrustedApps,
  })
  // Normalize at the fetch boundary. `ineffective` is newer than `apps`, so a
  // newer dashboard talking to an older gateway (or any response shape that drops
  // a list) would otherwise reach `ta.ineffective.length` and throw — taking down
  // the WHOLE Security page, not just this card. The i18n render gate caught
  // exactly that on /settings?tab=security.
  const ta = useMemo(
    () =>
      taRaw
        ? {
          ...taRaw,
          apps: Array.isArray(taRaw.apps) ? taRaw.apps : [],
          ineffective: Array.isArray(taRaw.ineffective) ? taRaw.ineffective : [],
          allowAll: taRaw.allowAll === true,
        }
        : undefined,
    [taRaw],
  )
  // A FAILED read, not a slow one: no actionable control, and say so.
  const taUnavailable = taError === true

  const [confirm, setConfirm] = useState<TrustConfirmTarget | null>(null)
  const [ack, setAck] = useState(false)
  // Name of the app whose revoke ALSO disabled it, so the panel can say so.
  // Cleared on the next revoke, so the notice always refers to the last action.
  const [revokeDisabledApp, setRevokeDisabledApp] = useState<string | null>(null)
  // A trust mutation can FAIL in ways the operator has to see rather than infer
  // from a toggle springing back: the setting may be owned by config.local.json
  // (so writing config.json would change nothing), or the blanket-off sweep may
  // have left apps running. Both arrive as a 409 with a `code`, and both mean
  // "the thing you asked for did not happen" — silence here would reproduce, in
  // the UI, exactly the false-success the backend fixed.
  const [trustError, setTrustError] = useState<string | null>(null)

  // The acknowledgment checkbox resets whenever the modal opens or closes.
  useEffect(() => { setAck(false) }, [confirm])

  const applyTrustSnapshot = (snap: TrustedAppsData) => {
    // Field-by-field rather than the whole response: the revoke result carries an
    // extra `disabled` flag that belongs to that one action, not to the snapshot.
    qc.setQueryData(['trusted-apps'], {
      apps: snap.apps,
      ineffective: snap.ineffective,
      allowAll: snap.allowAll,
    })
    // Trust changes gate whether an app's code may run, so the App Store's own
    // enable/disable state can change underneath us — refetch it too.
    qc.invalidateQueries({ queryKey: ['apps'] })
  }
  const setTrustAll = useMutation({
    mutationFn: (value: boolean) => api.setTrustAllApps(value),
    onSuccess: snap => {
      setTrustError(null)
      applyTrustSnapshot(snap)
    },
    onError: (err: unknown) => setTrustError(trustFailureMessage(err)),
  })
  const untrust = useMutation({
    mutationFn: (name: string) => api.untrustApp(name),
    onSuccess: (snap, name) => {
      setTrustError(null)
      applyTrustSnapshot(snap)
      // Only surface the notice when the backend actually disabled something —
      // revoking trust on an already-disabled app is a silent no-op there.
      setRevokeDisabledApp(snap.disabled ? name : null)
    },
    onError: (err: unknown) => setTrustError(trustFailureMessage(err)),
  })

  // Granting blanket trust WEAKENS protection → acknowledgement gate. Revoking
  // it tightens, so it applies immediately (same asymmetry as the deny rules).
  const onTrustAllToggle = (next: boolean) => {
    if (next) setConfirm({ kind: 'trust-all' })
    else setTrustAll.mutate(false)
  }

  const runConfirm = () => {
    if (!confirm) return
    if (confirm.kind === 'trust-all') setTrustAll.mutate(true)
    else untrust.mutate(confirm.name)
    setConfirm(null)
  }

  const confirmBody = !confirm ? '' : confirm.kind === 'trust-all'
    ? i18nT('pages.settings.securityPanel.trustedApps.allow_all_confirm_body')
    : i18nT('pages.settings.securityPanel.trustedApps.revoke_confirm_body', { name: confirm.name })
  // Revoking one grant TIGHTENS security — it withdraws permission and stops the
  // app. It still needs a confirm (the app stops working, which the user must see
  // coming) but not an "I understand this weakens protection" acknowledgement:
  // demanding one for the safe direction trains people to tick the box without
  // reading, which is exactly what makes it worthless on the dangerous direction.
  const needsAck = confirm?.kind === 'trust-all'

  return (
    <>
      <SettingsCard>
        <div className="text-[12px] text-muted mb-1 leading-relaxed">
          {i18nT('pages.settings.securityPanel.trustedApps.description')}
        </div>

        {/* UNKNOWN is not OFF. If the snapshot has not resolved, the persisted
            flag may well be on, and rendering the switch at OFF would be wrong
            twice over: the blanket-trust warning below stays hidden while
            third-party code is still admitted, and a click would write `true`
            onto an ALREADY-true setting rather than revoking it. So on a failed
            read render no switch at all — `role="switch"` has no "unknown"
            (aria-checked `mixed` is checkbox-only), so any switch here would
            assert a state we could not read. A still-loading read keeps the
            disabled switch, since it resolves on its own. */}
        {taUnavailable ? (
          <div className="flex items-start gap-1.5 text-[12px] text-warn py-1.5 leading-relaxed">
            <AlertTriangle size={13} className="shrink-0 mt-0.5" />
            <span>{i18nT('pages.settings.securityPanel.third_party_apps_unavailable')}</span>
          </div>
        ) : (
          <SettingsToggle
            label={i18nT('pages.settings.securityPanel.trustedApps.allow_all_label')}
            description={i18nT('pages.settings.securityPanel.trustedApps.allow_all_description')}
            checked={ta?.allowAll === true}
            onChange={onTrustAllToggle}
            disabled={!ta}
          />
        )}

        {/* What switching it back off actually does. #1414 shipped this string
            saying an already-running app stays up; that stopped being true once
            the falling edge gained a teardown sweep, so the copy was corrected
            and is rendered here rather than left orphaned in the catalogs. */}
        <div className="text-[12px] text-muted py-1 leading-relaxed">
          {i18nT('pages.settings.securityPanel.third_party_apps_scope_note')}
        </div>

        {/* The blanket flag's real cost, in the words #1414 already shipped and
            translated: it trusts every third-party app including future ones. */}
        {ta?.allowAll === true && (
          <div className="flex items-start gap-1.5 text-[12px] text-warn py-1.5 leading-relaxed">
            <AlertTriangle size={13} className="shrink-0 mt-0.5" />
            <span>{i18nT('pages.settings.securityPanel.third_party_apps_on_warning')}</span>
          </div>
        )}

        {ta && (ta.apps.length === 0 ? (
          <div className="text-[12px] text-muted py-2 leading-relaxed border-t border-border mt-1 pt-2">
            {i18nT('pages.settings.securityPanel.trustedApps.empty')}
          </div>
        ) : (
          <div className="divide-y divide-border border-t border-border mt-1">
            {ta.apps.map(name => (
              <div key={name} data-testid={`trusted-app-${name}`} className="flex items-center gap-2.5 py-2">
                <Package size={14} className="lucide-inline shrink-0 text-muted" />
                <code className="flex-1 min-w-0 text-[12px] font-mono text-text break-all">{name}</code>
                <Badge variant="warn">{i18nT('pages.settings.securityPanel.trustedApps.trusted_badge')}</Badge>
                {/* The consequence is stated BEFORE the click (hint here, full
                    sentence in the confirm), because revoking stops an app the
                    user is actively using — learning that afterwards is the
                    failure a first-run reviewer flagged as a blocker. */}
                <span className="text-[11px] text-muted hidden sm:inline">
                  {i18nT('pages.settings.securityPanel.trustedApps.revoke_hint')}
                </span>
                <Btn
                  danger
                  disabled={untrust.isPending}
                  onClick={() => setConfirm({ kind: 'revoke-app', name })}
                >
                  {i18nT('pages.settings.securityPanel.trustedApps.revoke')}
                </Btn>
              </div>
            ))}
          </div>
        ))}

        {/* Stored-but-unenforced entries. `ineffective` is the set the gate
            IGNORES because the name fails the app-name charset (a hand-edited
            config.json can hold `LD-App`, a trailing space, a fullwidth
            homoglyph, `..`, `*`). Rendering them inside the list above claimed
            trust that does not exist, and left the user with no explanation for
            why their app was still blocked. Revoke is offered here too — the
            endpoint deliberately does NOT validate the name being removed, so
            junk that can never be granted can still be cleared out.

            Explicit color-mix rather than a `bg-card/88` opacity modifier:
            theme colors are raw `var(--x)` with no <alpha-value>, so a Tailwind
            opacity suffix silently generates nothing. */}
        {ta && ta.ineffective.length > 0 && (
          <div
            data-testid="trusted-apps-ineffective"
            className="mt-2 rounded-md border border-border bg-[color-mix(in_srgb,var(--card)_88%,transparent)] px-3 py-2.5"
          >
            <div className="flex items-start gap-2.5">
              <AlertTriangle size={14} className="lucide-inline text-warn shrink-0 mt-0.5" />
              <div className="min-w-0">
                <div className="text-[12px] font-semibold text-text">
                  {i18nT('pages.settings.securityPanel.trustedApps.ineffective_label')}
                </div>
                <div className="text-[12px] text-muted leading-relaxed">
                  {i18nT('pages.settings.securityPanel.trustedApps.ineffective_description')}
                </div>
              </div>
            </div>
            <div className="divide-y divide-border border-t border-border mt-2">
              {ta.ineffective.map(name => (
                <div key={name} data-testid={`ineffective-app-${name}`} className="flex items-center gap-2.5 py-2">
                  <Package size={14} className="lucide-inline shrink-0 text-muted" />
                  <code className="flex-1 min-w-0 text-[12px] font-mono text-muted line-through break-all">{name}</code>
                  <Btn danger disabled={untrust.isPending} onClick={() => untrust.mutate(name)}>
                    {i18nT('pages.settings.securityPanel.trustedApps.revoke')}
                  </Btn>
                </div>
              ))}
            </div>
          </div>
        )}

        {trustError && (
          <div className="flex items-start gap-2.5 mt-2 rounded-md bg-bg-elevated border border-danger px-3 py-2">
            <AlertTriangle size={14} className="lucide-inline text-danger shrink-0 mt-0.5" />
            <span className="text-[12px] text-text leading-relaxed">
              {i18nT('pages.settings.securityPanel.trustedApps.change_failed', { detail: trustError })}
            </span>
          </div>
        )}
        {revokeDisabledApp && (
          <div className="flex items-start gap-2.5 mt-2 rounded-md bg-bg-elevated border border-border px-3 py-2">
            <AlertTriangle size={14} className="lucide-inline text-warn shrink-0 mt-0.5" />
            <span className="text-[12px] text-muted leading-relaxed">
              {/* `name` is passed whether or not the catalog string interpolates
                  it — an unused variable is ignored, a missing one would render
                  the raw `{{name}}` placeholder to the user. */}
              {i18nT('pages.settings.securityPanel.trustedApps.revoke_disables', { name: revokeDisabledApp })}
            </span>
          </div>
        )}
      </SettingsCard>

      {/* ── Confirm modal (trust every app / revoke one grant) ── */}
      <Modal
        open={confirm !== null}
        onClose={() => setConfirm(null)}
        title={confirm?.kind === 'trust-all'
          ? i18nT('pages.settings.securityPanel.trustedApps.allow_all_label')
          : confirm
            ? i18nT('pages.settings.securityPanel.trustedApps.revoke_confirm_title', { name: confirm.name })
            : ''}
        maxWidth={480}
        footer={
          <>
            <Btn onClick={() => setConfirm(null)}>{i18nT('pages.settings.securityPanel.cancel')}</Btn>
            {/* "Disable" is the wrong verb for granting blanket trust. Reuses the
                existing one-word `trustDropdown.trust` string rather than minting an
                eleventh catalog key for a word already translated in all locales. */}
            <Btn danger disabled={needsAck && !ack} onClick={runConfirm}>
              {confirm?.kind === 'trust-all'
                ? i18nT('components.trustDropdown.trust')
                : i18nT('pages.settings.securityPanel.trustedApps.revoke_confirm_ok')}
            </Btn>
          </>
        }
      >
        <div className="flex items-start gap-3">
          <AlertTriangle size={18} className="text-warn shrink-0 mt-0.5" />
          <div className="text-[13px] text-text leading-relaxed">{confirmBody}</div>
        </div>
        {/* eslint-disable-next-line jsx-a11y/label-has-for -- the Checkbox control is nested inside the label */}
        {needsAck && (
          <label className="flex items-center gap-2.5 mt-4 cursor-pointer">
            <Checkbox checked={ack} onChange={e => setAck(e.target.checked)} />
            <span className="text-[13px] text-text">{i18nT('pages.settings.securityPanel.trustedApps.allow_all_confirm_ack')}</span>
          </label>
        )}
      </Modal>
    </>
  )
}

/* ── Defense-in-depth section ── */
function LayersSection() {
  return (
    <SettingsSection title={i18nT('pages.settings.securityPanel.defense_in_depth_architecture')}>
      <SettingsCard>
        <div className="text-[12px] text-muted mb-3 leading-relaxed">
          {i18nT('pages.settings.securityPanel.kirocrew_implements_6_security_layers_each_layer')}
        </div>
        <div className="divide-y divide-border">
          {FEATURES.map(f => <FeatureRow key={f.key} feature={f} />)}
        </div>
      </SettingsCard>
    </SettingsSection>
  )
}

/* ── Documentation section ── */
function DocsSection() {
  return (
    <SettingsSection title={i18nT('pages.settings.securityPanel.documentation')}>
      <SettingsCard>
        <div className="flex flex-col gap-2">
          {[
            { label: i18nT('pages.settings.securityPanel.security_deep_dive'), href: `${CODE_BASE}/docs/architecture/security-deep-dive.md` },
            { label: i18nT('pages.settings.securityPanel.security_module_spec'), href: `${CODE_BASE}/docs/system-specs/modules/security.md` },
          ].map(link => (
            <a key={link.label} href={link.href} target="_blank" rel="noopener noreferrer" className="flex items-center gap-2 text-[13px] text-accent hover:underline py-1">
              <ExternalLink size={12} />
              {link.label}
            </a>
          ))}
        </div>
      </SettingsCard>
    </SettingsSection>
  )
}

/* ── Section registry ───────────────────────────────────────────────────────
 *
 * The panel is a list-detail inspector rather than one long scroll: it mixes
 * things the user can change (approval, rules, third-party apps) with things
 * that are enforced for them (the layers, the enterprise ceiling), and stacking
 * both in one column gave a knob and a read-only fact identical visual weight.
 * The rail states which is which before any row is read, and the two large
 * tables (137 rules, ~20 governed scopes) get a pane instead of a fold.
 */
type SecuritySectionKey = 'posture' | 'approval' | 'rules' | 'tailnet' | 'apps' | 'layers' | 'governance' | 'docs'
type SecuritySectionGroup = 'status' | 'yours' | 'enforced' | 'reference'

interface SecuritySectionDef {
  key: SecuritySectionKey
  icon: React.ReactNode
  group: SecuritySectionGroup
}

/**
 * Catalog KEY per rail label — reusing each section's EXISTING heading key, not
 * a new parallel set. The rail label and the pane's own `SettingsSection` title
 * are the same words by construction, so they cannot drift, and translators are
 * not asked to name the same section twice.
 *
 * Keys, not copy, and indexed inline at the `i18nT()` call for the reason given
 * on `FEATURE_LABEL_KEY`: a module-scope `i18nT()` would freeze the boot
 * language, and a key the i18n lint cannot resolve statically is a key it cannot
 * verify exists.
 */
export const SECTION_LABEL_KEY: Record<SecuritySectionKey, string> = {
  posture: 'pages.settings.securityPanel.live_security_posture',
  approval: 'pages.settings.securityPanel.yolo_auto_approve',
  rules: 'pages.settings.securityPanel.denied_commands',
  tailnet: 'pages.settings.securityPanel.tailnet_section',
  apps: 'pages.settings.securityPanel.third_party_apps_section',
  layers: 'pages.settings.securityPanel.defense_in_depth_architecture',
  governance: 'pages.settings.securityPanel.governance_policy',
  docs: 'pages.settings.securityPanel.documentation',
}

/** Catalog KEY per rail group header. */
export const SECTION_GROUP_KEY: Record<SecuritySectionGroup, string> = {
  status: 'pages.settings.securityPanel.section_group_status',
  yours: 'pages.settings.securityPanel.section_group_your_settings',
  enforced: 'pages.settings.securityPanel.section_group_enforced',
  reference: 'pages.settings.securityPanel.section_group_reference',
}

/** Display order. The group of each entry drives the rail's headers, so entries
 *  sharing a group must stay adjacent. */
const SECURITY_SECTIONS: readonly SecuritySectionDef[] = [
  { key: 'posture', icon: <ShieldCheck size={15} />, group: 'status' },
  { key: 'approval', icon: <Gauge size={15} />, group: 'yours' },
  { key: 'rules', icon: <Terminal size={15} />, group: 'yours' },
  { key: 'tailnet', icon: <Network size={15} />, group: 'yours' },
  { key: 'apps', icon: <Boxes size={15} />, group: 'yours' },
  { key: 'layers', icon: <Layers size={15} />, group: 'enforced' },
  { key: 'governance', icon: <Gavel size={15} />, group: 'enforced' },
  { key: 'docs', icon: <BookOpen size={15} />, group: 'reference' },
]

/** Below this container width the rail and the detail pane stack: the rail
 *  becomes the whole view and choosing a section replaces it (with a back
 *  link), the same responsive contract ChannelsPanel uses. */
const TWO_PANE_MIN_WIDTH = 760

/** One rail row. `summary` is a live, FACTUAL value (a count, an on/off) — never
 *  a verdict: a rail that renders its own "OK" is a security claim made by the
 *  navigation, and it would keep claiming it while the underlying read failed.
 *
 *  Two lines, with the summary UNDER the label rather than beside it. Side-by-side
 *  they compete for the same row: at any rail width that still fits the settings
 *  page, a badge next to the label truncated the longest names to
 *  "Denied Comman…" and "Defense-in-Dept…". Stacking is what lets the rail reuse
 *  each section's real heading instead of inventing shorter rail-only copy. */
/**
 * An auto-approve expiry sized for the RAIL: "11:40 AM" when it lands today,
 * "Sat, 11:40 AM" once it crosses a day boundary.
 *
 * Two deliberate differences from the card's `fmtTimeNumeric`, both driven by
 * the row being an 11px line that truncates:
 *
 * - Seconds are dropped. A grant that ends at 11:40:00 does not end more
 *   precisely than "11:40" for any decision a reader makes here, so the extra
 *   characters are noise competing with the label for a truncating line.
 * - The weekday is added when the expiry is NOT today, because the offered
 *   durations reach 24 hours. A bare "Until 10:00 AM" on a grant that ends
 *   tomorrow morning reads as a time that has already passed — on this row
 *   that means believing a live grant has expired, which is the one misread
 *   worth spending characters to prevent.
 */
function fmtRailExpiry(expiry: Date, now: Date = new Date()): string {
  const sameDay =
    expiry.getFullYear() === now.getFullYear() &&
    expiry.getMonth() === now.getMonth() &&
    expiry.getDate() === now.getDate()
  return sameDay
    ? fmtTime(expiry)
    : fmtDateFields(expiry, { weekday: 'short', hour: 'numeric', minute: '2-digit' })
}

function SectionRow({ section, active, summary, onSelect, twoPane }: {
  section: SecuritySectionDef
  active: boolean
  summary?: string
  onSelect: () => void
  twoPane: boolean
}) {
  const label = i18nT(SECTION_LABEL_KEY[section.key])
  return (
    <button
      type="button"
      role="option"
      aria-selected={active}
      onClick={onSelect}
      // The longest label still ellipsizes in the most verbose locales, so the
      // full string stays reachable on hover rather than being lost.
      title={label}
      className={`flex items-center gap-2.5 w-full px-2.5 py-2 rounded-md text-left cursor-pointer border-none transition-colors ${
        active ? 'bg-accent-subtle text-accent' : 'bg-transparent text-muted hover:text-text hover:bg-bg-hover'
      }`}
    >
      <span className={`w-4 h-4 shrink-0 flex items-center justify-center ${active ? 'text-accent' : 'text-muted'}`}>
        {section.icon}
      </span>
      <span className="flex-1 min-w-0">
        {/* Wraps to two lines rather than truncating. The rail is a fixed 248px
            and the longest label ("Defense-in-Depth Architecture") inflates to
            52 characters under the pseudolocale, which truncated at 1.36x —
            over the render gate's 1.35x budget, and a real problem in any
            verbose locale, not just en-XA. Widening the rail would only move
            the boundary; not truncating removes it. `title` stays for the
            pathological case. */}
        <span className="block text-[13px] font-medium line-clamp-2">{label}</span>
        {summary && (
          <span className="block text-[11px] text-muted tabular-nums truncate mt-px">{summary}</span>
        )}
      </span>
      {!twoPane && <ChevronRight size={14} className="text-muted shrink-0" />}
    </button>
  )
}

export function SecurityPanel() {
  const [params, setParams] = useSearchParams()
  const [containerRef, width] = useContainerWidth<HTMLDivElement>()
  // null width = first paint before measurement; assume wide to avoid flashing
  // the narrow layout on desktop.
  const twoPane = width === null || width >= TWO_PANE_MIN_WIDTH

  // Held HERE, not in the rules pane: picking another rail section unmounts that
  // pane, and a half-typed deny pattern living in its local state would be
  // silently discarded. The 137-row rule table still unmounts — only the draft
  // string is lifted, so the reason the rail mounts lazily is preserved.
  const [denyDraft, setDenyDraft] = useState('')

  const rawSection = params.get('section')
  const selectedKey = SECURITY_SECTIONS.some(s => s.key === rawSection)
    ? (rawSection as SecuritySectionKey)
    : null
  // Wide mode always shows a detail pane; default to the first section.
  const effectiveKey = selectedKey ?? (twoPane ? SECURITY_SECTIONS[0].key : null)

  const setSection = (key: SecuritySectionKey | null) => setParams(prev => {
    const next = new URLSearchParams(prev)
    if (key) next.set('section', key)
    else next.delete('section')
    return next
  }, { replace: true })

  // Canonicalize the wide-mode implicit selection into the URL, so shrinking
  // below the breakpoint does not silently drop the shown pane back to the bare
  // rail. Gated on a REAL measurement: the pre-measurement paint optimistically
  // renders wide, and writing `section=posture` before the ResizeObserver
  // reports would make a fresh narrow visit open a section instead of the rail.
  useEffect(() => {
    if (width !== null && twoPane && !selectedKey) setSection(SECURITY_SECTIONS[0].key)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [width, twoPane, selectedKey])

  // Rail summaries. Both reads are shared cache entries with the sections that
  // own them, so the rail adds no extra request.
  const status = useAppSelector(s => s.dashboard.status)
  const { data: dc } = useQuery<DeniedCommandsData>({ queryKey: ['denied-commands'], queryFn: api.deniedCommands })
  const { data: cfg, isError: cfgError } = useQuery<KirocrewCfgShape>({ queryKey: ['kirocrewConfig'], queryFn: api.kirocrewConfig })
  // Same key and staleTime the card uses, so the rail adds no second request.
  const { data: tailnet, isError: tailnetError } = useQuery<TailnetStatusData>({
    queryKey: ['tailnet-status'],
    queryFn: api.tailnetStatus,
    staleTime: 300_000,
  })

  const summaryFor = (key: SecuritySectionKey): string | undefined => {
    switch (key) {
      case 'approval':
        // An active grant outranks the configured duration: it is the state that
        // is currently weakening the install, so it is what the rail reports.
        //
        // It reports WHEN THE GRANT ENDS, not that it exists. Returning the
        // section's own label here made the row read "YOLO (auto-approve)" twice
        // — once as the label, once as a muted 11px echo underneath — spending
        // the rail's most important row on a duplicate instead of the one fact a
        // reader needs. The expiry is already in `status`; the card below shows
        // the same values in full sentences.
        if (status?.yolo) {
          if (status.yolo_until_shutdown) return i18nT('pages.settings.securityPanel.rail_until_restart')
          // Parse before formatting, and fall back to the bare "active" string
          // when the timestamp will not parse. `fmtRailExpiry` would otherwise
          // be handed an invalid Date and render an em-dash placeholder, which
          // would put "Until —" on a row asserting that a grant is live —
          // announcing a weakened install while withholding the one fact that
          // makes the claim actionable. The backend sends ISO-or-empty today,
          // so this is a guard against the field's shape changing.
          const expiry = toDate(status.yolo_expires_at)
          return expiry
            ? i18nT('pages.settings.securityPanel.rail_until_time', { time: fmtRailExpiry(expiry) })
            : i18nT('pages.settings.securityPanel.rail_active')
        }
        // `== null`, NOT `=== undefined`: `dashboard.status` is typed
        // `StatusData | null` and initialises to `null`, so an `undefined` check
        // never fires and the rail would claim the safe "Interactive" on every
        // fresh load — before any status payload has arrived, on an install where
        // auto-approve may well be active. Same rule the apps case follows, where
        // React Query genuinely yields `undefined`: an unread state is reported as
        // no summary, never as the reassuring one.
        return status == null ? undefined : i18nT('pages.settings.securityPanel.interactive')
      case 'rules':
        return dc ? String(dc.builtins.filter(r => r.enabled).length) : undefined
      case 'tailnet':
        // An unread state gets no summary, never the reassuring one — the same
        // rule the apps case follows. The label is the server-owned `state`, so
        // the rail cannot disagree with the card it navigates to.
        if (tailnetError || tailnet === undefined) return undefined
        return i18nT(TAILNET_STATE_KEY[tailnet.state])
      case 'apps':
        // An UNREADABLE value is not "off" — mirror the card's own handling and
        // render no summary rather than asserting a state we could not read.
        if (cfgError || cfg === undefined) return undefined
        // Names what the gate DOES rather than "On"/"Off": a bare "On" is a
        // connector word with nothing for a translator to work from, and the
        // verb reads better against a blanket admission control.
        return cfg.agent?.apps_allow_third_party === true
          ? i18nT('pages.settings.securityPanel.state_allowed')
          : i18nT('pages.settings.securityPanel.state_blocked')
      case 'layers':
        return String(FEATURES.length)
      default:
        return undefined
    }
  }

  // Grouped as listbox > group > option. The group headers used to be
  // `aria-hidden`, which handed screen-reader users seven flat options and threw
  // away the yours-vs-enforced split the rail exists to convey; `role="group"`
  // with the header as its accessible name is the ARIA-valid way to keep it,
  // since a listbox may contain groups but not arbitrary children.
  const groupedSections = SECURITY_SECTIONS.reduce<{ group: SecuritySectionGroup; items: SecuritySectionDef[] }[]>(
    (acc, section) => {
      const last = acc[acc.length - 1]
      if (last && last.group === section.group) last.items.push(section)
      else acc.push({ group: section.group, items: [section] })
      return acc
    },
    [],
  )

  const rail = (
    // No aria-label on the wrapper: the listbox inside already carries this name,
    // and naming both makes a screen reader announce it twice.
    <nav className={twoPane ? 'w-[248px] shrink-0' : 'w-full'}>
      <div className="flex flex-col gap-0.5" role="listbox" aria-label={i18nT('pages.settings.securityPanel.security_sections')}>
        {groupedSections.map(({ group, items }) => (
          <div key={group} role="group" aria-label={i18nT(SECTION_GROUP_KEY[group])}>
            <div className="text-[11px] text-muted uppercase tracking-wider font-medium px-2.5 pt-2.5 pb-1 select-none">
              {i18nT(SECTION_GROUP_KEY[group])}
            </div>
            {items.map(section => (
              <SectionRow
                key={section.key}
                section={section}
                active={twoPane && section.key === effectiveKey}
                summary={summaryFor(section.key)}
                onSelect={() => setSection(section.key)}
                twoPane={twoPane}
              />
            ))}
          </div>
        ))}
      </div>
    </nav>
  )

  return (
    <div ref={containerRef}>
      {/* ── Data Classification Warning ──
       *  Outside the rail on purpose. It is an instruction about what to type
       *  into the product, not a section of the security model, and a notice you
       *  can navigate away from is a notice most readers never see. */}
      <div className="mb-5 bg-bg-elevated border rounded-lg p-4 flex items-start gap-3 animate-rise" style={{ borderColor: 'color-mix(in srgb, var(--warn) 45%, transparent)' }}>
        <AlertTriangle size={18} className="text-warn shrink-0 mt-0.5" />
        <div>
          <div className="text-[13px] font-semibold text-text-strong">{i18nT('pages.settings.securityPanel.data_classification_notice')}</div>
          <div className="text-[12px] text-muted mt-1 leading-relaxed">
            {i18nT('pages.settings.securityPanel.do_not_enter_highly_sensitive_or_restricted_data')}
          </div>
        </div>
      </div>

      {/* Both responsive modes render the same child slots in the same order
          (rail?, back-link?, pane wrapper) so React reconciles the pane by
          position and a width transition never remounts it — remounting would
          discard an unsaved custom deny pattern mid-type. Only changing the
          selected section remounts, which is intended. */}
      <div className={twoPane ? 'flex gap-6 items-start' : 'flex flex-col'}>
        {(twoPane || !effectiveKey) && rail}
        {!twoPane && effectiveKey && (
          <button
            type="button"
            onClick={() => setSection(null)}
            className="flex items-center gap-1.5 self-start text-[13px] font-medium text-accent bg-transparent border-none cursor-pointer px-0 py-1 mb-2 hover:underline"
          >
            <ArrowLeft size={14} />
            {i18nT('pages.settings.securityPanel.security_sections')}
          </button>
        )}
        <div className={twoPane ? 'flex-1 min-w-0' : 'w-full'}>
          {effectiveKey === 'posture' && <PostureSection />}
          {effectiveKey === 'approval' && (
            <SettingsSection title={i18nT('pages.settings.securityPanel.yolo_auto_approve')}>
              <YoloDurationCard />
            </SettingsSection>
          )}
          {effectiveKey === 'rules' && <DeniedCommandsSection draft={denyDraft} onDraftChange={setDenyDraft} />}
          {effectiveKey === 'tailnet' && (
            <SettingsSection title={i18nT('pages.settings.securityPanel.tailnet_section')}>
              <TailnetOriginCard />
            </SettingsSection>
          )}
          {effectiveKey === 'apps' && (
            <SettingsSection title={i18nT('pages.settings.securityPanel.third_party_apps_section')}>
              <ThirdPartyAppsCard />
            </SettingsSection>
          )}
          {effectiveKey === 'layers' && <LayersSection />}
          {effectiveKey === 'governance' && <GovernancePolicyViewer />}
          {effectiveKey === 'docs' && <DocsSection />}
        </div>
      </div>
    </div>
  )
}
