/**
 * The pending reply bubble while the gateway prepares the warm welcome
 * (`PreparingReplyBubbleView`). Loaded on its own, outside the app's first chunk.
 * Both chat surfaces mount it with the pane, so the code is in before any
 * `preparing` frame arrives.
 */
import { lazy, Suspense } from 'react'

const PreparingReplyBubbleView = lazy(() => import('./PreparingReplyBubbleView'))

export default function PreparingReplyBubble(props: { caption: string; shown: boolean; testId: string }) {
  return (
    <Suspense fallback={null}>
      <PreparingReplyBubbleView {...props} />
    </Suspense>
  )
}
