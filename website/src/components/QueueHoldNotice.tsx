import { Hourglass } from 'lucide-react'
import NoticeCard from '../pages/chat/NoticeCard'

export default function QueueHoldNotice({ message }: { message: string | null }) {
  if (!message) return null
  return (
    <div role="status" data-testid="queue-interrupt-notice" className="relative z-[2] mx-auto w-full px-4 pb-1" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <NoticeCard content={message} tone="info" icon={Hourglass} />
    </div>
  )
}
