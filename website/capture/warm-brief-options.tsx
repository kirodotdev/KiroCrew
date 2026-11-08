/**
 * Isolated capture entry: the owner's option buttons under a warm welcome.
 *
 * The REAL WarmBriefCard and the REAL FollowUpBar mount in one frame, fed by the
 * REAL `deriveFollowUpOptions`. The brief row carries the `[OPTIONS:]` line the
 * gateway copies (`_pending_options`); `fix=off` is the brief row as main posts
 * it, with no copied line.
 * Query string: ?fix=on|off&theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import type { ChatMessage } from '../src/types'
import { initI18n } from '../src/i18n'
import FollowUpBar from '../src/components/FollowUpBar'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import WarmBriefCard, { warmBriefOf } from '../src/components/chat/WarmBriefCard'
import { deriveFollowUpOptions, parseOptions } from '../src/app-sdk/protocol'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const fixOn = params.get('fix') !== 'off'
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const ASK: ChatMessage = {
  role: 'assistant',
  cls: 'msg msg-a',
  content: 'PR #101 is green. How do you want to land it? [OPTIONS: Merge it | Rebase first | Hold for review]',
}
const BRIEF: ChatMessage = {
  role: 'assistant',
  cls: 'msg msg-a',
  content: 'Welcome back.' + (fixOn ? '\n\n[OPTIONS: Merge it | Rebase first | Hold for review]' : ''),
  meta: {
    kind: 'warm_task_brief_result',
    brief: { status: 'needs_you', headline: 'PR #101 waits on your merge choice.', body: 'CI is green on the last head. Nothing else changed while you were away.' },
  },
}
const messages = [ASK, BRIEF]
const { followUpOptions } = deriveFollowUpOptions(messages, false)
const brief = warmBriefOf(BRIEF)

function Scene() {
  return (
    <div className="bg-bg text-text flex flex-col justify-end min-h-screen" data-scene>
      <div className="px-4 pt-4 mx-auto w-full flex flex-col gap-3" style={{ maxWidth: 720 }}>
        <div className="max-w-[80%] text-sm"><MarkdownRenderer content={parseOptions(ASK.content).text} /></div>
        <div className="max-w-[80%]">{brief && <WarmBriefCard brief={brief} />}</div>
      </div>
      <div className="input-area px-4 pb-1 pt-3 mx-auto w-full flex flex-col" style={{ maxWidth: 720 }}>
        <div data-bar>
          {followUpOptions.length > 0 && (
            <FollowUpBar options={[...followUpOptions]} picked={new Set()} onSelect={() => {}} onSend={() => {}} layout="multiline" />
          )}
        </div>
        <div className="mt-1 rounded-2xl border border-border bg-bg-elevated px-3 py-3 text-[13px] text-muted">
          Message Kiro Crew…
        </div>
      </div>
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Scene />)
