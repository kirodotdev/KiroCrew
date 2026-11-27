/** A connection source this Crew does not have yet, as the dashed card People
 *  shows for it (`PeopleSources`): its name, the status the gateway reports,
 *  what it is for, and its setup button.
 *
 *  Guidance only. The setup button opens the source's guide, where the source
 *  is configured; the card turns solid once the gateway reports it on. The
 *  owner of a Crew whose Tailscale invite read answered also keeps the Invite…
 *  entry here, as the page offered it before Tailscale was set up. */
import type { ReactNode } from 'react'
import { ExternalLink } from 'lucide-react'
import type { ConnectionSource, SourceId } from '../../api/sharedCrewMode'
import { SSM_GUIDE_URL } from '../../api/sharedCrewSsm'
import { i18nT } from '../../i18n/t'
import { statusBadge } from './PeopleModes'
import { InviteEntry } from './TailscaleInvites'

const GUIDE_BASE = 'https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/'

/** Existing guides only; a source with no guide shows no setup button. */
const GUIDES: Partial<Record<SourceId, string>> = {
  tailscale: `${GUIDE_BASE}remote-and-mobile.md#share-one-crew-with-teammates`,
  ssm: SSM_GUIDE_URL,
}

/** A source's name, as its card and each person's chip read it. */
export function sourceName(id: SourceId): string {
  switch (id) {
    case 'tailscale': return i18nT('sharedCrew.setup_tailscale')
    case 'ssm': return i18nT('sharedCrew.setup_ssm')
    case 'organization': return i18nT('sharedCrew.source_org_title')
    default: return i18nT('sharedCrew.setup_kiro_login')
  }
}

/** The source a person or an invite came in through, as a small chip on their row. */
export function SourceChip({ id }: { id: SourceId }) {
  return (
    <span className="inline-flex items-center rounded border border-border px-1.5 text-[11px] leading-[18px] text-muted whitespace-nowrap" data-testid="person-source" data-source={id}>
      {sourceName(id)}
    </span>
  )
}

/** The card frame: solid while the source is on, dashed while it is not. */
export function SourceFrame({ id, on, children }: { id: SourceId; on: boolean; children: ReactNode }) {
  return (
    <li
      className={`flex flex-col gap-2 min-w-0 rounded-lg border p-4 ${on ? 'border-border bg-card' : 'border-dashed border-border'}`}
      data-testid={`source-${id}`}
      data-state={on ? 'on' : 'off'}
    >
      {children}
    </li>
  )
}

export function SourceHeader({ id, badge }: { id: SourceId; badge: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-2 min-w-0 flex-wrap">
      <span className="text-sm font-semibold text-text-strong">{sourceName(id)}</span>
      <span data-testid={`source-${id}-status`}>{badge}</span>
    </div>
  )
}

function setupBody(id: SourceId): string {
  return id === 'ssm' ? i18nT('sharedCrew.setup_ssm_body') : i18nT('sharedCrew.setup_tailscale_body')
}

export function SetupSourceCard({ source, onOpenChange }: { source: ConnectionSource; onOpenChange?: (open: boolean) => void }) {
  const guide = GUIDES[source.id]
  const name = sourceName(source.id)
  return (
    <SourceFrame id={source.id} on={false}>
      <SourceHeader id={source.id} badge={statusBadge(source)} />
      <p className="text-[13px] text-muted m-0">{setupBody(source.id)}</p>
      <div className="flex items-center gap-3 min-w-0 flex-wrap">
        {guide && (
          <a
            className="inline-flex items-center gap-1 self-start rounded-md border border-border px-3 py-1.5 text-[13px] text-text-strong hover:bg-bg-hover"
            href={guide}
            target="_blank"
            rel="noopener noreferrer"
            data-testid={`source-${source.id}-setup`}
          >
            {i18nT('sharedCrew.setup_source', { name })}
            <ExternalLink size={12} className="lucide-inline" aria-hidden />
          </a>
        )}
        {source.id === 'tailscale' && <InviteEntry transport="tailscale" showErrors={false} onOpenChange={onOpenChange} />}
      </div>
    </SourceFrame>
  )
}
