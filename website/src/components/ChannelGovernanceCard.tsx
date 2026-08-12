import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Landmark, Loader2 } from 'lucide-react'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { connectionForTransport, useConnections } from '../hooks/useConnections'

/**
 * What governs this channel's connection — and the control that changes it.
 *
 * Placed ABOVE the channel's configuration because it governs the fields below:
 * when the sender list is pinned by policy the input is not editable, and an
 * operator who discovers that after typing into the form was misled by the form.
 *
 * The enrol control lives HERE rather than on a separate admin screen because
 * this is where an operator already comes to make a bot work. A connection that
 * is credentialed but not enrolled silently fails to attach, so the page that
 * shows the credentials has to be the page that says "and it is not allowed in
 * yet", with the way to allow it.
 *
 * Enrolment writes the keystone trust roster through the dashboard, the same
 * shape Settings > Security uses for `denied_commands.json`: the AGENT cannot
 * touch either file, while the operator edits both from here.
 */
export default function ChannelGovernanceCard({ transport }: { transport: string }) {
  const { data, isError } = useConnections()
  const queryClient = useQueryClient()
  // Reported IN the card rather than the notification centre, so this stays a
  // presentational component: the channel panel has never needed a Redux store,
  // and a failed enrol is most useful next to the button that failed.
  const [actionError, setActionError] = useState('')
  const connection = connectionForTransport(data, transport)

  const failed = (e: unknown) => setActionError(
    i18nT('components.channelGovernanceCard.action_failed', {
      reason: e instanceof Error && e.message ? e.message : 'unknown error',
    }),
  )

  const enrol = useMutation({
    mutationFn: (id: string) => api.enrolConnection(id),
    onSuccess: (payload) => {
      setActionError('')
      // The response IS the refreshed read model, so the card's own headline
      // flips without a second round trip — no success toast needed to tell the
      // operator it worked.
      queryClient.setQueryData(['connections'], payload)
    },
    onError: failed,
  })

  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeConnection(id),
    onSuccess: (payload) => {
      setActionError('')
      queryClient.setQueryData(['connections'], payload)
    },
    onError: failed,
  })

  if (isError || !data || !connection) return null

  const rosterLoaded = data.roster.loaded
  const busy = enrol.isPending || revoke.isPending

  // A roster that could not be READ is not an operator decision, and it must not
  // offer a write either: this API refuses to rewrite a corrupt roster rather than
  // discard whatever is in it, so the card explains instead of offering a button
  // that would 409.
  const headline = !rosterLoaded
    ? i18nT('components.channelGovernanceCard.roster_unreadable', { path: data.roster.path })
    : !connection.enrolled
      ? i18nT('components.channelGovernanceCard.not_enrolled_actionable')
      : connection.permitted === false
        ? i18nT('components.channelGovernanceCard.denied_by_policy')
        : connection.senders_pinned
          ? i18nT('components.channelGovernanceCard.senders_pinned')
          : i18nT('components.channelGovernanceCard.enrolled_ok')

  const revokeConfirm = () => {
    // Revoking cuts a live inbound path, and it bites on the connection's NEXT
    // message rather than at some later restart — worth one confirm, matching the
    // precedent for destructive actions elsewhere in Settings.
    if (!window.confirm(i18nT('components.channelGovernanceCard.confirm_revoke', { id: connection.id }))) return
    revoke.mutate(connection.id)
  }

  return (
    <div className="rounded-lg border border-border bg-card p-3.5 mb-4">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 shrink-0 w-7 h-7 rounded-md bg-accent-subtle flex items-center justify-center text-accent">
          <Landmark size={14} className="lucide-inline" aria-hidden />
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-semibold text-text-strong">
            {i18nT('components.channelGovernanceCard.title')}
          </div>
          <div className="mt-0.5 text-[12px] text-muted leading-relaxed">{headline}</div>
          <div className="mt-1.5 flex items-center gap-2 flex-wrap">
            <span className="font-mono text-[11px] text-muted">{connection.id}</span>
            {rosterLoaded && (
              connection.enrolled ? (
                <button
                  type="button"
                  onClick={revokeConfirm}
                  disabled={busy}
                  className="inline-flex items-center gap-1 rounded-md border border-border bg-transparent px-2 py-0.5 text-[11.5px] text-danger cursor-pointer hover:bg-danger-subtle transition-colors disabled:opacity-50"
                >
                  {busy && <Loader2 size={11} className="animate-spin" aria-hidden />}
                  {i18nT('components.channelGovernanceCard.revoke')}
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => enrol.mutate(connection.id)}
                  disabled={busy}
                  className="inline-flex items-center gap-1 rounded-md border border-accent bg-accent-subtle px-2 py-0.5 text-[11.5px] text-accent cursor-pointer hover:bg-bg-hover transition-colors disabled:opacity-50"
                >
                  {busy && <Loader2 size={11} className="animate-spin" aria-hidden />}
                  {i18nT('components.channelGovernanceCard.enrol')}
                </button>
              )
            )}
          </div>
          {actionError && (
            <div role="alert" className="mt-1.5 text-[11.5px] text-danger leading-relaxed">
              {actionError}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
