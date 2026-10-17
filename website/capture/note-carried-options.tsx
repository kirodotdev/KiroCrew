/**
 * Isolated capture + measurement entry for note-carried `[OPTIONS:]` (#5737).
 *
 * WHY ISOLATED: the two halves only make sense together — the bubble must lose
 * the marker at the moment the pill row gains it. Either alone proves nothing: a
 * stripped bubble with no pills has deleted the choices, and pills under a bubble
 * still printing `[OPTIONS: Fix | Skip]` is the duplicate render that was
 * flagged. So the REAL `inject` renderer and the REAL FollowUpBar mount in one
 * frame, fed by the REAL `deriveFollowUpOptions`.
 *
 * `fix=off` restores the two pre-fix expressions VERBATIM (see BEFORE_* below) so
 * the before arm is asserted to reproduce rather than assumed. `scene=cron`
 * covers the second finding: a non-note `inject` row must gain no pills of its own.
 * Query string: ?scene=note|cron&fix=on|off&theme=dark
 */
import { createRoot } from 'react-dom/client'
import type { ChatMessage } from '../src/types'
import { initI18n } from '../src/i18n'
import FollowUpBar from '../src/components/FollowUpBar'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import UserMessage from '../src/pages/chat/UserMessage'
import { defaultMessageRenderers, resolveRenderer } from '../src/app-sdk/messageRenderers'
import { deriveFollowUpOptions, parseOptions } from '../src/app-sdk/protocol'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = (params.get('scene') || 'note') as 'note' | 'cron'
const theme = params.get('theme') || 'dark'
const fixOn = params.get('fix') !== 'off'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const NOTE_CLS = 'reconcile-note'
const MARKER = '[OPTIONS: Fix | Skip]'

/** The note a zero-token triage cron writes — the feature's actual use case. */
const NOTE: ChatMessage = {
  role: 'inject',
  cls: NOTE_CLS,
  content: `Triage complete: 3 peer reviews scanned, 1 needs a reply. ${MARKER}`,
}

/** A cron prompt that merely CONTAINS a marker. Not a note: no note token in cls. */
const CRON: ChatMessage = {
  role: 'inject',
  cls: 'msg msg-u',
  content: `Reminder: ask the human whether to retry the failed shard. ${MARKER}`,
}

const row = scene === 'cron' ? CRON : NOTE

/**
 * The pre-fix renderer body, verbatim: `cleanContent` went straight to
 * MarkdownRenderer with no `parseOptions`, so the marker printed as prose.
 */
function BeforeBubble({ m }: { m: ChatMessage }) {
  return (
    <div className="msg-content px-4 py-3 text-sm leading-6 whitespace-pre-wrap rounded-lg bg-warning-subtle text-fg ring-1 ring-inset ring-warning/30 rounded-bl-[4px] overflow-hidden min-w-0">
      <MarkdownRenderer content={m.content} />
    </div>
  )
}

/** The pre-fix gate, verbatim: the bare `inject` role, with no `cls` test. */
function beforeOptions(m: ChatMessage): string[] {
  if (m.role === 'inject' && m.content) return parseOptions(m.content).options
  return []
}

const messages: ChatMessage[] = [
  { role: 'user', content: 'Keep an eye on the review queue and ping me if anything needs a reply.', cls: 'msg msg-u' },
  row,
]

const options = fixOn ? deriveFollowUpOptions(messages, false).followUpOptions : beforeOptions(row)

function InjectRow({ m }: { m: ChatMessage }) {
  const entry = resolveRenderer(m, defaultMessageRenderers)
  if (!entry) return null
  return <>{entry.render(m, {
    index: 1,
    messages,
    running: false,
    key: 'capture-row',
    hideCardOwnedOAuth: false,
    autoDeniedIds: new Set<string>(),
    wrapper: children => <div className="flex flex-col items-start max-w-[80%]" data-bubble>{children}</div>,
    row: children => <div>{children}</div>,
  })}</>
}

function Scene() {
  return (
    <div className="bg-bg text-text flex flex-col justify-end min-h-screen">
      <div className="px-4 pt-4 mx-auto w-full flex flex-col gap-3" style={{ maxWidth: 900 }}>
        <div className="self-end max-w-[80%]">
          <UserMessage content={messages[0].content} renderContent={c => <MarkdownRenderer content={c} />} />
        </div>
        {fixOn
          ? <InjectRow m={row} />
          : <div className="flex flex-col items-start max-w-[80%]" data-bubble><BeforeBubble m={row} /></div>}
      </div>
      <div className="input-area px-4 pb-1 pt-3 mx-auto w-full flex flex-col" style={{ maxWidth: 900 }}>
        <div data-bar>
          {options.length > 0 && (
            <FollowUpBar options={options} picked={new Set()} onSelect={() => {}} onSend={() => {}} layout="multiline" />
          )}
        </div>
        <div className="mt-1 rounded-2xl border border-border bg-bg-elevated px-3 py-3 text-[13px] text-muted">
          Message Kiro Crew… (/command · @file · $skill)
        </div>
      </div>
    </div>
  )
}

interface NoteMeasure {
  scene: string
  fix: string
  /** Text the note bubble actually rendered. The marker must be absent post-fix. */
  bubbleText: string
  markerVisibleInBubble: boolean
  /** Labels the pill row drew, read from the DOM rather than from the input. */
  pills: string[]
}

declare global {
  interface Window {
    __measure: () => NoteMeasure
  }
}

window.__measure = () => {
  const bubble = document.querySelector<HTMLElement>('[data-bubble]')
  const bubbleText = (bubble?.textContent ?? '').trim()
  const chips = Array.from(document.querySelectorAll<HTMLElement>('span.followup-chip'))
  return {
    scene,
    fix: fixOn ? 'on' : 'off',
    bubbleText,
    markerVisibleInBubble: bubbleText.includes('[OPTIONS:'),
    pills: chips.map(c => (c.textContent ?? '').trim()).filter(Boolean),
  }
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Scene />)
