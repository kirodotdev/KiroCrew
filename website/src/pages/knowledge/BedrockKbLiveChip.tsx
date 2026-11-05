import { useEffect, useRef, useState } from 'react'
import { hashKey, notifyManager, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type AwsConsentStatus } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import type { Source } from './types'

/** The (profile, region) a saved bedrock_kb row retains in its properties. */
export function bedrockKbTarget(s: Source): { profile: string; region: string } | null {
  const raw = s.properties
  let props: Record<string, unknown> = {}
  if (typeof raw === 'string') {
    try { props = JSON.parse(raw) ?? {} } catch { props = {} }
  } else if (raw && typeof raw === 'object') {
    props = raw
  }
  const region = typeof props.region === 'string' ? props.region.trim() : ''
  if (!region) return null
  const profile = typeof props.profile === 'string' ? props.profile.trim() : ''
  return { profile, region }
}

/**
 * The meta chip of a Bedrock KB row. "Live" shows only while the consent
 * query the add form polls reports the row's (profile, region) as granted.
 * When that query answers anything else, the chip is a "Not enabled · Review
 * AWS account" button that opens the consent card for that target, because every retrieval is then
 * refused server-side with only a log line.
 *
 * A granted row the gateway reports as not searchable (added under another
 * account) says so in visible text and points at the row's remove button, since a new confirmation
 * cannot bring it back.
 *
 * The query key is the add form's and AwsConsentGate's own, so a grant or
 * revoke made in the card invalidates this row too. While the answer is
 * loading, or when the read itself fails, the chip keeps saying "Live": an
 * unanswered read says nothing about the grant, and a granted user must not
 * see a revoked flash on every page load. A failed read also shows a notice
 * that the grant could not be checked.
 */
export default function BedrockKbLiveChip({ source, onReviewConsent }: {
  source: Source
  onReviewConsent: (target: { profile: string; region: string }) => void
}) {
  const target = bedrockKbTarget(source)
  const consentQ = useQuery<AwsConsentStatus>({
    queryKey: ['awsConsent', 'bedrock-kb', target?.profile ?? '', target?.region ?? ''],
    queryFn: () => api.awsConsent('bedrock-kb', target!),
    enabled: target !== null,
    // The global staleTime is Infinity; a grant can also end outside this
    // page (Settings, or the account behind the profile changing), so a
    // remount after a minute reads it again.
    staleTime: 60_000,
  })
  // `searchable` comes from the sources list, computed against the grant at
  // that fetch. On this row's first consent answer, and whenever a later one
  // names a different grant (confirmed or withdrawn anywhere), the list is
  // read again, and `searchable` is trusted only from a read that landed
  // after that moment. A background refetch with an unchanged grant keeps the
  // answer on screen; a failed re-read says the check could not be made.
  const queryClient = useQueryClient()
  // Landed `/sources` reads, counted from the query cache's own events:
  // only a fetch's result counts, never a `setQueryData` write (an
  // optimistic removal and its rollback are `manual` updates), and a read
  // cancelled by a newer one dispatches no result at all. Counters, not
  // clock times: two updates can share a millisecond.
  // The ref is updated synchronously by the cache subscriber, so a snapshot
  // taken in an effect never misses a read that landed just before it; the
  // state copy only re-renders the chip.
  const readsRef = useRef({ ok: 0, failed: 0 })
  const [reads, setReads] = useState({ ok: 0, failed: 0 })
  useEffect(() => queryClient.getQueryCache().subscribe(event => {
    if (event.type !== 'updated' || event.query.queryHash !== hashKey(['knowledge-sources'])) return
    const action = event.action as { type: string; manual?: boolean }
    const r = readsRef.current
    if (action.type === 'success' && !action.manual) readsRef.current = { ...r, ok: r.ok + 1 }
    else if (action.type === 'error') readsRef.current = { ...r, failed: r.failed + 1 }
    else return
    // Through React Query's own batch, after the observers that hand the
    // parent its new rows: a re-render on the count alone would pair the
    // fresh count with the previous row.
    const next = readsRef.current
    notifyManager.schedule(() => setReads(next))
  }), [queryClient])
  const grantKey = consentQ.isSuccess
    ? `${consentQ.data?.granted === true}|${consentQ.data?.grant?.account ?? ''}|${consentQ.data?.grant?.granted_at ?? ''}`
    : null
  // The read counters when the current grant was first seen. The
  // invalidation right after the snapshot cancels any read already in
  // flight, so only a read started for this grant can move them.
  const [asked, setAsked] = useState<{ key: string; ok: number; failed: number } | null>(null)
  useEffect(() => {
    if (grantKey === null || asked?.key === grantKey) return
    setAsked({ key: grantKey, ok: readsRef.current.ok, failed: readsRef.current.failed })
    queryClient.invalidateQueries({ queryKey: ['knowledge-sources'] }).catch(() => undefined)
  }, [grantKey, asked, queryClient])
  const askedNow = asked !== null && asked.key === grantKey
  const listFresh = askedNow && reads.ok > asked.ok
  const listFailed = askedNow && !listFresh && reads.failed > asked.failed
  const revoked = target !== null && consentQ.isSuccess && consentQ.data?.granted !== true
  // Granted, but the gateway says no search queries this row: it was added
  // under an account other than the one the current grant confirms (the
  // profile now resolves to another one). Confirming again cannot fix that,
  // so the chip names the recovery that does: remove the source and add it
  // again.
  const needsReadd = target !== null && consentQ.isSuccess && consentQ.data?.granted === true
    && source.searchable === false && source.searchable_reason === 'other_account' && listFresh
  if (needsReadd) {
    return (
      /* Visible text, not a hover title, and not styled like the clickable
         "Not enabled" chip: it says why the row is dark and which control
         fixes it (the row's own remove button). */
      <span className="text-warn min-w-0" data-testid="bedrock-kb-needs-readd">
        {i18nT('pages.knowledge.sourcesList.needs_readd')}
      </span>
    )
  }
  if (revoked) {
    return (
      <button type="button" onClick={() => onReviewConsent(target)}
        title={i18nT('pages.knowledge.sourcesList.grant_revoked_hint')}
        data-testid="bedrock-kb-grant-revoked"
        className="text-warn shrink-0 underline decoration-dotted underline-offset-2 hover:decoration-solid">
        {i18nT('pages.knowledge.sourcesList.grant_revoked')}
      </button>
    )
  }
  const live = <span className="text-muted shrink-0">{/* No dot: a grey dot read as a status ("off"), and "Live" is a kind, not a state. */}{i18nT('pages.knowledge.sourcesList.live_query')}</span>
  if (target === null || !(consentQ.isError || (consentQ.data?.granted === true && listFailed))) return live
  // The failed read is said, not swallowed: the chip keeps its kind label,
  // and the notice beside it says the grant could not be checked.
  return (
    <>
      {live}
      {/* No hand-off: the Sources page may hold an unsaved add-source draft
          (KB ids, region, profile) that navigating to chat would discard. */}
      <ErrorNotice
        variant="inline"
        showIcon={false}
        className="min-w-0"
        testId="bedrock-kb-grant-check-error"
        message={i18nT('pages.knowledge.sourcesList.grant_check_failed')}
      />
    </>
  )
}
