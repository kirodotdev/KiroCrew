/**
 * Gap 9: a nested card above the composer for every KAS workflow approval held
 * against THIS chat (a step tool approval, or a plan approval). Without it a
 * held step approval lives only under the run card, which scrolls out of view —
 * so once it is gone there is nothing near the composer to answer.
 *
 * The card itself is the SAME `KasApprovalCard` the run views draw (one source
 * of truth for how an approval renders and answers); this only sources the
 * approvals for the slot and places them in the composer stack.
 */
import { KasApprovalCard } from '../../apps/workflows/kas/KasRunView'
import { useKasComposerApprovals } from '../../apps/workflows/kas/useKasWorkflows'

export function KasComposerApprovals({ slotId }: { slotId: string | null }) {
  const { approvals, answer } = useKasComposerApprovals(slotId)
  if (!slotId || approvals.length === 0) return null
  return (
    <div className="px-4 mb-1 flex flex-col gap-2" data-testid="kas-composer-approvals">
      {approvals.map(approval => (
        <KasApprovalCard
          key={approval.key}
          approval={approval}
          pending={answer.isPending && answer.variables?.key === approval.key}
          onAnswer={optionId => answer.mutate({ key: approval.key, optionId })}
        />
      ))}
    </div>
  )
}
