/** Settings > People > Connection sources: one card per way people reach this
 *  Crew, all at once (`GET /api/shared-crew/mode` `sources`).
 *
 *  - Tailscale and AWS SSM: status, how many people came in, and the owner's
 *    own invite button ("Invite…", "Make invite link"). Tailscale also holds the
 *    Crew link to send. Not set up, each is a dashed card with its setup guide
 *    (`SetupSourceCard`).
 *  - Your organization: only while a Console manages access; read-only, with its
 *    Console link.
 *  - Kiro Login: only while this Crew is shared as a team Crew over the Kiro
 *    relay; read-only, since that sharing is managed outside Kiro Crew.
 *
 *  A teammate sees the sources that are on, with each invite button locked:
 *  inviting and setting a source up are the owner's (`useViewerIsNotOwner`). */
import { useState } from 'react'
import { Check, Copy, ExternalLink, Lock } from 'lucide-react'
import type { SharedCrewCapabilities, TailnetDirect } from '../../api/sharedCrew'
import { SOURCE_ORDER, sourceOn, type ConnectionSource, type CrewModeResponse, type SourceId } from '../../api/sharedCrewMode'
import { SettingsSection } from '../../components/settings'
import { Badge, Btn, Input } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { tailnetDirectActive } from '../../components/TailnetDirectLink'
import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { copyToClipboard } from '../../utils/clipboard'
import { ConsoleAccess, TAILSCALE_USERS_URL, statusBadge } from './PeopleModes'
import { SetupSourceCard, SourceFrame, SourceHeader } from './SharedAccessSetup'
import { SsmReadError, useSsmStatus } from './SsmInvite'
import { CopyInviteMessage, InviteEntry, InviteReadError, useInvites } from './TailscaleInvites'

/** The card's body line, then how many came in through it. */
function SourceBody({ id, text, people }: { id: SourceId; text: string; people: number }) {
  return (
    <p className="text-[13px] text-muted m-0" data-testid={`source-${id}-body`}>
      {text}
      <span aria-hidden>{'\u00A0·\u00A0'}</span>
      <span data-testid={`source-${id}-people`}>{i18nT('sharedCrew.source_people', { n: fmtNumber(people) })}</span>
    </p>
  )
}

/** A teammate's view of an owner's invite button: shown, locked, and saying why. */
function LockedInvite({ id, label }: { id: SourceId; label: string }) {
  const noteId = `source-${id}-locked-note`
  return (
    <div className="flex flex-col gap-1 min-w-0">
      <Btn disabled aria-describedby={noteId} className="self-start" data-testid={`source-${id}-invite-locked`}>
        <Lock size={14} className="lucide-inline" aria-hidden /> {label}
      </Btn>
      <span id={noteId} className="text-[12px] text-muted">{i18nT('sharedCrew.invite_err_owner')}</span>
    </div>
  )
}

/** The Crew link to send, and its Copy: the owner copies the whole invite
 *  message, a teammate the link. */
function CrewLink({ direct, owner }: { direct: TailnetDirect; owner: boolean }) {
  const [copied, setCopied] = useState<boolean | null>(null)
  const [primary, ...fallbacks] = direct.urls
  const copy = async () => {
    let ok = false
    try { ok = await copyToClipboard(primary) } catch { ok = false }
    setCopied(ok)
    if (ok) setTimeout(() => setCopied(null), 2000)
  }
  return (
    <div className="flex flex-col gap-2 min-w-0" data-testid="share-hero-link-row">
      <div className="flex items-center gap-2 min-w-0 flex-wrap">
        <Input
          readOnly
          value={primary}
          onFocus={e => e.currentTarget.select()}
          aria-label={i18nT('sharedCrew.direct_link_label')}
          className="flex-1 min-w-[12rem] font-mono text-[12px]"
          data-testid="share-hero-link"
        />
        {owner ? (
          <CopyInviteMessage testId="share-hero-copy-message" />
        ) : (
          <Btn onClick={() => { void copy() }} data-testid="share-hero-copy">
            {copied ? <Check size={14} className="lucide-inline" aria-hidden /> : <Copy size={14} className="lucide-inline" aria-hidden />}
            {' '}{copied ? i18nT('sharedCrew.invite_copied') : i18nT('sharedCrew.hero_copy_link')}
          </Btn>
        )}
      </div>
      {/* No hand-off: an invite draft may be open in the dialog. */}
      {copied === false && <ErrorNotice variant="inline" message={i18nT('sharedCrew.direct_link_copy_failed')} testId="share-hero-copy-error" />}
      {fallbacks.length > 0 && (
        <details className="text-[12px] text-muted">
          <summary className="cursor-pointer">{i18nT('sharedCrew.direct_link_fallbacks')}</summary>
          <ul className="flex flex-col gap-1 pt-1 font-mono break-all">
            {fallbacks.map(url => <li key={url}>{url}</li>)}
          </ul>
        </details>
      )}
    </div>
  )
}

function TailscaleCard({ source, direct, teammate, onOpenChange }: { source: ConnectionSource; direct: TailnetDirect | undefined; teammate: boolean; onOpenChange: (open: boolean) => void }) {
  const active = tailnetDirectActive(direct)
  let host = ''
  if (active) {
    try { host = new URL(direct.urls[0]).hostname } catch { host = '' }
  }
  return (
    <SourceFrame id="tailscale" on>
      <SourceHeader id="tailscale" badge={statusBadge(source)} />
      <SourceBody id="tailscale" text={host ? i18nT('sharedCrew.source_tailscale_reachable', { host }) : i18nT('sharedCrew.setup_tailscale_body')} people={source.people} />
      {teammate ? <LockedInvite id="tailscale" label={i18nT('sharedCrew.hero_invite')} /> : (
        <InviteEntry transport="tailscale" showErrors={false} onOpenChange={onOpenChange}>
          <a className="inline-flex items-center gap-1 text-[13px] text-accent" href={TAILSCALE_USERS_URL} target="_blank" rel="noopener noreferrer" data-testid="share-hero-manage">
            {i18nT('sharedCrew.adapter_tailscale_manage')}
            <ExternalLink size={12} className="lucide-inline" aria-hidden />
          </a>
        </InviteEntry>
      )}
      {active && <CrewLink direct={direct} owner={!teammate} />}
    </SourceFrame>
  )
}

function SsmCard({ source, teammate, onOpenChange }: { source: ConnectionSource; teammate: boolean; onOpenChange: (open: boolean) => void }) {
  const ssm = useSsmStatus().data
  const listening = ssm?.state === 'active' && ssm.port > 0
  return (
    <SourceFrame id="ssm" on>
      <SourceHeader id="ssm" badge={statusBadge(source)} />
      <SourceBody id="ssm" text={listening ? i18nT('sharedCrew.source_ssm_listening', { port: String(ssm.port) }) : i18nT('sharedCrew.setup_ssm_body')} people={source.people} />
      {teammate ? <LockedInvite id="ssm" label={i18nT('sharedCrew.source_make_invite_link')} /> : <InviteEntry transport="ssm" showErrors={false} onOpenChange={onOpenChange} />}
    </SourceFrame>
  )
}

function OrganizationCard({ source, mode }: { source: ConnectionSource; mode: CrewModeResponse | null | undefined }) {
  return (
    <SourceFrame id="organization" on>
      <SourceHeader id="organization" badge={<Badge variant="muted">{i18nT('sharedCrew.managed_by_admin')}</Badge>} />
      <SourceBody id="organization" text={i18nT('sharedCrew.source_org_body')} people={source.people} />
      {mode && <ConsoleAccess mode={mode} />}
    </SourceFrame>
  )
}

function KiroLoginCard({ source }: { source: ConnectionSource }) {
  return (
    <SourceFrame id="kiro_login" on>
      <SourceHeader id="kiro_login" badge={statusBadge(source)} />
      <SourceBody id="kiro_login" text={i18nT('sharedCrew.source_kiro_login_body')} people={source.people} />
    </SourceFrame>
  )
}

/** Which cards a viewer sees, in order: the sources that are on, then (for the
 *  owner) Tailscale and AWS SSM not set up. Your organization and Kiro Login
 *  are not set up here, so they show only while on. */
export function visibleSources(sources: readonly ConnectionSource[], teammate: boolean): ConnectionSource[] {
  const byId = new Map(sources.map(s => [s.id, s]))
  const ordered = SOURCE_ORDER.map(id => byId.get(id)).filter((s): s is ConnectionSource => !!s)
  const on = ordered.filter(sourceOn)
  const setUp = teammate ? [] : ordered.filter(s => !sourceOn(s) && (s.id === 'tailscale' || s.id === 'ssm'))
  return [...on, ...setUp]
}

export function PeopleSources({ sources, capabilities, mode, teammate }: {
  sources: readonly ConnectionSource[]
  capabilities: SharedCrewCapabilities
  mode: CrewModeResponse | null | undefined
  /** The viewer is not this machine's owner. */
  teammate: boolean
}) {
  const invites = useInvites()
  const ssm = useSsmStatus()
  const shown = visibleSources(sources, teammate)
  // An open invite dialog owns the read notices, with its draft beside them.
  const [dialogOpen, setDialogOpen] = useState(false)
  return (
    <SettingsSection title={i18nT('sharedCrew.sources_title')}>
      <div className="flex flex-col gap-3 min-w-0" data-testid="people-sources">
        <p className="text-[13px] text-muted m-0">{i18nT('sharedCrew.sources_note')}</p>
        <ul className="grid gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3" aria-label={i18nT('sharedCrew.sources_title')}>
          {shown.map(source => {
            if (!sourceOn(source)) return <SetupSourceCard key={source.id} source={source} onOpenChange={setDialogOpen} />
            switch (source.id) {
              case 'tailscale': return <TailscaleCard key={source.id} source={source} direct={capabilities.tailnet_direct} teammate={teammate} onOpenChange={setDialogOpen} />
              case 'ssm': return <SsmCard key={source.id} source={source} teammate={teammate} onOpenChange={setDialogOpen} />
              case 'organization': return <OrganizationCard key={source.id} source={source} mode={mode} />
              default: return <KiroLoginCard key={source.id} source={source} />
            }
          })}
        </ul>
        {/* One owner for each invite read's failure: the cards' buttons hide while it shows. */}
        {!dialogOpen && <InviteReadError query={invites} testId="people-invites-error" />}
        {!dialogOpen && <SsmReadError query={ssm} testId="people-ssm-error" />}
      </div>
    </SettingsSection>
  )
}
