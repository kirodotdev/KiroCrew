/**
 * Settings -> Security -> Defense-in-depth architecture: the qualitative layer
 * registry, its catalog keys, and the section that lists it. The Security rail
 * reads `FEATURES` for its count, and `SecurityPanel.tsx` re-exports
 * `FEATURE_LABEL_KEY` and `FEATURE_DESCRIPTION_KEY`.
 */
import React from 'react'
import {
  ShieldCheck, Lock, Eye, EyeOff, FileWarning, Terminal, Globe, Fingerprint, KeyRound, ScanLine,
  Layers, AlertTriangle, CheckCircle2,
} from 'lucide-react'
import { Badge } from '../../components/ui'
import { SettingsSection, SettingsCard } from '../../components/settings'
import { i18nT } from '../../i18n/t'

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
 * in `pages/chat-sidebar/SearchDock.tsx` and `EFFORT_LABEL_KEY` in `lib/effort.ts`.
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

export const FEATURES: SecurityFeature[] = [
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

/* ── Defense-in-depth section ── */
export function LayersSection() {
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
