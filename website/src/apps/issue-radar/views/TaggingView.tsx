import { Tags } from 'lucide-react'
import ComingSoon from './ComingSoon'

/** Tagging dashboard — bulk-label triage. Accept AI-suggested labels across a
 * queue of issues, computed locally; nothing is written to GitHub until you
 * confirm. Placeholder until the workflow lands; owned independently so it can
 * be built in isolation. Pairs with the per-repo AI label recommendations on
 * the Settings page. */
export default function TaggingView() {
  return (
    <ComingSoon
      icon={Tags}
      title="Tagging"
      blurb="Bulk-label issues without leaving Issue Radar. Accept AI-suggested labels one issue at a time or across a whole batch — computed locally — and nothing is written to GitHub until you confirm each change."
    />
  )
}
