import { BarChart3 } from 'lucide-react'
import ComingSoon from './ComingSoon'

/** Insights dashboard — trends over time (open/close velocity, stale issues,
 * response time, label distribution). Placeholder; owned independently. */
export default function InsightsView() {
  return (
    <ComingSoon
      icon={BarChart3}
      title="Insights"
      blurb="Trends over time: open vs. closed velocity, stale-issue backlog, first-response time, and how your label mix shifts week over week."
    />
  )
}
