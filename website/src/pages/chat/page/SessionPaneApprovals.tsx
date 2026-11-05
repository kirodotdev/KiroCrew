/**
 * The native approval strip over an app-drawn session pane.
 *
 * An app pane replaces the transcript and the composer, which is where a slot's
 * tool approvals are answered, so without this the agent would wait on a request
 * nobody can see. Approvals stay host controls (rfc-project-apps §9): the strip
 * draws the composer's own pieces — `ToolApprovalBar` over `useToolApproval`, the
 * spawn card over `useSpawnApprovals`, and `CrewApprovalCards` for the slot's
 * workers — so a decision here takes the same route and gets the same outcome
 * notices as one made in the composer.
 *
 * Bottom-right of the pane, above the app, growing upward, and only while
 * something is waiting. The app keeps the bottom-left for its own dock, at most
 * 420px wide, so the band to its right is the strip's.
 */
import { useAppDispatch, useAppSelector } from '../../../store'
import { useSpawnApprovals, useToolApproval } from '../../../components/chat-input/approval'
import { ToolApprovalBar, ToolApprovalStatusNotice } from '../../../components/chat-input/ToolApprovalBar'
import { SpawnApprovalCard } from '../../../components/chat-input/SpawnApprovalCard'
import CrewApprovalCards from '../../../components/crew/CrewApprovalCards'
import ErrorNotice from '../../../components/ErrorNotice'

export default function SessionPaneApprovals({ slotKey }: { slotKey: string }) {
  const dispatch = useAppDispatch()
  // No approval mode is passed: the pane mounts no mode picker, so the hint that
  // opens one is withheld, as is the nudge toward it.
  const approval = useToolApproval({ slotId: slotKey, slotApprovalChrome: true, dispatch })
  const spawn = useSpawnApprovals({ slotId: slotKey, slotApprovalChrome: true, dispatch, showApprovalError: approval.showApprovalError })
  // The same gate `MateApprovalCards` uses, for the crew tree: a worker of this
  // slot holds a request with an id to answer.
  const workersWaiting = useAppSelector(s => s.dashboard.slots.some(r =>
    r.crew_root === slotKey && !!r.pending_approval && !!r.pending_approval_info?.request_id))
  const { hasApproval, approvalNotice, approvalNoticeKind, setApprovalNotice } = approval
  const spawnWaiting = spawn.pendingSpawnApprovals.length > 0
  if (!hasApproval && !spawnWaiting && !workersWaiting && !approvalNotice && !spawn.spawnLivenessError) return null
  return (
    <div
      data-testid="app-session-approvals"
      className="absolute z-30 flex flex-col justify-end gap-2 pointer-events-none"
      // 420px beside the app's 420px dock plus its gutters; never narrower than a
      // usable card on a narrow pane, where it then overlaps the dock rather than
      // vanishing, and never wider than the pane.
      style={{ right: 16, bottom: 16, width: 'max(260px, min(420px, calc(100% - 460px)))', maxWidth: 'calc(100% - 32px)' }}
    >
      {workersWaiting && <div className="pointer-events-auto"><CrewApprovalCards root={slotKey} /></div>}
      {spawnWaiting && (
        <div className="pointer-events-auto rounded-2xl border border-border bg-bg-elevated glass-shadow overflow-hidden">
          <SpawnApprovalCard {...spawn} hasApproval={hasApproval} />
        </div>
      )}
      {hasApproval && (
        <div className="pointer-events-auto rounded-2xl border border-border bg-bg-elevated glass-shadow approval-glow overflow-hidden" data-testid="app-session-tool-approval">
          <ToolApprovalBar approval={approval} />
        </div>
      )}
      {approvalNotice && approvalNoticeKind === 'error' && (
        <div className="pointer-events-auto">
          {/* A failed decision with no draft beside it, so the hand-off is on. */}
          <ErrorNotice askAgent testId="app-session-approval-error" message={approvalNotice} onDismiss={() => setApprovalNotice(null)} />
        </div>
      )}
      {spawn.spawnLivenessError && (
        <div className="pointer-events-auto">
          <ErrorNotice askAgent testId="app-session-spawn-liveness-error" message={spawn.spawnLivenessError} onDismiss={spawn.dismissSpawnLivenessError} />
        </div>
      )}
      {approvalNotice && approvalNoticeKind !== 'error' && (
        <div className="pointer-events-auto bg-bg-elevated rounded-lg">
          <ToolApprovalStatusNotice approval={approval} />
        </div>
      )}
    </div>
  )
}
