/** The one empty state the People surface shows while this Crew has no
 *  shared-access path: it names each setup path, the availability the gateway's
 *  capabilities report for it, and a guide where one exists.
 *
 *  Guidance only. Nothing here sets a path up, so there is no button: each path
 *  is configured where its guide says, and the row turns available once the
 *  gateway reports it. */
import { ExternalLink, Users } from 'lucide-react'
import type { CapabilityState, SetupPath, SetupPathCapability, SharedCrewCapabilities } from '../../api/sharedCrew'
import { SETUP_PATH_ORDER, setupPathStates } from '../../api/sharedCrewUi'
import { Badge } from '../../components/ui'
import { i18nT } from '../../i18n/t'

const GUIDE_BASE = 'https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/'

/** Existing guides only; a path with no guide shows none. */
const GUIDES: Partial<Record<SetupPath, string>> = {
  tailscale: `${GUIDE_BASE}remote-and-mobile.md#share-one-crew-with-teammates`,
  systems_manager: `${GUIDE_BASE}cloud-instance-ssm-vs-ssh.md`,
}

function pathName(path: SetupPath): string {
  switch (path) {
    case 'tailscale': return i18nT('sharedCrew.setup_tailscale')
    case 'kiro_login': return i18nT('sharedCrew.setup_kiro_login')
    default: return i18nT('sharedCrew.setup_systems_manager')
  }
}

function pathBody(path: SetupPath): string {
  switch (path) {
    case 'tailscale': return i18nT('sharedCrew.setup_tailscale_body')
    case 'kiro_login': return i18nT('sharedCrew.setup_kiro_login_body')
    default: return i18nT('sharedCrew.setup_systems_manager_body')
  }
}

function statusOf(row: SetupPathCapability): { label: string; variant: 'ok' | 'warn' | 'muted' } {
  const state: CapabilityState = row.state
  if (state === 'available') return { label: i18nT('sharedCrew.setup_status_available'), variant: 'ok' }
  if (state === 'degraded') return { label: i18nT('sharedCrew.setup_status_degraded'), variant: 'warn' }
  if (row.reason === 'not_in_this_build') return { label: i18nT('sharedCrew.setup_status_not_in_build'), variant: 'muted' }
  return { label: i18nT('sharedCrew.setup_status_not_set_up'), variant: 'muted' }
}

function SetupPathRow({ path, row }: { path: SetupPath; row: SetupPathCapability }) {
  const status = statusOf(row)
  const guide = GUIDES[path]
  return (
    <li className="flex flex-col gap-1 py-2.5 min-w-0" data-testid={`setup-path-${path}`} data-state={row.state}>
      <div className="flex items-center gap-2 min-w-0 flex-wrap">
        <span className="text-sm font-medium text-text-strong">{pathName(path)}</span>
        <Badge variant={status.variant} data-testid={`setup-path-${path}-status`}>{status.label}</Badge>
      </div>
      <p className="text-[13px] text-muted">{pathBody(path)}</p>
      {guide && (
        <a className="inline-flex items-center gap-1 text-[13px] text-accent self-start" href={guide} target="_blank" rel="noopener noreferrer" data-testid={`setup-path-${path}-guide`}>
          {i18nT('sharedCrew.setup_guide_link', { name: pathName(path) })}
          <ExternalLink size={12} className="lucide-inline" aria-hidden />
        </a>
      )}
    </li>
  )
}

export function SharedAccessSetup({ capabilities }: { capabilities: SharedCrewCapabilities }) {
  const states = setupPathStates(capabilities)
  return (
    <section className="flex flex-col gap-3 w-full max-w-[640px] mx-auto py-6 px-1 sm:px-0" data-testid="shared-access-setup" aria-labelledby="shared-access-setup-title">
      <div className="flex flex-col items-center gap-2 text-center">
        <div className="text-[40px] opacity-[.12] select-none" aria-hidden><Users size={20} /></div>
        <h3 id="shared-access-setup-title" className="text-text-strong text-sm font-medium">{i18nT('sharedCrew.setup_title')}</h3>
        <p className="text-muted text-[13px] max-w-[420px]">{i18nT('sharedCrew.setup_body')}</p>
      </div>
      <ul className="flex flex-col divide-y divide-border border border-border rounded-lg px-3 bg-bg-elevated" aria-label={i18nT('sharedCrew.setup_list_label')}>
        {SETUP_PATH_ORDER.map(path => <SetupPathRow key={path} path={path} row={states[path]} />)}
      </ul>
    </section>
  )
}
