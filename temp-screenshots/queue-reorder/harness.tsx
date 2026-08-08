import React from 'react'
import { createRoot } from 'react-dom/client'
import QueueStack from '../../src/components/QueueStack'
import type { ChatMessage } from '../../src/types'
import '../../src/index.css'

/**
 * Ephemeral screenshot harness for the QueueStack reorder PR - renders the
 * real component with the real stylesheet, no backend. Not part of the build:
 * lives under temp-screenshots/ (never packaged) and is only referenced by
 * the capture script.
 *
 * Query params:
 *   ?state=collapsed | expanded
 *   &theme=dark | light
 */
const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme)
document.body.style.background = 'var(--bg)'

const msgs: ChatMessage[] = [
  { role: 'queued', content: 'Fix the failing build on the auth branch first', cls: 'msg msg-queued', meta: { queueId: 'q1' } },
  { role: 'queued', content: 'Then rerun the integration suite and post results', cls: 'msg msg-queued', meta: { queueId: 'q2' } },
  { role: 'queued', content: 'Draft the release notes for 0.3.0', cls: 'msg msg-queued', meta: { queueId: 'q3' } },
] as ChatMessage[]

function Harness() {
  return (
    <div style={{ paddingTop: 240, maxWidth: 900, margin: '0 auto' }}>
      <QueueStack
        messages={msgs}
        onCancel={() => {}}
        onInterrupt={() => {}}
        onEdit={() => {}}
        onReorder={() => {}}
        fuseBelow={false}
      />
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Harness />)
