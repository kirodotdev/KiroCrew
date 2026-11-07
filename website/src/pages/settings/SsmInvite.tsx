/** AWS SSM as the second transport of the one Invite dialog.
 *
 *  Shares this Crew with teammates who can reach this machine through AWS
 *  Systems Manager. The tab is a status line and the invite form: a name, then
 *  Make link. The link is the whole credential and is shown once. An invite
 *  nobody has opened is a row under Invites in the People list (`SsmInviteRow`); once
 *  they open it, their own row carries the Remove button (`RevokeControl`, the one
 *  control Tailscale invites use too). */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, UserPlus } from 'lucide-react'
import { api } from '../../api/client'
import { inviteErrorCode } from '../../api/sharedCrewInvites'
import { SSM_GUIDE_URL, ssmStatusKey, type SsmCreated, type SsmInvite, type SsmStatus } from '../../api/sharedCrewSsm'
import { Badge, Btn, Input } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { useGuardedMutation } from '../../hooks/sharedCrew/useGuardedMutation'
import { useSharedCrewPeople } from '../../hooks/sharedCrew/useSharedCrew'
import { fmtDateTime } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { ownerDisplayName } from './inviteMessage'
import { CopyInviteMessage, RevokeControl, sharedCrewAccessKey, useFirstShareGate, inviteErrorText } from './TailscaleInvites'

/** Invites whose link was copied in this page's lifetime. A link is shown once,
 *  so any other invite's row says it will not be shown again. */
const copiedLinks = new Set<string>()

export function buildSsmInviteMessage({ owner, command, url, guide = SSM_GUIDE_URL }: { owner: string; command: string; url: string; guide?: string }): string {
  const lines = [
    i18nT('sharedCrew.ssm_msg_header', { owner }),
    i18nT('sharedCrew.ssm_msg_run', { command }),
    i18nT('sharedCrew.ssm_msg_open', { url }),
  ]
  if (guide) lines.push(i18nT('sharedCrew.ssm_msg_guide', { url: guide }))
  return lines.join('\n')
}

/** The owner's SSM status. `null` when this gateway has no SSM routes or the
 *  viewer is not the owner; re-read while a made invite is still unopened. */
export function useSsmStatus() {
  return useQuery({
    queryKey: ssmStatusKey,
    queryFn: () => api.sharedCrewSsm(),
    retry: false,
    refetchInterval: q => ((q.state.data as SsmStatus | null | undefined)?.invites ?? []).some(i => !i.joined) ? 30_000 : false,
  })
}

/** A failed SSM status read, with a retry, at the surface that owns the invite controls.
 *  A 403 or 404 is not a failure (the read answers `null`), so this shows only for a
 *  read that could not be made, when the SSM pane and its invites would otherwise vanish. */
export function SsmReadError({ query, testId }: { query: ReturnType<typeof useSsmStatus>; testId: string }) {
  if (!query.isError) return null
  return (
    <div>
      {/* No hand-off: the People list around this holds unsaved names, pictures and invite drafts. */}
      <ErrorNotice message={inviteErrorText(inviteErrorCode(query.error) || 'error')} testId={testId} />
      <Btn onClick={() => { void query.refetch() }}>{i18nT('components.mobileConnect.try_again')}</Btn>
    </div>
  )
}

/** Whether SSM is a sharing path here: on, whether or not it is working yet. */
export function ssmConfigured(status: SsmStatus | null | undefined): status is SsmStatus {
  return !!status && status.state !== 'off'
}

/** Reasons that are a start-up failure, not a state of the machine. */
const FAILED_REASONS: ReadonlySet<string> = new Set(['port_in_use', 'bind_failed'])

function reasonText(reason: string): string {
  switch (reason) {
    case 'no_target': return i18nT('sharedCrew.ssm_reason_no_target')
    case 'port_in_use': return i18nT('sharedCrew.ssm_reason_port_in_use')
    case 'bind_failed': return i18nT('sharedCrew.ssm_reason_bind_failed')
    default: return i18nT('sharedCrew.ssm_reason_unknown')
  }
}

function GuideLink() {
  return (
    <a className="inline-flex items-center gap-1 text-[13px] text-accent self-start" href={SSM_GUIDE_URL} target="_blank" rel="noopener noreferrer" data-testid="ssm-guide">
      {i18nT('sharedCrew.setup_guide_link', { name: i18nT('sharedCrew.setup_ssm') })}
      <ExternalLink size={12} className="lucide-inline" aria-hidden />
    </a>
  )
}

function StatusLine({ status }: { status: SsmStatus }) {
  if (status.state === 'active') {
    return (
      <div className="flex items-center gap-2 min-w-0 flex-wrap" data-testid="ssm-status">
        <Badge variant="ok">{i18nT('sharedCrew.setup_status_available')}</Badge>
        <span className="text-[13px] text-muted truncate" data-testid="ssm-node">
          {i18nT('sharedCrew.ssm_ready', { node: status.target?.instance_id ?? '' })}
        </span>
      </div>
    )
  }
  return (
    <div className="flex flex-col gap-1 min-w-0" data-testid="ssm-status">
      <div className="flex items-center gap-2 min-w-0 flex-wrap">
        <Badge variant="warn">{i18nT('sharedCrew.setup_status_not_set_up')}</Badge>
        {FAILED_REASONS.has(status.reason)
          // No hand-off: the Invite dialog holds the alias being typed and any link shown once.
          ? <ErrorNotice variant="inline" message={reasonText(status.reason)} testId="ssm-reason" />
          : <span className="text-[13px] text-muted" data-testid="ssm-reason">{reasonText(status.reason)}</span>}
      </div>
      <GuideLink />
    </div>
  )
}

/** The SSM tab of the Invite dialog. */
export function SsmTab({ status }: { status: SsmStatus }) {
  const qc = useQueryClient()
  const people = useSharedCrewPeople(true, { fetchOnMount: false })
  const { confirmShare, firstShareDialog, shareError } = useFirstShareGate('ssm')
  const [alias, setAlias] = useState('')
  // Every link made while the tab is open stays shown: a link is not shown again.
  const [created, setCreated] = useState<SsmCreated[]>([])
  const create = useMutation({
    mutationFn: () => api.sharedCrewSsmCreateInvite(alias.trim()),
    onSuccess: made => {
      setCreated(list => [made, ...list])
      setAlias('')
      void qc.invalidateQueries({ queryKey: ssmStatusKey })
      void qc.invalidateQueries({ queryKey: sharedCrewAccessKey })
    },
  })
  const submission = useGuardedMutation(create.mutateAsync, confirmShare)
  const owner = ownerDisplayName(people.data?.people ?? [])
  const active = status.state === 'active'
  return (
    <div className="flex flex-col gap-3 min-w-0" data-testid="ssm-tab">
      <StatusLine status={status} />
      {active && (
        <form
          className="flex flex-col gap-3 min-w-0"
          data-testid="ssm-form"
          onSubmit={e => { e.preventDefault(); if (!submission.isPending) void submission.run() }}
        >
          {firstShareDialog}
          <div className="flex flex-col gap-1 min-w-0">
            <label htmlFor="ssm-alias" className="text-[13px] text-text-strong">{i18nT('sharedCrew.invite_alias_label')}</label>
            <Input id="ssm-alias" maxLength={64} value={alias} disabled={submission.isPending} onChange={e => setAlias(e.target.value)} data-testid="ssm-alias" />
          </div>
          <Btn type="submit" primary className="self-start" disabled={submission.isPending} data-testid="ssm-make-link">
            <UserPlus size={14} className="lucide-inline" aria-hidden /> {i18nT('sharedCrew.invite_make_link')}
          </Btn>
          {/* No hand-off: the alias being typed and any link shown once in this tab would be left behind. */}
          {shareError && <ErrorNotice variant="inline" message={inviteErrorText(shareError)} testId="ssm-share-error" />}
          {/* No hand-off: the alias being typed and any link shown once in this tab would be left behind. */}
          {create.isError && <ErrorNotice variant="inline" message={inviteErrorText(inviteErrorCode(create.error) || 'error')} testId="ssm-make-error" />}
        </form>
      )}
      {created.map(made => (
        <div key={made.invite.id} className="flex flex-col gap-2 min-w-0" data-testid="ssm-created">
          <span className="text-[12px] text-muted">{made.invite.alias ? `${made.invite.alias} · ` : ''}{i18nT('sharedCrew.ssm_shown_once')}</span>
          <div className="flex flex-wrap items-center gap-2 min-w-0">
            <code className="text-[12px] break-all min-w-0 flex-1" data-testid="ssm-created-link">{made.url}</code>
            <CopyInviteMessage
              message={buildSsmInviteMessage({ owner, command: made.command, url: made.url })}
              testId="ssm-copy-message"
              onCopied={() => copiedLinks.add(made.invite.id)}
            />
          </div>
        </div>
      ))}
    </div>
  )
}

/** A row under Invites in the People list for an SSM invite nobody has opened: its text dimmed,
 *  with Remove, like the Tailscale invite rows beside it. */
export function SsmInviteRow({ invite }: { invite: SsmInvite }) {
  const name = invite.alias || i18nT('sharedCrew.invite_link_row')
  return (
    <li className="flex flex-wrap items-center gap-3 py-2 min-w-0" data-testid={`ssm-invite-${invite.id}`} data-status="pending">
      <span className="inline-flex items-center justify-center w-8 h-8 rounded-full bg-bg-hover text-text text-[12px] font-semibold shrink-0" aria-hidden>
        {(name.trim()[0] ?? '?').toUpperCase()}
      </span>
      <div className="flex flex-col gap-0.5 min-w-0 flex-1 opacity-60">
        <div className="flex items-center gap-2 min-w-0 flex-wrap">
          <span className="text-sm font-medium text-text-strong truncate max-w-full" title={name}>{name}</span>
        </div>
        <span className="text-[12px] text-muted">
          {invite.created_at > 0 ? i18nT('sharedCrew.invite_created_at', { when: fmtDateTime(invite.created_at * 1000) }) : null}
          {invite.created_at > 0 && !copiedLinks.has(invite.id) ? <span aria-hidden>{'\u00A0·\u00A0'}</span> : null}
          {!copiedLinks.has(invite.id) ? i18nT('sharedCrew.ssm_not_copied') : null}
        </span>
      </div>
      <RevokeControl inviteId={invite.id} name={name} transport="ssm" />
    </li>
  )
}
