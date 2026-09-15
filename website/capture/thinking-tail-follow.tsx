/**
 * Evidence harness for the EXPANDED reasoning panel following its own tail.
 *
 * WHY ISOLATED: the behaviour only shows on a trace long enough to overflow the
 * panel's 360px cap while chunks are still arriving. A live session reaches that
 * state only during a long reasoning turn, and reasoning is never persisted (the
 * backend broadcasts `chat_thinking` and drops it), so it cannot be replayed
 * from history either. The sibling `thinking-turn-fold` and `thinking-bursts`
 * harnesses document the same constraint.
 *
 * WHAT IS FAITHFUL: ThinkingBlock is the real component, unmocked, inside the
 * real `RowDisclosureProvider` and the real stylesheet — so the scroll container
 * under test, its 360px cap and its overflow behaviour are the shipping ones.
 * The panel is expanded by CLICKING its header, exactly as a reader does, rather
 * than by forcing state.
 *
 * WHY THE PARAGRAPHS ARE NUMBERED: the whole claim is *which end of the trace is
 * on screen*, and unnumbered prose cannot show that. Each paragraph is labelled
 * `[n/N]`, so a frame showing `[1/N]` is parked at the oldest text and one
 * showing `[N/N]` is at the newest — legible without measuring anything.
 *
 * Run the SAME page against the pre-change ThinkingBlock for the "before" frame
 * and the patched one for "after"; the harness never changes, so the delta it
 * shows is only the code.
 *
 *   ?theme=dark|light &paras=10 &stream=1
 */
import { useEffect, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import { RowDisclosureProvider } from '../src/pages/chat/rowDisclosure'
import ThinkingBlock from '../src/pages/chat/ThinkingBlock'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
// `?stream=1` keeps appending paragraphs, which is what drives the tailing:
// ThinkingBlock's scroll effect is keyed on the content growing.
const stream = params.get('stream') === '1'
const paraCount = Math.max(4, Number(params.get('paras') || 10))

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** Sentence-shaped reasoning bodies, cycled and then labelled `[n/N]`. Each is
 *  long enough to wrap to ~3 lines at the panel's measure, so a handful already
 *  overflows the 360px cap and the panel genuinely has a scrollbar. */
const BODIES = [
  'The user says the expanded reasoning panel opens at the wrong end. Let me look at how that panel is rendered before answering, rather than guessing at the cause.',
  'The collapsed row holds its one-line preview scrolled to the end deliberately — there is an effect that sets scrollLeft to scrollWidth so the newest words sit against the right edge.',
  'But the expanded body is a bare max-h-[360px] overflow-auto container. Nothing gives it a ref, nothing listens for its scroll, and nothing ever writes its scrollTop.',
  'So it sits at scrollTop 0 for as long as it is open, which shows the oldest reasoning and never moves as new chunks land at the bottom.',
  'That means opening the panel starts you at the wrong end, and reading along with the model costs a manual scroll on every burst.',
  'The subagent output pane in ActivityViewer already solves exactly this shape of problem: capped height, appended to while live, pinned to the bottom by default.',
  'Its contract is worth copying rather than reinventing — pin to the end, release the moment the reader scrolls up, re-arm when they come back to the end.',
  'The release half matters as much as the pinning. A panel that yanks you back to the bottom mid-sentence is as annoying as one that never follows at all.',
  'A slack of about a line is needed on the at-the-end test, because sub-pixel offsets routinely leave scrollTop a hair short of the true bottom.',
  'Without that slack an exact comparison never re-arms, so a reader who scrolled back down would stay unfollowed for the rest of the turn — silently.',
  'Resetting the latch on collapse is the last piece: re-opening a still-growing trace should land at the newest reasoning, wherever the reader left the panel.',
  'That keeps the expanded panel consistent with the collapsed row above it, which has always shown the newest words rather than the oldest ones.',
]

function paragraph(i: number, total: number): string {
  return `[${i + 1}/${total}] ${BODIES[i % BODIES.length]}`
}

function trace(n: number, total: number): string {
  return Array.from({ length: n }, (_, i) => paragraph(i, total)).join('\n\n')
}

function TailFollowCapture() {
  // In stream mode the trace grows one paragraph at a time, so the frames show
  // a panel being appended to rather than a static snapshot.
  const [shown, setShown] = useState(stream ? Math.max(3, paraCount - 4) : paraCount)
  useEffect(() => {
    if (!stream) return
    const timer = setInterval(() => {
      setShown(s => (s >= paraCount ? s : s + 1))
    }, 400)
    return () => clearInterval(timer)
  }, [])
  const content = useMemo(() => trace(shown, paraCount), [shown])
  return (
    <div className="px-5 mx-auto w-full py-1" style={{ maxWidth: 'var(--mc-content-width, 760px)' }}>
      <ThinkingBlock content={content} disclosureKey="capture-tail" />
    </div>
  )
}

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <div
    data-capture-root
    className="bg-bg text-text"
    style={{ width: 900, minHeight: '100vh', paddingTop: 16, paddingBottom: 16, ['--mc-content-width' as string]: '760px' }}
  >
    <RowDisclosureProvider resetKey="capture">
      <TailFollowCapture />
    </RowDisclosureProvider>
  </div>,
)
