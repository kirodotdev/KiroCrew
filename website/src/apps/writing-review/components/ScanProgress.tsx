// Polls GET /jobs/{jobId} while a scan is running, then hands control
// back to the workspace by clearing activeJobId and selecting the
// newly-persisted review.
import { Loader2 } from 'lucide-react'
import { useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { i18nT } from '../../../i18n/t'
import { useWritingReview } from '../context'
import { writingReviewApi } from '../api'
import type { JobStatus } from '../lib/types'
import { phaseLabel } from '../lib/phaseLabels'

const POLL_INTERVAL_MS = 2000

export default function ScanProgress() {
  const {
    activeJobId,
    setActiveJobId,
    setActiveJobDocName,
    setActiveJobPhase,
    selectReview,
  } = useWritingReview()
  const queryClient = useQueryClient()

  const jobQuery = useQuery<JobStatus>({
    queryKey: ['writing-review', 'job', activeJobId],
    queryFn: () => writingReviewApi.getJob(activeJobId as string),
    enabled: activeJobId !== null,
    refetchInterval: activeJobId === null ? false : POLL_INTERVAL_MS,
  })

  useEffect(() => {
    if (!jobQuery.data) return
    // Publish the live phase to context so the sidebar in-progress card
    // renders the same friendly label as this pane. Cleared when we
    // clear activeJobId below.
    setActiveJobPhase(jobQuery.data.phase ?? null)
    // Clear the in-flight state whenever the job reaches a terminal
    // state (done / failed / interrupted). Missing review_id on 'done'
    // still terminates the poll -- the review might have failed to
    // persist, but leaving the sidebar+main pane stuck on Scanning
    // is a worse UX than the missing detail. If review_id is present,
    // also select it so the detail pane opens automatically.
    const jobStatus = jobQuery.data.status
    if (jobStatus === 'done') {
      if (jobQuery.data.review_id) {
        selectReview(jobQuery.data.review_id)
      }
      setActiveJobId(null)
      setActiveJobDocName(null)
      setActiveJobPhase(null)
      void queryClient.invalidateQueries({ queryKey: ['writing-review', 'reviews'] })
      return
    }
    if (jobStatus === 'failed' || jobStatus === 'interrupted') {
      setActiveJobId(null)
      setActiveJobDocName(null)
      setActiveJobPhase(null)
    }
  }, [
    jobQuery.data,
    selectReview,
    setActiveJobId,
    setActiveJobDocName,
    setActiveJobPhase,
    queryClient,
  ])

  return (
    <div className="flex-1 min-h-0 flex flex-col items-center justify-center gap-3 px-6 text-center">
      <Loader2 className="lucide-inline text-accent animate-spin" aria-hidden="true" style={{ fontSize: '28px' }} />
      <div className="text-[13px] text-text">
        {i18nT('apps.writingReview.scanProgress.title')}
      </div>
      <div className="text-[11.5px] text-muted">
        {phaseLabel(jobQuery.data?.phase)}
      </div>
      {jobQuery.data?.status === 'failed' && (
        <div className="text-[12px] text-danger">
          {i18nT('apps.writingReview.scanProgress.failed')}
        </div>
      )}
    </div>
  )
}
