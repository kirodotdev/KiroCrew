/**
 * Isolated capture + measurement entry for a note's `/chat?sid=` session link.
 *
 * WHY THE SCAFFOLDING: the fix changes an anchor's ATTRIBUTES, and `target` /
 * `title` do not paint — at rest the two arms are pixel-identical. So the fixture
 * draws the two things that make the delta legible, both read out of the live DOM
 * rather than written by hand: the anchor's attributes, and the session the reader
 * is on. The bubble markup is copied verbatim from the transcript's `inject`
 * branch with the REAL `MarkdownRenderer` inside it, so the anchor is production's.
 *
 * `fix=off` restores the pre-fix expression VERBATIM (`content` as the only prop),
 * so the before arm is asserted to reproduce rather than assumed. `scene=self` is
 * the negative control: a link naming the session already open must stay inert.
 * Query string: ?scene=other|self&fix=on|off&theme=dark
 */
import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { initI18n } from '../src/i18n'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import UserMessage from '../src/pages/chat/UserMessage'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = (params.get('scene') || 'other') as 'other' | 'self'
const theme = params.get('theme') || 'dark'
const fixOn = params.get('fix') !== 'off'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** Synthetic roster. Slot keys must match `chat-<n>-<unix-ts>` to resolve at all. */
const HERE = 'chat-1-1788000000'
const THERE = 'chat-2-1788000001'
const ROSTER: ReadonlyMap<string, string> = new Map([
  [HERE, 'Packing checklist review'],
  [THERE, 'Release notes draft'],
])

const TARGET = scene === 'self' ? HERE : THERE
const NOTE = `Handoff complete: the checklist is ready. Next: [${ROSTER.get(TARGET)}](/chat?sid=${TARGET})`

/** The transcript's own note-row bubble, class list copied from the inject branch. */
const BUBBLE_CLS = 'msg-content px-4 py-3 text-sm leading-6 whitespace-pre-wrap rounded-lg bg-warn-subtle text-text ring-1 ring-inset forced-colors:border ring-warn/30 rounded-bl-[4px] overflow-hidden min-w-0'

/** Attributes of the rendered anchor, re-read from the DOM after every paint. */
function Readout() {
  const [attrs, setAttrs] = useState<{ target: string | null; title: string | null }>({ target: null, title: null })
  useEffect(() => {
    const a = document.querySelector<HTMLAnchorElement>('[data-bubble] a')
    setAttrs({ target: a?.getAttribute('target') ?? null, title: a?.getAttribute('title') ?? null })
  })
  const line = (k: string, v: string | null) => (
    <div className="flex gap-2">
      <span className="text-muted w-14 shrink-0">{k}</span>
      <span className={v ? 'text-text' : 'text-muted'}>{v ? v.replace(/\n/g, ' \u2937 ') : '\u2014 absent'}</span>
    </div>
  )
  return (
    <div
      data-readout
      className="mx-auto w-full px-4 pt-4 font-mono text-[12px] leading-5"
      style={{ maxWidth: 900 }}
    >
      <div className="text-muted pb-1">rendered &lt;a&gt;, read from the DOM</div>
      <div className="rounded-lg border border-border bg-bg-elevated px-3 py-2 flex flex-col gap-0.5">
        {line('target', attrs.target)}
        {line('title', attrs.title)}
      </div>
    </div>
  )
}

function Scene() {
  const [active, setActive] = useState(HERE)
  return (
    <div className="bg-bg text-text flex flex-col min-h-screen">
      <div
        className="border-b border-border px-4 py-2 text-[13px] text-muted mx-auto w-full"
        style={{ maxWidth: 900 }}
      >
        Viewing session <span data-active className="text-text font-medium">{ROSTER.get(active)}</span>
      </div>
      <div className="px-4 pt-4 mx-auto w-full flex flex-col gap-3" style={{ maxWidth: 900 }}>
        <div className="self-end max-w-[80%]">
          <UserMessage
            content="Post a note when the packing checklist is ready, and link the session that picks it up."
            renderContent={c => <MarkdownRenderer content={c} />}
          />
        </div>
        <div className="flex flex-col items-start max-w-[80%]" data-bubble>
          <div className={BUBBLE_CLS}>
            {fixOn
              ? <MarkdownRenderer
                  content={NOTE}
                  onSessionOpen={setActive}
                  sessions={ROSTER}
                  activeSession={active}
                />
              : <MarkdownRenderer content={NOTE} />}
          </div>
        </div>
      </div>
      <Readout />
    </div>
  )
}

interface LinkMeasure {
  scene: string
  fix: string
  /** Attributes read off the rendered anchor — the reviewable claim itself. */
  href: string | null
  target: string | null
  rel: string | null
  title: string | null
  /** Title the active-session strip shows, so a switch is read from the DOM. */
  activeTitle: string
  /** Unchanged across a click is what "in place" means. */
  locationHref: string
}

declare global {
  interface Window {
    __measure: () => LinkMeasure
  }
}

window.__measure = () => {
  const a = document.querySelector<HTMLAnchorElement>('[data-bubble] a')
  return {
    scene,
    fix: fixOn ? 'on' : 'off',
    href: a?.getAttribute('href') ?? null,
    target: a?.getAttribute('target') ?? null,
    rel: a?.getAttribute('rel') ?? null,
    title: a?.getAttribute('title') ?? null,
    activeTitle: (document.querySelector<HTMLElement>('[data-active]')?.textContent ?? '').trim(),
    locationHref: location.href,
  }
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Scene />)
