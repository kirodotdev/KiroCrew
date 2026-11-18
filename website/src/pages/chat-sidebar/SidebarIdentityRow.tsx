/** The sidebar's identity row on a team Crew: who this tab is recorded as and
 *  the path it came in on ("Ana via Kiro Login"), so a person knows the name
 *  their sends carry before they send, and a read-only "Shared" status with how
 *  many people are here now.
 *
 *  Shown by the one rule every team surface follows (`useTeamAttribution`): the
 *  Shared Crew preview is on, the mode is read and not personal, and someone
 *  besides the owner has joined. There is no toggle: sharing is set up in
 *  Settings > People. The reads' failures belong to the read notices below
 *  (`SharedCrewReadNotices`), so this row shows none. */
import { CircleUser, Users } from 'lucide-react'
import type { ViewerVia } from '../../api/sharedCrewMode'
import { isHereNow, principalName } from '../../api/sharedCrewUi'
import { useSharedCrewMode, useSharedCrewPeople, useTeamAttribution } from '../../hooks/sharedCrew/useSharedCrew'
import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/** "<name> via <path>", or the name alone at this machine or on a path the
 *  gateway did not name. */
export function identityLabel(name: string, via: ViewerVia | undefined): string {
  switch (via) {
    case 'kiro_login': return i18nT('sharedCrew.identity_via_kiro_login', { name })
    case 'tailscale': return i18nT('sharedCrew.identity_via_tailscale', { name })
    case 'ssm': return i18nT('sharedCrew.identity_via_ssm', { name })
    default: return name
  }
}

/** The path alone ("via Kiro Login"), drawn beside the name so it never truncates. */
function viaWords(via: ViewerVia | undefined): string {
  switch (via) {
    case 'kiro_login': return i18nT('sharedCrew.group_via_kiro_login')
    case 'tailscale': return i18nT('sharedCrew.group_via_tailscale')
    case 'ssm': return i18nT('sharedCrew.group_via_ssm')
    default: return ''
  }
}

export function SidebarIdentityRow() {
  const team = useTeamAttribution()
  // The same cached reads `useTeamAttribution` made; mounting here refetches nothing.
  const mode = useSharedCrewMode(team, { fetchOnMount: false })
  const people = useSharedCrewPeople(team, { fetchOnMount: false })
  if (!team) return null
  const name = principalName(mode.data?.viewer)
  const here = (people.data?.people ?? []).filter(isHereNow).length
  const sharedLabel = here > 0
    ? i18nT('sharedCrew.identity_shared_here', { n: fmtNumber(here) })
    : i18nT('sharedCrew.identity_shared')
  return (
    <div className="flex items-center gap-2 px-3 pb-1 min-w-0 text-[12px] text-muted" data-testid="sidebar-identity-row">
      {name && (
        <span className="inline-flex items-center gap-1 min-w-0 flex-1" title={i18nT('sharedCrew.identity_title', { name })} data-testid="sidebar-identity">
          <CircleUser size={13} className="lucide-inline shrink-0" aria-hidden />
          {/* The name gives way first, so the path stays readable in a narrow sidebar. */}
          <span className="sr-only">{identityLabel(name, mode.data?.viewer_via)}</span>
          <span className="truncate" aria-hidden>{name}</span>
          {viaWords(mode.data?.viewer_via) && <span className="shrink-0" aria-hidden>{viaWords(mode.data?.viewer_via)}</span>}
        </span>
      )}
      {/* Compact, so the name and its path keep the room: the count with the
          people icon, and the full "Shared · N here now" as its name and tooltip. */}
      <span
        className="ml-auto inline-flex items-center gap-1 shrink-0"
        data-testid="sidebar-shared-status"
        title={sharedLabel}
        aria-label={sharedLabel}
        role="img"
      >
        <Users size={13} className="lucide-inline" aria-hidden />
        <span aria-hidden>{here > 0 ? fmtNumber(here) : i18nT('sharedCrew.identity_shared')}</span>
      </span>
    </div>
  )
}
