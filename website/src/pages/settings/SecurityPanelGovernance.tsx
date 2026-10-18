/**
 * Settings -> Security -> Governance policy: the read-only viewer of the
 * effective governance ceiling, its scope labels and planes, and the
 * central-distribution block (`SecurityPanel.tsx` re-exports `SCOPE_LABEL_KEY`,
 * `humaniseScopeLeaf` and `GOV_PLANE_TITLE_KEY`).
 */
import React, { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  ShieldCheck, Lock, Terminal, Globe, AlertTriangle, Clock, Gavel, Building2, Gauge, ToggleRight,
  MessageSquare, ListChecks, Network, Package,
} from 'lucide-react'
import { Badge } from '../../components/ui'
import { SettingsSection, SettingsCard } from '../../components/settings'
import InfoTip from '../../components/InfoTip'
import {
  api, type GovernanceDistributionData, type GovernancePolicyData, type GovernanceScope,
  type GovernanceScopeDetail,
} from '../../api/client'
import { i18nT } from '../../i18n/t'
import { fmtDuration, fmtList, fmtUnit, compareText } from '../../i18n/format'
import ErrorNotice from '../../components/ErrorNotice'
import { PINNED_TOOLTIP_KEY } from './SecurityPanelDeniedCommands'

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
  // Named after the menu entry it withdraws ("Share as image"), not the scope's
  // leaf: a user whose Share entry vanished reads THIS row to learn why, and
  // "Social Share" would not connect to the feature they lost.
  'capabilities.social_share': 'pages.settings.securityPanel.gov_scope_social_share',
}

/** Localised scope name, falling back to a humanised leaf for an unknown scope.
 *
 *  The fallback exists for a scope this build has no catalog entry for, which in
 *  practice means one a COMPANION EDITION registered through ``register_scope``:
 *  the snapshot endpoint iterates ``SCOPE_CATALOG``, so such a row reaches this
 *  panel, but shipping an i18n key for it is impossible — the core does not know
 *  the scope exists.
 *
 *  Humanised rather than raw, because governed-scope leaves are snake_case by
 *  convention (``capabilities.capability_install``,
 *  ``capabilities.external_access``), and a title-cased raw leaf renders them as
 *  `Capability_install` / `External_access` in the middle of an otherwise
 *  copy-edited security panel. Underscores and hyphens become spaces and each word
 *  is capitalised, so the row reads `Capability Install` / `External Access`.
 *
 *  Deliberately NOT localised: the leaf is an operator-authored identifier, not
 *  product copy, so there is nothing to translate and no key to translate it under.
 *  A known scope still resolves through ``SCOPE_LABEL_KEY`` and stays translated in
 *  every locale.
 */
function scopeLabel(scope: string): string {
  // `hasOwnProperty`, not a truthiness test on the lookup: `scope` arrives from
  // `GET /api/governance-policy`, so a scope named `toString` or `constructor` would
  // otherwise resolve to an inherited Object.prototype member and hand a FUNCTION to
  // i18next and then to JSX. Same hazard as `effortLabel` in `lib/effort.ts`.
  if (Object.prototype.hasOwnProperty.call(SCOPE_LABEL_KEY, scope)) return i18nT(SCOPE_LABEL_KEY[scope])
  const leaf = scope.includes('.') ? scope.slice(scope.indexOf('.') + 1) : scope
  return humaniseScopeLeaf(leaf)
}

/** Turn a snake_case / kebab-case scope leaf into Title Case words.
 *
 *  Exported for the test: the panel renders the result verbatim, so the mapping
 *  from identifier to displayed text is worth pinning directly rather than only
 *  through a mounted component.
 */
export function humaniseScopeLeaf(leaf: string): string {
  return leaf
    .split(/[_-]+/)
    // Drop empties so a leading, trailing or doubled separator cannot produce a
    // double space or a stray leading space in the rendered row.
    .filter(Boolean)
    .map(word => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
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
  approval_modes: 'modes',
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

/**
 * Catalog KEY per central-distribution refresh outcome, and per posture error code.
 *
 * Flat `Record`s of full literal keys indexed inline at the `i18nT()` call, the same
 * shape and for the same reason as `TAILNET_STATE_KEY` (SecurityPanelTailnet.tsx): that is the form
 * `scripts/check-i18n-keys.mjs` resolves statically, so a key rendered here is a key
 * it can prove exists.
 *
 * Keyed by `string` rather than by the union, and read through an `in` guard, because
 * these enums are produced by the backend: a value this build has no copy for must
 * render NOTHING, never a raw backend token standing in for English.
 */
const DISTRIBUTION_REFRESH_KEY: Record<string, string> = {
  applied: 'pages.settings.securityPanel.distribution_refresh_applied',
  unchanged: 'pages.settings.securityPanel.distribution_refresh_unchanged',
  rejected: 'pages.settings.securityPanel.distribution_refresh_rejected',
  unreachable: 'pages.settings.securityPanel.distribution_refresh_unreachable',
  not_configured: 'pages.settings.securityPanel.distribution_refresh_not_configured',
}

/** Badge tone for the two refresh outcomes an operator has to act on. Everything
 *  else falls to the muted default below, deliberately: a green badge is a health
 *  verdict, and this block reports facts. */
const DISTRIBUTION_REFRESH_VARIANT: Record<string, 'err' | 'warn'> = {
  rejected: 'err',
  unreachable: 'warn',
}

/**
 * A coarse interval or age, in the units an operator reads at a glance.
 *
 * Days/hours/minutes and no seconds: the poll interval has a 60-second floor and a
 * cache age is judged against a staleness bound measured in hours, so seconds are
 * noise on both. `dropZero` is what keeps three days from rendering as `3d 0h 0m`.
 * Below a minute falls back to seconds, so a cache written moments ago does not read
 * as `0m` — which on this row would look like a broken measurement.
 */
function fmtCoarseSecs(secs: number): string {
  const s = Math.max(0, Math.floor(secs))
  if (s < 60) return fmtUnit(s, 'second', { maximumFractionDigits: 0 })
  return fmtDuration(
    [
      [Math.floor(s / 86400), 'day'],
      [Math.floor((s % 86400) / 3600), 'hour'],
      [Math.floor((s % 3600) / 60), 'minute'],
    ],
    { dropZero: true, maximumFractionDigits: 0 },
  )
}

/**
 * Where the ceiling itself comes from, when an admin publishes one centrally.
 *
 * READ-ONLY like the rest of this viewer, and for a stronger reason: the source is
 * declared in the file-authored policy or in per-machine environment, so a control
 * here could only ever lie about being able to change it.
 *
 * It reports facts and no verdict — the transport, whether the poll loop is alive in
 * this process, how old the last-known-good copy is, what the most recent refresh did,
 * and whether the declaration could be resolved at all. Deliberately no "healthy"
 * badge, by the same rule the section rail follows: a summary that renders its own
 * verdict keeps asserting it while the read behind it is failing.
 *
 * `pending` is the state the rest of the viewer cannot express. A host configured to
 * fetch a central ceiling whose first fetch has not landed reports `has_policy: false`
 * and `profile: null`, which is indistinguishable from an ungoverned machine
 * everywhere else on this page.
 */
function PolicyDistributionBlock({ posture, pending }: { posture: GovernanceDistributionData; pending: boolean }) {
  const interval = posture.refresh_interval_seconds ?? 0
  const age = posture.cache_age_seconds
  const status = posture.last_refresh_status ?? ''
  const errorCode = posture.error_code ?? ''
  return (
    <div className="border-b border-border pb-2 mb-2">
      <div className="flex items-center gap-1.5">
        <Lock size={11} className="lucide-inline text-muted" />
        <span className="text-[11px] font-semibold uppercase tracking-[.04em] text-muted">{i18nT('pages.settings.securityPanel.distribution_title')}</span>
        <InfoTip text={i18nT('pages.settings.securityPanel.distribution_tip')} />
      </div>
      <div className="mt-1 flex items-center gap-2 flex-wrap">
        {/* The SCHEME, never the URL — the producer withholds the endpoint because
            this page is reachable by the agent's own browser tooling, and it is the
            fleet's control plane. A scheme still answers what an operator asks here:
            is the channel encrypted, or is this a local mount.
            Rendered verbatim rather than through a key map, unlike the two enums
            below: the transport registry is extensible, so a map would be incomplete
            by construction and would silently blank out an edition's own scheme. It
            is an identifier in a mono badge, not copy — there is nothing to
            translate. */}
        {posture.source_scheme && (
          <Badge variant="muted"><Network size={11} className="lucide-inline" /> {i18nT('pages.settings.securityPanel.distribution_source', { scheme: posture.source_scheme })}</Badge>
        )}
        {posture.configured && (
          <Badge variant="muted">
            <Clock size={11} className="lucide-inline" />{' '}
            {/* Three distinct facts, not one with two spellings: no interval means
                boot-only fetching (still centrally governed), an interval with no live
                loop means a pushed change will NOT bind until the next restart, and a
                live loop is the state the feature promises. */}
            {interval <= 0
              ? i18nT('pages.settings.securityPanel.distribution_boot_only')
              : posture.refresher_running
                ? i18nT('pages.settings.securityPanel.distribution_polling', { interval: fmtCoarseSecs(interval) })
                : i18nT('pages.settings.securityPanel.distribution_polling_stopped')}
          </Badge>
        )}
        {posture.configured && (
          <Badge variant={posture.cache_present ? 'muted' : 'warn'}>
            <Package size={11} className="lucide-inline" />{' '}
            {/* The cache is what keeps a host governed through an outage, so its
                absence is the fact worth a warn tone: this machine has no fallback
                if the source goes away. */}
            {posture.cache_present && typeof age === 'number'
              ? i18nT('pages.settings.securityPanel.distribution_cache_age', { age: fmtCoarseSecs(age) })
              : i18nT('pages.settings.securityPanel.distribution_cache_none')}
          </Badge>
        )}
        {status in DISTRIBUTION_REFRESH_KEY && (
          <Badge variant={DISTRIBUTION_REFRESH_VARIANT[status] ?? 'muted'}>{i18nT(DISTRIBUTION_REFRESH_KEY[status])}</Badge>
        )}
        {errorCode === 'misconfigured' && (
          <Badge variant="warn"><AlertTriangle size={11} className="lucide-inline" /> {i18nT('pages.settings.securityPanel.distribution_error_misconfigured')}</Badge>
        )}
      </div>
      {/* The chips above are the job's status glyphs; the two outcomes that mean
          the LAST REFRESH FAILED (a rejected or unreachable source) and a
          declaration the pins cannot parse are failures of a background job —
          the rule's `last_error` case — so they also render through
          ErrorNotice. Read-only block, nothing to lose → hand-off on. Literal
          keys per outcome, not a computed one, so the key gate can see them. */}
      {(status === 'rejected' || status === 'unreachable' || errorCode === 'misconfigured') && (
        <ErrorNotice
          variant="inline"
          className="mt-1.5"
          message={errorCode === 'misconfigured'
            ? i18nT('pages.settings.securityPanel.distribution_error_misconfigured_detail')
            : status === 'rejected'
              ? i18nT('pages.settings.securityPanel.distribution_refresh_rejected_detail')
              : i18nT('pages.settings.securityPanel.distribution_refresh_unreachable_detail')}
          askAgent
        />
      )}
      {pending && (
        <div className="text-[12px] text-warn mt-1.5 flex items-start gap-1.5 leading-relaxed">
          <AlertTriangle size={13} className="lucide-inline shrink-0 mt-0.5" />
          <span>{i18nT('pages.settings.securityPanel.distribution_awaiting_policy')}</span>
        </div>
      )}
    </div>
  )
}

/** Read-only viewer: the effective governance ceiling across every scope. */
export function GovernancePolicyViewer() {
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

  // Central distribution reports itself on BOTH the normal and the fail-safe
  // snapshot, so it survives `unavailable` — a host whose governance resolution
  // glitched still knows where its ceiling is fetched from, and that is exactly when
  // an operator needs to be told. A misconfigured declaration reports
  // `configured: false` (the fleet DID point this host somewhere, the pins just do not
  // parse), so the block renders on either signal or the one state that most needs
  // saying is the one that disappears.
  const distribution = data?.distribution
  const distributionConfigured = !!distribution && (distribution.configured || !!distribution.error_code)

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

  // Profiles naming capability scopes this build does not register — a
  // companion edition's scopes (or a misspelled key), tolerated at load and
  // inert here. Empty lists are filtered so a profile can never render a badge
  // with nothing after it. Deliberately NOT memoized: compareText resolves the
  // active language per call, and a useMemo whose deps exclude the language
  // would keep the old sort order across a language switch.
  const unknownScopeRows = Object.entries(data?.unknown_profile_scopes ?? {})
    .filter(([, scopes]) => scopes.length > 0)
    .sort(([a], [b]) => compareText(a, b))

  return (
    <SettingsSection title={i18nT('pages.settings.securityPanel.governance_policy')}>
      <SettingsCard>
        {/* data-setting-label: deep-link anchor for the manual registry entry
            (settingsManual.ts) — the highlight hook queries the rendered label. */}
        <div className="flex items-start gap-3 pb-1" data-setting-label={i18nT('pages.settings.securityPanel.governance_policy')}>
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

        {/* Rendered OUTSIDE the branch tree below, not inside one arm of it. Every
            arm is a claim about the ceiling, and where the ceiling comes from
            qualifies all three of them: the soft "temporarily unavailable" notice, the
            reassuring "no enterprise policy" card, and the governed rows. Putting it
            here is what keeps `unavailable` from swallowing a distribution state the
            payload did report. */}
        {distributionConfigured && distribution && (
          // `pending` is gated on `!unavailable` because the fail-safe snapshot sets
          // `has_policy: false` UNCONDITIONALLY — it is what the backend answers when it
          // could not resolve the ceiling at all, not a report that there is none. Read
          // as pending it would tell an operator no enterprise ceiling restricts the
          // host surface on a machine whose ceiling is installed and enforcing, which is
          // the same false reassurance the term below exists to remove, in the other
          // direction.
          <PolicyDistributionBlock posture={distribution} pending={!unavailable && !data?.has_policy} />
        )}

        {isLoading ? (
          <div className="text-[12px] text-muted py-2">{i18nT('pages.settings.securityPanel.loading_governance_policy')}</div>
        ) : unavailable ? (
          // A failed read or the backend's fail-safe snapshot. Read-only viewer,
          // nothing to lose → hand-off on.
          <ErrorNotice
            className="mt-1"
            message={i18nT('pages.settings.securityPanel.governance_status_is_temporarily_unavailable_enf')}
            askAgent
          />
        ) : /* `!distributionConfigured` is a load-bearing term, not a tidy-up. A host
              pointed at a central ceiling whose first fetch has not landed reports
              `has_policy: false` and `profile: null`, so without it this arm renders a
              reassuring "no enterprise policy in effect" — with a green shield — for a
              machine that is supposed to have one and could not get it. It yields to
              the scope rows instead, which say every scope is unrestricted, and the
              distribution block above says why. */
          !data?.has_policy && !data?.profile && unknownScopeRows.length === 0 && !distributionConfigured ? (
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
            {(data?.fallback_profiles?.length ?? 0) > 0 && (
              <div className="flex items-start gap-2.5 py-2.5 mt-1 mb-1 rounded-md bg-warn/10 border border-warn/30 px-3">
                <AlertTriangle size={14} className="lucide-inline text-warn shrink-0 mt-0.5" />
                <div className="text-[12px] text-text leading-relaxed">
                  {/* Title on its OWN line, not joined to the body by a space:
                      inline they render as one run-on sentence ("… in effect A
                      profile named below could not be loaded"). */}
                  <div className="font-semibold">{i18nT('pages.settings.securityPanel.profile_unusable_title')}</div>
                  <div>
                    {i18nT('pages.settings.securityPanel.profile_unusable_body')}
                    {/* fmtList for the same reason the other_bound_surfaces block
                        below gives: zh joins with 、 and no spaces, so a literal
                        ', ' renders wrong in several of the twelve locales. */}
                    {' '}{i18nT('pages.settings.securityPanel.profile_unusable_which', { profiles: fmtList(data!.fallback_profiles!, { type: 'unit' }) })}
                  </div>
                  {/* The cause list and the restart caveat are the rare edge of a
                      rare state; demoted so the two sentences that matter are not
                      buried in a paragraph wall at 12px. */}
                  <div className="text-muted mt-1">{i18nT('pages.settings.securityPanel.profile_unusable_detail')}</div>
                </div>
              </div>
            )}
            {/* Profiles naming capability scopes this build does not register —
                typically a companion edition's scopes, tolerated at load and
                inert here. Rendered so a reader auditing a profile file can
                account for every key it declares, not just the ones this build
                enforces. Reachable even with no policy and no host profile: the
                payload aggregates EVERY loaded profile (see
                `unknown_profile_scopes` in api/client/security.ts), so the
                no-policy branch above yields to this block when rows exist. */}
            {unknownScopeRows.length > 0 && (
              <div className="border-t border-border pt-2 mt-2">
                <div className="flex items-center gap-1.5">
                  <span className="text-[11px] font-semibold uppercase tracking-[.04em] text-muted">{i18nT('pages.settings.securityPanel.unknown_scopes_title')}</span>
                  <InfoTip text={i18nT('pages.settings.securityPanel.unknown_scopes_tip')} />
                </div>
                <div className="mt-1 space-y-1">
                  {unknownScopeRows.map(([stem, scopes]) => (
                    <div key={stem} className="flex items-center gap-2 flex-wrap">
                      <Badge variant="muted"><ListChecks size={11} className="lucide-inline" /> {stem}</Badge>
                      {/* fmtList for the same reason as the profile_unusable
                          block above: zh joins with 、 and no spaces. */}
                      <span className="text-[11px] font-mono text-muted break-all">{fmtList(scopes, { type: 'unit' })}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
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
