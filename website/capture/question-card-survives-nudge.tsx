/**
 * Evidence for "an unanswered ask_question card must survive auto-nudge cycles".
 *
 * THE PROBLEM. A monitored session called `ask_question`, the dashboard rendered
 * the stateless card above the composer, and the monitor loop then ran ~10
 * nudge cycles. `nudge` was a QUESTION_RETIRING_ROLE, so each cycle deleted the
 * card (and, server-side, the `/api/ask-question/pending` record a reload
 * rehydrates from). The user came back to a session that still needed an answer
 * with nothing on screen to answer it.
 *
 * THE SCENE. The REAL `PendingQuestionCard`, driven by the REAL `chatSlice`
 * reducer, in the same wrapper `ChatPage` renders it in (`px-4 pb-2`, content
 * width), above a static composer replica. One scene per `?scene=`:
 *
 *   ?scene=before   the OLD behaviour, replicated by dispatching the retirement
 *                   the old reducer performed on the nudge frame — the card is
 *                   gone while the question is still open. A replica because the
 *                   role set no longer contains `nudge`; the frames listed above
 *                   the panel are the real ones that were applied.
 *   ?scene=after    the same ten `nudge` frames through the real reducer: the
 *                   card is still there and still answerable.
 *   ?scene=reload   a FRESH store (what a reload starts from) rehydrated from a
 *                   `/api/ask-question/pending` row through the real
 *                   `reconcileQuestions`, then two more nudge frames.
 *
 *   ?theme=dark|light   ?lang=en|zh-CN
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import PendingQuestionCard from '../src/components/PendingQuestionCard'
import { clearQuestionCard, setQuestionCard, sseChatMessage } from '../src/store/chatSlice'
import { reconcileQuestions } from '../src/hooks/useWebSocket'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'after'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const lang = params.get('lang') || 'en'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(lang)

const SLOT = 'chat-110'
const QUESTIONS = [
  {
    question: 'Screenshots for this PR: which route?',
    header: 'EVIDENCE',
    options: [
      { label: 'Capture page + Playwright', description: 'Real components, no pod needed' },
      { label: 'Label it no-screenshots', description: 'Behavioural change only' },
    ],
  },
]

/** The frames each scene applies, rendered as the caption so the panel says what
 *  it did rather than asking the reader to trust the title. */
const CAPTIONS: Record<string, string[]> = {
  before: [
    'ask_question → question_card (stateless, no ask_id)',
    'nudge  [auto-nudge cycle 1]  → card RETIRED (old behaviour)',
    'the question is still unanswered, and there is nothing left to answer it',
  ],
  after: [
    'ask_question → question_card (stateless, no ask_id)',
    'nudge × 10  [auto-nudge cycles 1…10], assistant × 10 between them',
    'the card is still on screen and still answerable',
  ],
  reload: [
    'fresh store (a reload holds no cards — question_card is a one-shot frame)',
    'GET /api/ask-question/pending → reconcileQuestions → the card is restored',
    'nudge × 2 after the reload leave it in place',
  ],
}

const nudge = (cycle: number) =>
  store.dispatch(
    sseChatMessage({
      slot: SLOT,
      role: 'nudge',
      content: `[auto-nudge cycle ${cycle}]\ncheck the PR and report`,
      meta: { mid: `n-${cycle}` },
    }) as never,
  )

const post = () =>
  store.dispatch(setQuestionCard({ slot: SLOT, questions: QUESTIONS, fresh: true }) as never)

if (scene === 'reload') {
  // Exactly what useWebSocket.syncPendingQuestions does with a /pending row.
  const { add } = reconcileQuestions({}, {}, [
    { slot: SLOT, card_id: 'card-a', questions: QUESTIONS },
  ])
  for (const q of add) {
    store.dispatch(
      setQuestionCard({ slot: q.slot as string, card_id: q.card_id, questions: q.questions as typeof QUESTIONS }) as never,
    )
  }
  nudge(1)
  nudge(2)
} else if (scene === 'before') {
  post()
  nudge(1)
  // The old reducer's effect, replicated: `dropStaleStatelessQuestion` deleted
  // the slot's entry when the role was in QUESTION_RETIRING_ROLES.
  store.dispatch(clearQuestionCard({ slot: SLOT }) as never)
} else {
  post()
  for (let cycle = 1; cycle <= 10; cycle++) {
    nudge(cycle)
    store.dispatch(
      sseChatMessage({
        slot: SLOT,
        role: 'assistant',
        content: `cycle ${cycle}: CI still red on the same lane.`,
        meta: { mid: `a-${cycle}` },
      }) as never,
    )
  }
}

function Scene() {
  return (
    <div className="min-h-screen bg-[var(--mc-bg)] text-[var(--mc-text)] p-6">
      <div className="mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
        <div className="text-xs opacity-70 mb-3">
          {CAPTIONS[scene]?.map((line) => <div key={line}>{line}</div>)}
        </div>
        {/* Transcript tail, so the nudge cycles are visible as rows the way they
            are in the session the card belongs to. */}
        <div className="rounded-lg border border-[var(--mc-border)] p-3 mb-3 text-[13px] opacity-80">
          <div className="mb-1">🤖 I need a decision before I can carry on.</div>
          <div className="mb-1 opacity-70">↻ [auto-nudge cycle 1] check the PR and report</div>
          <div className="opacity-70">🤖 cycle 1: CI still red on the same lane.</div>
        </div>
        {/* The wrapper ChatPage uses for this card, so the placement above the
            composer is the real one. */}
        <div className="px-4 pb-2 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
          <PendingQuestionCard slotKey={SLOT} onFallbackSend={() => {}} onDirectSend={() => {}} />
        </div>
        {/* Static composer replica: the card's anchor. Inline-styled on purpose,
            so it can never be mistaken for the real composer's rendering. */}
        <div
          style={{
            border: '1px solid var(--mc-border)',
            borderRadius: 12,
            padding: '12px 14px',
            opacity: 0.6,
            fontSize: 13,
          }}
        >
          Ask anything…
        </div>
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <Scene />
  </Provider>,
)
