import { Sparkles } from 'lucide-react'
import ComingSoon from './ComingSoon'

/** Ranking dashboard — AI-ordered triage priority queue. Placeholder until the
 * ranking model lands; owned independently so it can be built in isolation. */
export default function RankingView() {
  return (
    <ComingSoon
      icon={Sparkles}
      title="Ranking"
      blurb="A priority-ordered triage queue. Issue Radar will rank open issues by urgency and impact — locally, no cloud AI keys required — so you always know what to look at first."
    />
  )
}
