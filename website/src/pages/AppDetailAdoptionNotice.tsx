/**
 * The App Store detail page's notice for a former built-in that the gateway is
 * switching to its App Store (registry) copy (`/api/apps` `adoption`).
 *
 * Shown only when there is something to say: the switch is on hold, a transient
 * attempt failed, the copy is staged for the next restart, or the installed copy
 * needs a newer host. Each reason code maps to the copy for its REMEDY, so codes
 * with one remedy share one message; the code itself stays out of the copy. The
 * copy names the switch itself ("switching <app> to its App Store version"), so
 * no line rests on an unexplained "the move". A record still owned by the
 * built-in also says it stays one, which answers "is my app broken?" without
 * claiming it runs: the page's own Enabled/Disabled tag says that.
 */
import { Info, ShieldAlert } from 'lucide-react'
import type { AdoptionReasonCode, AppAdoptionRow } from '../components/appstore/types'
import { i18nT } from '../i18n/t'

type AdoptionRemedy =
  | 'network' | 'local' | 'policy' | 'trust' | 'permissions' | 'platform' | 'host' | 'verification'

/** Every reason code, by the remedy its copy names. Typed total: a new code fails to compile here. */
const REMEDY_FOR: Record<AdoptionReasonCode, AdoptionRemedy> = {
  network_unreachable: 'network',
  network_timeout: 'network',
  http_status: 'network',
  disk_full: 'local',
  config_unreadable: 'local',
  admission_denied: 'policy',
  trust_conflict: 'trust',
  trust_overlay_owned: 'trust',
  permission_widening: 'permissions',
  platform_mismatch: 'platform',
  host_too_old: 'host',
  host_sdk_too_old: 'host',
  commit_not_found: 'verification',
  archive_digest_mismatch: 'verification',
  tree_digest_mismatch: 'verification',
  manifest_identity_mismatch: 'verification',
  version_mismatch: 'verification',
  tree_requires_build: 'verification',
  tree_has_link: 'verification',
  tree_unportable_name: 'verification',
  tree_too_large: 'verification',
  pinned_feature_unsupported: 'verification',
}

/**
 * Whether the remedy's copy names a way the switch can still finish (a retry, a
 * setting to change, an update). Typed total, like `REMEDY_FOR`. A verification
 * failure is never retried for this release, and a wider-access or wrong-platform
 * copy has no approval or variant this host can offer, so those are on hold.
 */
const REMEDY_HAS_A_THEN: Record<AdoptionRemedy, boolean> = {
  network: true,
  local: true,
  policy: true,
  trust: true,
  host: true,
  permissions: false,
  platform: false,
  verification: false,
}

function remedyText(remedy: AdoptionRemedy, name: string): string {
  switch (remedy) {
    case 'network': return i18nT('pages.appDetailPage.adoption.network', { name })
    case 'local': return i18nT('pages.appDetailPage.adoption.local', { name })
    case 'policy': return i18nT('pages.appDetailPage.adoption.policy', { name })
    case 'trust': return i18nT('pages.appDetailPage.adoption.trust', { name })
    case 'permissions': return i18nT('pages.appDetailPage.adoption.permissions', { name })
    case 'platform': return i18nT('pages.appDetailPage.adoption.platform', { name })
    case 'host': return i18nT('pages.appDetailPage.adoption.host', { name })
    case 'verification': return i18nT('pages.appDetailPage.adoption.verification', { name })
  }
}

/**
 * The notice's sentences for *adoption*, or `[]` when nothing needs saying.
 * Exported for the unit test; the component below only lays them out.
 */
export function adoptionNoticeLines(
  adoption: AppAdoptionRow | undefined,
  name: string,
  builtinOwned: boolean,
): string[] {
  if (!adoption) return []
  const lines: string[] = []
  const reason = adoption.reason
  const remedy = reason && Object.prototype.hasOwnProperty.call(REMEDY_FOR, reason)
    ? REMEDY_FOR[reason]
    : undefined
  if ((adoption.state === 'blocked' || adoption.state === 'pending') && remedy) {
    lines.push(remedyText(remedy, name))
    if (builtinOwned) {
      // Only a remedy the user or a retry can complete has a "then" for the
      // record to stay a built-in until.
      lines.push(REMEDY_HAS_A_THEN[remedy]
        ? i18nT('pages.appDetailPage.adoption.keeps_builtin_until')
        : i18nT('pages.appDetailPage.adoption.keeps_builtin'))
    }
  } else if (adoption.state === 'staged') {
    lines.push(i18nT('pages.appDetailPage.adoption.staged', { name }))
  }
  if (adoption.hostTooOld) {
    lines.push(i18nT('pages.appDetailPage.adoption.installed_needs_newer_host', { name }))
  }
  return lines
}

export default function AppDetailAdoptionNotice({
  adoption,
  name,
  builtinOwned,
}: {
  adoption: AppAdoptionRow | undefined
  /** The app's display name, already localised. */
  name: string
  /** The record is still the built-in's (`origin === 'builtin'`). */
  builtinOwned: boolean
}) {
  const lines = adoptionNoticeLines(adoption, name, builtinOwned)
  if (lines.length === 0) return null
  // Warn-styled only when the owner has something to change; a retry in
  // progress or a staged switch is information, not a problem.
  const blocked = adoption?.state === 'blocked' || !!adoption?.hostTooOld
  return (
    <div
      role="status"
      className={blocked
        ? 'mb-4 flex items-start gap-2 rounded-lg border border-warn/30 bg-warn-subtle p-3 animate-rise'
        : 'mb-4 flex items-start gap-2 rounded-lg border border-border bg-bg-elevated p-3 animate-rise'}
    >
      {blocked
        ? <ShieldAlert size={14} className="mt-[3px] shrink-0 text-warn" />
        : <Info size={14} className="mt-[3px] shrink-0 text-muted" />}
      <span className="text-text text-sm">{lines.join(' ')}</span>
    </div>
  )
}
