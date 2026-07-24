import { CopyCheck } from 'lucide-react'
import ComingSoon from './ComingSoon'

/** Duplicates dashboard — clusters of likely-duplicate issues (the core local
 * dedup differentiator). Placeholder; owned independently. */
export default function DuplicatesView() {
  return (
    <ComingSoon
      icon={CopyCheck}
      title="Duplicates"
      blurb="Clusters of issues that look like duplicates, detected locally on your machine. Review a cluster, pick the canonical issue, and close the rest — you confirm before anything is written back to GitHub."
    />
  )
}
