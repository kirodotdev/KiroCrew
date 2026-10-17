import { useCallback, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import StatusNotice from '../../components/StatusNotice'
import { api } from '../../api/client'
import { apiErrorCode } from '../../api/apiError'

/** How a staged page stopped being staged, as the host tells the person. */
export type PreviewOutcome = 'kept' | 'discarded' | 'closed'

/** Why the last Keep or Discard did not go through. */
type Refusal = 'locked' | 'changed' | 'keep_failed' | 'discard_failed'

/**
 * The person's Keep and Discard for a crewmate's STAGED dashboard.
 *
 * Drawn by the HOST, above the sandboxed frame and outside it, so the page being
 * judged has no way to press them. Both name the staged page the frame has mounted
 * and drawn (`stagedMs`), so the gateway acts on that page or refuses
 * `preview_changed` -- never on one staged again after the look. A locked layout is
 * the store's own refusal, said in the tab's badge words.
 */
export default function DashboardPreviewActions({ slug, member, stagedMs, onDone }: {
  slug: string
  member: string
  /** The staged page the frame has drawn; 0 until it has. */
  stagedMs: number
  /** The staged page is no longer staged: hand over to the live page. */
  onDone: (outcome: PreviewOutcome) => void
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [refusal, setRefusal] = useState<Refusal | null>(null)
  // A newer page was staged: draw it, so the next click names that one.
  const redraw = () => queryClient.invalidateQueries({ queryKey: ['member-dashboard', slug] })
  const keep = useMutation({
    mutationFn: () => api.memberDashboardPreviewApply(slug, member, stagedMs),
    onMutate: () => setRefusal(null),
    onSuccess: () => onDone('kept'),
    onError: (e) => {
      const code = apiErrorCode(e)
      if (code === 'no_preview') onDone('closed')
      else if (code === 'dashboard_locked') setRefusal('locked')
      else if (code === 'preview_changed') {
        setRefusal('changed')
        void redraw()
      } else setRefusal('keep_failed')
    },
  })
  const discard = useMutation({
    mutationFn: () => api.memberDashboardPreviewDiscard(slug, member, stagedMs),
    onMutate: () => setRefusal(null),
    onSuccess: () => onDone('discarded'),
    onError: (e) => {
      if (apiErrorCode(e) === 'preview_changed') {
        setRefusal('changed')
        void redraw()
      } else setRefusal('discard_failed')
    },
  })
  const busy = keep.isPending || discard.isPending
  return (
    <div className="flex flex-col gap-1.5 px-3 py-2 border-b border-border" data-testid="dashboard-preview-actions">
      {/* The two choices, alone on their row; every line about them sits below. */}
      <div className="flex items-center gap-2">
        <Btn type="button" primary disabled={busy || stagedMs <= 0} onClick={() => keep.mutate()} data-testid="dashboard-preview-keep">
          {t('pages.chat.dashboardPreviewPanel.keep')}
        </Btn>
        <Btn type="button" disabled={busy || stagedMs <= 0} onClick={() => discard.mutate()} data-testid="dashboard-preview-discard">
          {t('pages.chat.dashboardPreviewPanel.discard')}
        </Btn>
      </div>
      {/* Said beside the choice it is about, not at the end of the hint. */}
      <p className="text-[13px] text-muted" data-testid="dashboard-preview-discard-note">
        {t('pages.chat.dashboardPreviewPanel.discard_note')}
      </p>
      {/* A newer staged page is the safety gate working, not a failure: nothing was
          written, the live page is unchanged, the newer page is already drawn below
          and the next step is the ordinary Keep. That is `StatusNotice`'s own case
          ("nothing failed and nothing was lost"). A request that really failed --
          the lock refusal included -- stays an ErrorNotice, with no agent hand-off:
          that navigates away from the Members page's unsaved drafts, and pressing
          the button again is the retry. */}
      {refusal === 'changed' && (
        <StatusNotice tone="warn" message={t('pages.chat.dashboardPreviewPanel.changed')} testId="dashboard-preview-changed" />
      )}
      {refusal && refusal !== 'changed' && (
        <ErrorNotice
          variant="inline"
          message={refusal === 'locked'
            ? t('pages.membersPage.dashboard_locked_badge')
            : refusal === 'keep_failed'
              ? t('pages.chat.dashboardPreviewPanel.keep_failed')
              : t('pages.chat.dashboardPreviewPanel.discard_failed')}
          testId="dashboard-preview-problem"
        />
      )}
    </div>
  )
}

/** The one line a host shows on the live page after a preview ended. Only the
 *  person's own Keep is known to have changed the page, so only it carries the
 *  undo half. */
export function DashboardPreviewOutcomeNote({ outcome, name }: { outcome: PreviewOutcome; name: string }) {
  const { t } = useTranslation()
  return (
    <p role="status" className="px-3 py-2 text-[13px] text-muted border-b border-border" data-testid="dashboard-preview-outcome" data-outcome={outcome}>
      {outcome === 'kept'
        ? <>{t('pages.chat.dashboardPreviewPanel.outcome_kept')} {t('pages.chat.dashboardPreviewPanel.outcome_undo', { name })}</>
        : outcome === 'discarded'
          ? t('pages.chat.dashboardPreviewPanel.outcome_discarded')
          : t('pages.chat.dashboardPreviewPanel.outcome_closed')}
    </p>
  )
}

/**
 * The hand-over from a staged page to the live one, shared by both hosts.
 *
 * `leave` drops the cached LIVE reads under *queryKey*, so the live frame that
 * mounts next reads the page that is live NOW rather than a copy cached before
 * the keep. Then the host stops asking for the preview (`onLeave`), so no
 * "closed" screen is drawn in between.
 */
export function usePreviewHandover(queryKey: readonly string[], onLeave?: () => void) {
  const queryClient = useQueryClient()
  const [outcome, setOutcome] = useState<PreviewOutcome | null>(null)
  const keyText = JSON.stringify(queryKey)
  const leave = useCallback((next: PreviewOutcome) => {
    const key = JSON.parse(keyText) as string[]
    // The staged read is left alone: it is being unmounted, and refetching it would
    // only read the 404 this hand-over exists to keep off the screen.
    const live = (q: { queryKey: readonly unknown[] }) => q.queryKey[q.queryKey.length - 1] !== 'preview'
    // A cached live copy is from before the keep; dropping it makes the frame that
    // mounts next read the page that is live now. One still on screen elsewhere refetches.
    queryClient.removeQueries({ queryKey: key, type: 'inactive', predicate: live })
    void queryClient.invalidateQueries({ queryKey: key, predicate: live })
    setOutcome(next)
    onLeave?.()
  }, [queryClient, keyText, onLeave])
  const reset = useCallback(() => setOutcome(null), [])
  return { outcome, leave, reset }
}
