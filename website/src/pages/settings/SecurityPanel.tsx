/**
 * SecurityPanel: Settings -> Security, a list-detail inspector over the sections
 * in `SECURITY_SECTIONS`. This file holds the rail (the section registry, the rail
 * summaries, the panel) and the Documentation section. Each other section lives in
 * a sibling owner, and every name callers import from here is re-exported:
 * - `SecurityPanelPosture.tsx`: Live security posture
 * - `SecurityPanelApprovals.tsx`: Auto-approve, Flagged-file delivery
 * - `SecurityPanelDeniedCommands.tsx`: Denied commands
 * - `SecurityPanelTailnet.tsx`: Tailnet
 * - `SecurityPanelTrust.tsx`: Third-party apps, Registry trust
 * - `SecurityPanelRedaction.tsx`: Redaction
 * - `SecurityPanelLayers.tsx`: Defense-in-depth architecture
 * - `SecurityPanelGovernance.tsx`: Governance policy
 * Callers import from this file; an owner never imports it.
 */
import React, { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  ShieldCheck, EyeOff, FileWarning, Terminal, Layers, ExternalLink, Gavel, Gauge, Boxes, BookOpen,
  Network, GitBranch,
} from 'lucide-react'
import { useAppSelector } from '../../store'
import { SettingsSubNav } from '../../components/SettingsSubNav'
import { SettingsSection, SettingsCard } from '../../components/settings'
import { api, type DeniedCommandsData, type TailnetStatusData } from '../../api/client'

import { i18nT } from '../../i18n/t'
import { fmtDateFields, fmtTime, toDate } from '../../i18n/format'
import { CODE_BASE, PostureSection } from './SecurityPanelPosture'
import {
  type KirocrewCfgShape, YoloDurationCard, FileDeliveryConsentCard,
} from './SecurityPanelApprovals'
import { DeniedCommandsSection } from './SecurityPanelDeniedCommands'
import { TAILNET_STATE_KEY, TailnetOriginCard } from './SecurityPanelTailnet'
import { ThirdPartyAppsCard, TrustedRegistriesCard } from './SecurityPanelTrust'
import { RedactionSection } from './SecurityPanelRedaction'
import { FEATURES, LayersSection } from './SecurityPanelLayers'
import { GovernancePolicyViewer } from './SecurityPanelGovernance'
export { trustFailureMessage, registryTrustFailureMessage } from './SecurityPanelTrust'
export { RedactionAllowedHostsCard, RedactionSection } from './SecurityPanelRedaction'
export { FEATURE_LABEL_KEY, FEATURE_DESCRIPTION_KEY } from './SecurityPanelLayers'
export { SCOPE_LABEL_KEY, humaniseScopeLeaf, GOV_PLANE_TITLE_KEY } from './SecurityPanelGovernance'

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
type SecuritySectionKey = 'posture' | 'approval' | 'rules' | 'tailnet' | 'apps' | 'registries' | 'redaction' | 'delivery' | 'layers' | 'governance' | 'docs'
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
 * The one deliberate exception is `registries`: the rail is an 11px line that
 * wraps on the longest names, so it keeps the short `…section_title_rail`
 * ("Registry trust"), while the pane's `SettingsSection` uses the descriptive
 * `…section_title` ("Registries trusted with your Git credentials") — a
 * first-time reader has to be told WHOSE credentials are at stake, and that
 * sentence does not fit the rail. The two keys live under the same
 * `trustedRegistries` block so a translator still names the section once.
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
  registries: 'pages.settings.securityPanel.trustedRegistries.section_title_rail',
  redaction: 'pages.settings.securityPanel.redaction_section',
  delivery: 'pages.settings.securityPanel.file_delivery_section',
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
  { key: 'registries', icon: <GitBranch size={15} />, group: 'yours' },
  { key: 'redaction', icon: <EyeOff size={15} />, group: 'yours' },
  { key: 'delivery', icon: <FileWarning size={15} />, group: 'yours' },
  { key: 'layers', icon: <Layers size={15} />, group: 'enforced' },
  { key: 'governance', icon: <Gavel size={15} />, group: 'enforced' },
  { key: 'docs', icon: <BookOpen size={15} />, group: 'reference' },
]

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

/** Security tab. `basePath` opts the section sub-nav into path navigation
 *  (`${basePath}/security/<section>`); omitted, the historical ?sub= /
 *  ?section= query behavior is unchanged. Passed by the Settings host. */
export function SecurityPanel({ basePath }: { basePath?: string } = {}) {
  // Held HERE, not in the rules pane: picking another rail section unmounts that
  // pane, and a half-typed deny pattern living in its local state would be
  // silently discarded. The 137-row rule table still unmounts — only the draft
  // string is lifted, so the reason the rail mounts lazily is preserved.
  const [denyDraft, setDenyDraft] = useState('')
  // Lifted for the same reason as denyDraft: the rules section unmounts on rail
  // navigation, which would discard a half-typed note.
  const [denyNoteDraft, setDenyNoteDraft] = useState('')

  // Rail summaries. Both reads are shared cache entries with the sections that
  // own them, so the rail adds no extra request.
  // Deliberately NOT an error surface: on a failed read the summaries below
  // return nothing (a blank rail line, never a fake value), and the failure
  // itself is reported through ErrorNotice by the card that owns each read —
  // YoloDurationCard for `cfgError`, TailnetOriginCard for `tailnetError`,
  // DeniedCommandsSection for the denied-commands list. A second notice in a
  // 192px rail would repeat the card's message without room to act on it.
  const status = useAppSelector(s => s.dashboard.status)
  const { data: dc } = useQuery<DeniedCommandsData>({ queryKey: ['denied-commands'], queryFn: api.deniedCommands })
  const { data: cfg, isError: cfgError } = useQuery<KirocrewCfgShape>({ queryKey: ['kirocrewConfig'], queryFn: api.kirocrewConfig })
  // Same key and staleTime the card uses, so the rail adds no second request.
  const { data: tailnet, isError: tailnetError } = useQuery<TailnetStatusData>({
    queryKey: ['tailnet-status'],
    queryFn: api.tailnetStatus,
    staleTime: 300_000,
  })

  // NO rail summary for the flagged-file-delivery row, deliberately, and this is a
  // gate decision rather than a design preference. `SettingsSubNav` renders a
  // label and a summary as two adjacent catalog keys, which the render-time i18n
  // gate counts as one `fragment/multi-unit` finding per site
  // (SettingsSubNav.tsx:241 and :264). The YOLO and third-party-apps rows already
  // carry exactly that finding on this surface, and `[vs-base]` fails on ANY
  // per-surface increase against a goal of zero -- so giving this row a summary
  // adds four findings to a surface that is trying to reach none. The state is one
  // click away in the card, which shows it prominently. The real remedy is the one
  // the gate prints ("merge the adjacent catalog keys into one key with {{vars}}"),
  // and it belongs to `SettingsSubNav` for every panel at once rather than to this
  // row: doing it here alone would also break SECTION_LABEL_KEY's rule that a rail
  // label REUSES its section's heading key.
  //
  // The credential-redaction row follows the same decision, for the same reason:
  // a summary there was measured at +4 `fragment/multi-unit` findings on this
  // surface and failed `[vs-base]`. A rail-level read would also hit the
  // owner-gated GET for every allow-listed non-owner who merely opens Security,
  // writing an audited refusal per mount. The OFF state is one click away, where
  // the card shows it with a warning and the time it was turned off.

  const summaryFor = (key: SecuritySectionKey): string | undefined => {
    switch (key) {
      case 'approval':
        // An active grant outranks the configured duration: it is the state that
        // is currently weakening the install, so it is what the rail reports —
        // WHEN THE GRANT ENDS, not that it exists.
        if (status?.yolo) {
          if (status.yolo_until_shutdown) return i18nT('pages.settings.securityPanel.rail_until_restart')
          // Parse before formatting, and fall back to the bare "active" string
          // when the timestamp will not parse: "Until —" on a row asserting a
          // live grant would announce a weakened install while withholding the
          // one fact that makes the claim actionable.
          const expiry = toDate(status.yolo_expires_at)
          return expiry
            ? i18nT('pages.settings.securityPanel.rail_until_time', { time: fmtRailExpiry(expiry) })
            : i18nT('pages.settings.securityPanel.rail_active')
        }
        // `== null`, NOT `=== undefined`: `dashboard.status` is typed
        // `StatusData | null` and initialises to `null`. An unread state is
        // reported as no summary, never as the reassuring one.
        return status == null ? undefined : i18nT('pages.settings.securityPanel.interactive')
      case 'rules':
        return dc ? String(dc.builtins.filter(r => r.enabled).length) : undefined
      case 'tailnet':
        // An unread state gets no summary, never the reassuring one. The label
        // is the server-owned `state`, so the rail cannot disagree with the
        // card it navigates to.
        if (tailnetError || tailnet === undefined) return undefined
        return i18nT(TAILNET_STATE_KEY[tailnet.state])
      case 'apps':
        // An UNREADABLE value is not "off" — mirror the card's own handling and
        // render no summary rather than asserting a state we could not read.
        if (cfgError || cfg === undefined) return undefined
        return cfg.agent?.apps_allow_third_party === true
          ? i18nT('pages.settings.securityPanel.state_allowed')
          : i18nT('pages.settings.securityPanel.state_blocked')
      case 'layers':
        return String(FEATURES.length)
      default:
        return undefined
    }
  }

  const items = SECURITY_SECTIONS.map(section => {
    const summary = summaryFor(section.key)
    return {
      key: section.key,
      label: i18nT(SECTION_LABEL_KEY[section.key]),
      icon: section.icon,
      group: i18nT(SECTION_GROUP_KEY[section.group]),
      summary: summary ? (
        <span className="block text-[11px] text-muted tabular-nums truncate mt-px">{summary}</span>
      ) : undefined,
    }
  })

  return (
    <SettingsSubNav
      items={items}
      railWidth={248}
      listLabel={i18nT('pages.settings.securityPanel.security_sections')}
      backLabel={i18nT('settings.tabs.security.label')}
      basePath={basePath}
    >
      {active => {
        const key = active as SecuritySectionKey | null
        return (
          <>
            {key === 'posture' && <PostureSection />}
            {key === 'approval' && (
              <SettingsSection title={i18nT('pages.settings.securityPanel.yolo_auto_approve')}>
                <YoloDurationCard />
              </SettingsSection>
            )}
            {key === 'rules' && <DeniedCommandsSection draft={denyDraft} onDraftChange={setDenyDraft} noteDraft={denyNoteDraft} onNoteDraftChange={setDenyNoteDraft} />}
            {key === 'tailnet' && (
              <SettingsSection title={i18nT('pages.settings.securityPanel.tailnet_section')}>
                <TailnetOriginCard />
              </SettingsSection>
            )}
            {key === 'apps' && (
              <SettingsSection title={i18nT('pages.settings.securityPanel.third_party_apps_section')}>
                <ThirdPartyAppsCard />
              </SettingsSection>
            )}
            {key === 'registries' && (
              <SettingsSection title={i18nT('pages.settings.securityPanel.trustedRegistries.section_title')}>
                <TrustedRegistriesCard />
              </SettingsSection>
            )}
            {key === 'redaction' && <RedactionSection />}
            {key === 'delivery' && (
              <SettingsSection title={i18nT('pages.settings.securityPanel.file_delivery_section')}>
                <FileDeliveryConsentCard />
              </SettingsSection>
            )}
            {key === 'layers' && <LayersSection />}
            {key === 'governance' && <GovernancePolicyViewer />}
            {key === 'docs' && <DocsSection />}
          </>
        )
      }}
    </SettingsSubNav>
  )
}
