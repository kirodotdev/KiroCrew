import { i18nT } from '../../i18n/t'
import { lazyRetry } from '../lazyRetry'
import type { WaitingOnYouStackProps } from './WaitingOnYouStack'

// A failed load shows its notice above the message box instead of throwing to the
// chat route's error boundary. No hand-off: the composer below may hold a draft.
const WaitingOnYouStack = lazyRetry(() => import('./WaitingOnYouStack'), {
  layout: 'block',
  title: () => i18nT('components.errorBoundary.something_went_wrong'),
  scope: 'waiting-on-you-stack',
  askAgent: false,
})

export default function LazyWaitingOnYouStack(props: WaitingOnYouStackProps) {
  return <WaitingOnYouStack {...props} />
}
