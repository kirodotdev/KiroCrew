/**
 * Visual evidence for the pinned-prompt HAND-OFF, recorded as a scroll.
 *
 * WHY ISOLATED: the claim is about what the eye sees across ~40 frames of a
 * scroll, and reproducing it in a live session means finding a session whose
 * prompts are taller than one line, at a scroll offset that straddles the
 * hand-off, and scrolling it by the same amount twice. A capture page makes the
 * same scroll reproducible frame for frame.
 *
 * WHAT IS FAITHFUL is the GEOMETRY, since that is the whole claim:
 *   - `usePinnedPrompt` is the REAL hook — the code the fix changes.
 *   - `PinnedPrompt` is the REAL banner card.
 *   - `UserMessage` is the REAL bubble.
 *   - Rows are wrapped in the literal host wrapper (`px-4 mx-auto w-full py-1`
 *     under `--mc-content-width`, ChatPage.tsx:7066) and hidden by the literal
 *     host rule (`visibility: 'hidden'` matched on message ts, ChatPage.tsx:7089).
 *   - The fold sentinel sits directly under an opaque title row, as in ChatPage.
 * Nothing about the hand-off decision is reimplemented here.
 *
 *   ?theme=dark|light
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6820 --strictPort
 *   node scripts/capture-pinned-prompt-handoff.mjs http://127.0.0.1:6820 \
 *     ../temp-screenshots/pinned-prompt-handoff
 */
import { useEffect, useRef } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import PinnedPrompt from '../src/pages/chat/PinnedPrompt'
import UserMessage from '../src/pages/chat/UserMessage'
import { usePinnedPrompt } from '../src/pages/chat/usePinnedPrompt'
import type { DisplayItem } from '../src/pages/chat/types'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
/**
 * Draw a dashed line on the card's resting position (the "pin position"). An
 * annotation, not chrome: without it a reader cannot see that the bubble
 * travelled past the place the banner later lands on.
 */
const guides = params.get('guides') === '1'
/**
 * Make the middle prompt taller than the viewport — the case the bottom-edge rule
 * was chosen for ("an essay, a pasted stack trace"). Used to measure what the
 * top-edge rule does to it, which the three-line prompts cannot show.
 */
const tall = params.get('tall') === '1'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/**
 * Deliberately MULTI-LINE prompts. A one-line prompt is the single case where
 * the hand-off line and the bubble's own bottom edge coincide, so it is the one
 * case that already looks right — a capture built from one-liners would
 * photograph the working path and prove nothing.
 */
const PROMPTS = [
  'Prompt one. Walk the retry path and say which of the three call sites can '
  + 'observe a partially written batch, then tell me whether the guard belongs '
  + 'on the writer or on every reader.',
  'Prompt two. The cache warms on first read, so the second request pays no '
  + 'latency and the first pays all of it. Measure both and tell me whether '
  + 'pre-warming on startup is worth the extra boot time.',
  'Prompt three. Two of the four workers exit before draining their queue. '
  + 'Find the shutdown path that skips the drain and say whether the fix is a '
  + 'join or a sentinel.',
]

const REPLY = 'Traced it end to end. The writer commits the batch in one pass, '
  + 'so a reader can only ever see the previous complete batch or the new one, '
  + 'never a mixture. The guard on each reader was defensive rather than load '
  + 'bearing. I removed two of them and left the third, which also covers the '
  + 'empty-batch case that the writer does not.'

function buildMessages(): ChatMessage[] {
  const out: ChatMessage[] = []
  // Leading reply text so the FIRST prompt starts below the fold, as it does in a
  // real session (a transcript scrolled to its top has the earlier-messages bar
  // and the turn's own chrome above the first row). Without it the harness put
  // row 0's top exactly ON the hand-off line at scrollTop 0, which is a
  // coincidence of the harness and not of the product — it pinned before the
  // reader had scrolled at all.
  out.push({ role: 'assistant', content: REPLY, ts: '2026-01-01T00:00:00.000Z' } as ChatMessage)
  PROMPTS.forEach((content, i) => {
    const body = (tall && i === 1)
      ? Array.from({ length: 30 }, (_, k) => `Line ${k + 1} of a pasted stack trace that the reader has not finished reading yet.`).join('\n')
      : content
    out.push({ role: 'user', content: body, ts: `2026-01-0${i + 2}T00:00:00.000Z` } as ChatMessage)
    // Enough reply height that each prompt scrolls past the fold on its own,
    // one at a time — the displacing sequence the claim is about.
    for (let k = 0; k < 4; k++) {
      out.push({ role: 'assistant', content: REPLY, ts: `2026-01-0${i + 2}T00:0${k + 1}:00.000Z` } as ChatMessage)
    }
  })
  return out
}

const MESSAGES = buildMessages()
const ITEMS: DisplayItem[] = MESSAGES.map((msg, idx) => ({ kind: 'single', msg, idx }))

function PinnedHost() {
  const scrollerRef = useRef<HTMLDivElement | null>(null)
  const {
    displayItemsRef, pinFoldRef, pinCardRef, pinned,
    pinExpanded, setPinExpanded, onPinCollapsedHeight, scrollTranscriptBy, updatePinnedPrompt, onScrollPin,
  } = usePinnedPrompt({ scrollerRef })

  displayItemsRef.current = ITEMS
  // One recompute after layout so the band is correct at scrollTop 0, matching
  // ChatPage's own mount-time call.
  useEffect(() => { updatePinnedPrompt() }, [updatePinnedPrompt])

  return (
    <div data-capture-root className="relative h-screen w-screen flex flex-col bg-bg text-text overflow-hidden">
      {/* Overlay band: title row, fold sentinel, banner — the ChatPage stack. */}
      <div className="absolute inset-x-0 top-0 z-20 pointer-events-none">
        <div className="pointer-events-auto bg-bg border-b border-border px-4 py-2 text-sm text-muted">
          Pinned prompt hand-off
        </div>
        {/* Fold sentinel — zero-height, always mounted. Its top edge is the line
            the pinned prompt sticks to (see updatePinnedPrompt). */}
        <div ref={pinFoldRef} aria-hidden className="h-0" />
        {guides && (
          <div
            aria-hidden
            className="relative"
            style={{ height: 0 }}
          >
            <div
              style={{
                position: 'absolute', left: 0, right: 0, top: 4, height: 0,
                borderTop: '1px dashed #f0a', opacity: 0.9,
              }}
            />
            <div
              style={{
                position: 'absolute', right: 8, top: 6,
                font: '11px ui-monospace, monospace', color: '#f0a',
              }}
            >
              pin position
            </div>
          </div>
        )}
        {pinned && (
          <PinnedPrompt
            text={pinned.text}
            fullText={pinned.full}
            images={pinned.images}
            bodyBeyondPreview={pinned.bodyBeyondPreview}
            pushUp={pinned.push}
            liveH={pinned.liveH}
            bannerH={pinned.bannerH}
            expanded={pinExpanded}
            onToggleExpanded={() => setPinExpanded(p => !p)}
            onJump={() => undefined}
            cardRef={pinCardRef}
            onCollapsedHeight={onPinCollapsedHeight}
            scrollTranscriptBy={scrollTranscriptBy}
          />
        )}
      </div>

      <div
        ref={scrollerRef}
        data-capture-scroller
        onScroll={onScrollPin}
        className="flex-1 min-h-0 overflow-y-auto"
        style={{ paddingTop: 37, paddingBottom: 16 }}
      >
        {ITEMS.map((item, idx) => {
          const msg = item.kind === 'single' ? item.msg : null
          if (!msg) return null
          return (
            <div
              key={idx}
              data-display-index={idx}
              className="px-4 mx-auto w-full py-1"
              style={{
                maxWidth: 'var(--mc-content-width, 900px)',
                // The literal host rule: hide the row the banner is standing in
                // for, by message IDENTITY (ts), so the bubble appears to stop
                // travelling and stick rather than being replaced.
                visibility: pinned && pinned.ts != null && msg.ts === pinned.ts ? 'hidden' : undefined,
              }}
            >
              {msg.role === 'user' ? (
                <UserMessage content={msg.content} renderContent={c => <p className="my-1 leading-6">{c}</p>} />
              ) : (
                <div className="text-sm leading-6 text-text">{msg.content}</div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <PinnedHost />
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
