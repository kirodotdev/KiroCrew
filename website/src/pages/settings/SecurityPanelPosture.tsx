/**
 * Settings -> Security -> Live security posture: the posture section, and the
 * status row and chip primitives it shares with the tailnet card. One of the
 * sections `SecurityPanel.tsx` composes.
 */
import React from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  ShieldCheck, ShieldAlert, Lock, Eye, FileWarning, Terminal, Globe, Fingerprint, KeyRound,
  ScanLine, AlertTriangle, ExternalLink,
} from 'lucide-react'
import { useAppSelector } from '../../store'
import { Badge } from '../../components/ui'
import { SettingsSection, SettingsCard } from '../../components/settings'
import { api, type DeniedCommandsData, type SecurityPostureData } from '../../api/client'
import { PostureDisclosureRow, CODE_BASE as POSTURE_CODE_BASE } from './PostureDisclosure'
import { MobileLoginCard } from './MobileLoginCard'
import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'

// Shared with PostureDisclosure so the repo URL lives in exactly one place.
export const CODE_BASE = POSTURE_CODE_BASE

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
export function StatusChip({ label, value, variant }: { label: string; value: string; variant: 'ok' | 'warn' | 'muted' }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-[11px] text-muted">
      <span className="uppercase tracking-wider font-medium">{label}</span>
      <Badge variant={variant} className="text-[11px] px-1.5 py-0">{value}</Badge>
    </span>
  )
}

/* ── Live Security Posture section ── */

/** The two single-valued modes plus the live posture registry.
 *
 *  Reads `denied-commands` on the SAME query key the rules section uses, so the
 *  two share one cache entry and one request rather than racing: the deny gate's
 *  pill has to show the EFFECTIVE count (after opt-outs), which only that
 *  endpoint knows. */
export function PostureSection() {
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
      <MobileLoginCard />
      {/* data-setting-label: deep-link anchor for the manual registry entry
        * (settingsManual.ts `security.live-security-posture`) — the section is
        * raw markup the extractor cannot see, so without this the highlight
        * would querySelector a label no element carries and silently no-op. */}
      <div data-setting-label={i18nT('pages.settings.securityPanel.live_security_posture')}>
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
            // Read failure on a read-only status list → hand-off on.
            <ErrorNotice
              className="my-1"
              message={i18nT('pages.settings.securityPanel.security_posture_detail_is_temporarily_unavailab')}
              askAgent
            />
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
      </div>
    </SettingsSection>
  )
}
