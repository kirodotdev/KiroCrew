import { useCallback, useEffect, useRef, useState, memo, type RefObject } from 'react'
import { flushSync } from 'react-dom'
import { useImeGuard } from '../hooks/useImeGuard'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { Check, ChevronLeft, ChevronRight, MessageSquare } from 'lucide-react'
import { Glass } from './Glass'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
interface QuestionOption {
  label: string
  description?: string
}

interface Question {
  question: string
  header?: string
  options: QuestionOption[]
  multiSelect?: boolean
}

interface QuestionCardProps {
  questions: Question[]
  onSubmit: (answers: Record<string, string>) => void
  /** Unblock the agent with no answer, or — for a legacy card, where nothing is
   *  blocked — just take the card off screen. Always supplied by
   *  PendingQuestionCard: a card the user can neither answer nor remove sits on
   *  top of the composer forever. */
  onDismiss?: () => void
  /** True while a submission is in flight: both controls lock so a second
   *  click cannot produce a duplicate resolution or a duplicate chat turn. */
  busy?: boolean
  /** Surface-specific action copy; other chat callers keep the standard label. */
  submitLabel?: string
  /** Flips of "the user has an answer in progress" — a non-empty custom
   *  input OR a pending option selection. All of that state lives only in
   *  this component; publishing the boolean lets the store refuse to
   *  auto-retire (unmount) a card whose half-entered answer would be
   *  silently destroyed by a turn-consuming frame. */
  onDraftChange?: (active: boolean) => void
}

function QuestionCard({ questions, onSubmit, onDismiss, busy = false, onDraftChange, submitLabel }: QuestionCardProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const ime = useImeGuard()
  const [selections, setSelections] = useState<Record<number, Set<string>>>({})
  const [customInputs, setCustomInputs] = useState<Record<number, string>>({})
  const reduceMotion = useReducedMotion()
  /* Which question is on screen. ONE index replaces the three mechanisms a
     stacked card needed to stay usable — a fold-all-but-the-first on mount, an
     auto-fold that opened the next unanswered question on every answer, and a
     viewport cap with an inner scroller for whatever was still open. A pager is
     bounded by construction: the card is never taller than its tallest single
     question, so none of that is load-bearing any more.

     What the stack did give away for free was review — a folded row carried its
     own answer, so the whole card could be checked before Submit. That is not
     free here, so it is paid for explicitly: the footer names how many questions
     are still unanswered and jumps to the first of them. */
  const [requestedPage, setPage] = useState(0)
  /* Which way the last move went, so the leaving and entering questions slide
     the same way. Not derived from the indices during render: the exiting page
     is already gone from state by the time AnimatePresence animates it out. */
  const [direction, setDirection] = useState(1)
  /* All three state maps are keyed by question INDEX, which only holds while the
     question set does. PendingQuestionCard keys this component by `ask_id` — but
     a legacy (ask_id-less) card falls back to the slot key, so a second
     stateless card in the same slot does NOT remount and would inherit the
     previous card's page and picks (index 0 of a different question).
     Compared on the whole serialized PAYLOAD, never on array identity and never
     on the prompts alone. Identity is wrong because a websocket reconnect
     re-dispatches the SAME still-pending card with a freshly parsed array
     (useWebSocket's syncPendingQuestions), and treating that as a new set would
     silently discard answers the user had already entered. Prompts alone are
     wrong because a card can reuse a prompt with DIFFERENT options, and a
     retained selection would then submit a label absent from the current card.
     The payload is small (a handful of questions, each with a handful of
     options, validated before broadcast), so serializing it per render is
     cheaper than the class of bug either shortcut admits. */
  const questionKey = JSON.stringify(questions)
  const [lastKey, setLastKey] = useState(questionKey)
  if (questionKey !== lastKey) {
    setLastKey(questionKey)
    setSelections({})
    setCustomInputs({})
    setPage(0)
    setDirection(1)
  }

  /* The reset above schedules `setPage(0)`, but React finishes THIS pass with the
     stale value, so a replacement question set that is SHORTER than the one the
     user had paged into would index past its end and throw — from the render, so
     it escapes to the root boundary and takes the whole shell down. That path is
     reachable by design: a stateless card carries no `ask_id`, so the key falls
     back to the slot and the component does not remount, and `page > 0` implies
     an answer exists, which is exactly when the draft guard deliberately KEEPS
     the card mounted. The crash would therefore destroy the answers the guard
     retained it to protect. Clamped once here rather than at the index site, so
     the requested page is unreadable downstream and every derived value —
     `isLast`, the indicator, the arrows' disabled state, the per-question
     selection and custom-input lookups — agrees on the same in-range page. */
  const page = Math.max(0, Math.min(requestedPage, questions.length - 1))

  /* Publish "answer in progress" to the store — pending option selections
     count exactly like typed custom text: both are component-local work a
     turn-consuming frame would silently destroy if the card auto-retired.
     One effect observes EVERY mutation path (option toggles, custom-input
     edits, the question-set reset above) instead of instrumenting each
     handler, and the cleanup clears the flag on unmount so a card removed
     for any other reason (self-answer, dismiss, resolution) cannot leave a
     stale draftActive behind blocking a future card's retirement. */
  const draftActive =
    Object.values(selections).some(s => s.size > 0) ||
    Object.values(customInputs).some(v => v.trim() !== '')
  const draftRef = useRef(onDraftChange)
  draftRef.current = onDraftChange
  useEffect(() => {
    draftRef.current?.(draftActive)
  }, [draftActive])
  useEffect(() => () => { draftRef.current?.(false) }, [])

  /** The answer for question *i*: a typed custom answer wins over picks, mirroring
   *  the mutual exclusion the two inputs enforce. `''` when unanswered. */
  const answerOf = (i: number) => {
    const custom = customInputs[i]?.trim()
    if (custom) return custom
    const selected = selections[i]
    return selected?.size ? [...selected].join(', ') : ''
  }

  const isAnswered = (i: number) => !!answerOf(i)

  /** Move to `next`, clamped, recording the direction for the slide. A no-op move
   *  still sets direction, which is harmless and keeps the callers branch-free. */
  const goTo = (next: number) => {
    const clamped = Math.max(0, Math.min(questions.length - 1, next))
    setDirection(clamped >= page ? 1 : -1)
    setPage(clamped)
  }

  /* The corner arrows and the footer Next move the page they themselves live on,
     and a move can take the activated control out from under its own focus: an
     arrow that reaches its end becomes `disabled`, and the footer Next becomes
     disabled on a page whose question is still unanswered — including the last
     page, where React reuses its node as the Submit button, disabled until that
     question is answered. A browser drops focus from an element that becomes
     disabled (or leaves the document), so it falls to <body> and the next Tab
     restarts from the top of the page — the same silent end of the keyboard walk
     `carryFocus` exists to prevent, left open on the controls `carryFocus`
     deliberately does not touch (they must not pull the caret into the text
     box). Focus goes instead to the corner arrow pointing back the way the user
     came: it is the one control guaranteed enabled on the page just reached, and
     it keeps the user inside the card without opening anything.

     The move is committed synchronously first. On a two-question card the
     receiving arrow is itself still disabled at click time (`‹` on page 0, `›`
     on page 1), and `focus()` on a disabled button is a no-op — the hand-off has
     to happen after React has re-rendered the arrows, not before. */
  const prevArrowRef = useRef<HTMLButtonElement>(null)
  const nextArrowRef = useRef<HTMLButtonElement>(null)
  const goToHandingFocus = (next: number, receiver: RefObject<HTMLButtonElement>) => {
    flushSync(() => goTo(next))
    receiver.current?.focus()
  }

  /* The page body is keyed by index, so advancing UNMOUNTS the custom-answer
     input — and with it the focus, which falls to <body>. That silently ends the
     keyboard walk the Enter handler exists to enable: the first Enter moves on,
     and the second goes nowhere. Focus is carried across only when the move came
     from the keyboard, so clicking a corner arrow does not yank the caret into a
     text box the user never asked for.

     Attached from the ENTERING input itself rather than from an effect on `page`.
     `mode="wait"` keeps the EXITING child rendered for the whole exit, so the
     commit that changes `page` has not mounted the replacement yet: an effect
     there focuses the input that is about to unmount, and focus falls to <body>
     when it goes. A callback ref runs when the replacement input mounts, which is
     after the exit has finished. Identity is stable, so React does not detach and
     re-attach it on unrelated renders. */
  const carryFocus = useRef(false)
  const bindCustomInput = useCallback((el: HTMLInputElement | null) => {
    if (!el || !carryFocus.current) return
    carryFocus.current = false
    el.focus()
  }, [])

  /* AnimatePresence keeps the previous page mounted during its exit. Event
     handlers on that retained element also keep their old render closure, so a
     `qIdx === page` check would compare two stale values and let keyboard
     activation through. Read the current page through a ref instead: pointer,
     keyboard and synthetic activations from any exiting page all become no-ops. */
  const pageRef = useRef(page)
  pageRef.current = page

  const toggleOption = (qIdx: number, label: string, multi: boolean) => {
    if (qIdx !== pageRef.current) return
    const wasSelected = !!selections[qIdx]?.has(label)
    setSelections(prev => {
      const current = prev[qIdx] || new Set<string>()
      const next = new Set(current)
      if (multi) {
        if (next.has(label)) next.delete(label); else next.add(label)
      } else {
        next.clear()
        if (!current.has(label)) next.add(label)
      }
      return { ...prev, [qIdx]: next }
    })
    setCustomInputs(prev => ({ ...prev, [qIdx]: '' }))
    /* Advance to the next question that still needs an answer. Only for
       single-select — a multi-select is not finished after one click, so
       advancing would steal the second pick — only when the card holds more than
       one question, and never when the click DESELECTED (the user is still
       choosing, and jumping away would look like the deselect did something
       else).

       Auto-advance is what makes a 4-question card one gesture per question
       instead of an answer plus a Next. It targets the next UNANSWERED question
       rather than `page + 1`, so re-opening an earlier question to change its
       pick returns to whatever is still outstanding instead of walking forward
       from the middle. When nothing is outstanding the page holds still: the
       card is complete and Submit is the only thing left to do. Answered means a
       picked option or typed custom text, the same pair Submit reads. */
    if (!multi && !wasSelected && questions.length > 1) {
      const answered = (i: number) =>
        i === qIdx ||
        (selections[i]?.size ?? 0) > 0 ||
        (customInputs[i] ?? '').trim() !== ''
      const nextUnanswered = questions.findIndex((_, i) => !answered(i))
      if (nextUnanswered !== -1) goTo(nextUnanswered)
    }
  }

  const handleSubmit = () => {
    const answers: Record<string, string> = {}
    questions.forEach((q, i) => {
      const answer = answerOf(i)
      if (answer) answers[q.question] = answer
    })
    onSubmit(answers)
  }

  /* Every question must be answered before Submit unlocks. The answer map is
     keyed by question text, so a partial submit resumes the blocked agent with
     a map missing entries it asked for -- it cannot tell "unanswered" from
     "never asked" and proceeds on incomplete input. A multi-question card is
     one atomic ask, so the gate is `every`, not `some`. */
  const allAnswered = questions.every((_, i) => isAnswered(i))
  const unansweredCount = questions.filter((_, i) => !isAnswered(i)).length
  const firstUnanswered = questions.findIndex((_, i) => !isAnswered(i))
  const paged = questions.length > 1
  const isLast = page === questions.length - 1
  /* Which primary action the footer offers: Next on every question except the
     last, Submit only there. The button under the pointer is then always the one
     that moves the card forward — otherwise a multi-select (where auto-advance
     deliberately does not fire, since one click does not finish it) leaves a
     disabled Submit as the only primary control and the corner `›` as the sole
     way on.

     Strictly positional, not "Submit once complete": one card is one atomic ask,
     so Submit belongs at the end of the walk and nowhere else. Completing the
     questions out of order therefore costs a trip to the last page, which the
     corner arrows and the unanswered jump both make one click. */
  const showSubmit = isLast
  const q = questions[page]
  /* Off-screen questions are unmounted, so an unanswered one is invisible — and
     on a paged card Submit is disabled with nothing on screen explaining why.
     This is the replacement for the answer summaries folded rows used to carry,
     and it is a button rather than a label because naming the problem without
     offering the jump would leave the user paging to hunt for it.

     Except when the outstanding question is the one already on screen, where the
     jump would land where the user is standing: an affordance that produces no
     visible change reads as broken, so it degrades to plain text and keeps only
     the count. */
  const showUnansweredJump = paged && !allAnswered
  const unansweredIsOnScreen = firstUnanswered === page
  /* Distance, not offset: reduced motion drops the slide entirely (the crossfade
     still marks the change) and the pages otherwise travel a short, equal
     distance in whichever direction the move went. */
  const slide = reduceMotion ? 0 : 12
  /* Dynamic variants rather than object-valued `initial` / `exit`: an object
     `exit` is captured with the page that rendered it, so the leaving question
     slides using the direction from BEFORE the move and the first reversal
     animates the wrong way. A function variant is resolved at exit time, and
     `custom` on AnimatePresence is what reaches it — which is the whole reason
     that prop exists. */
  const pageSlide = {
    enter: (d: number) => ({ opacity: 0, x: d * slide }),
    center: { opacity: 1, x: 0, pointerEvents: 'auto' as const },
    /* `mode="wait"` keeps the leaving page mounted for the whole exit. Keep the
       pointer fence so a rapid second click lands nowhere; `toggleOption` also
       rejects stale-page activation because pointer events do not stop Enter or
       key auto-repeat on the focused exiting button. */
    exit: (d: number) => ({ opacity: 0, x: d * -slide, pointerEvents: 'none' as const }),
  }

  return (
    /* Height-capped, and the question scrolls inside that cap. The card mounts in
       a static block above the composer, so a card taller than the remaining
       column height grows PAST the top of the viewport and is clipped there,
       unreachable because nothing between it and the window edge scrolls. Paging
       makes that far less likely than stacking did — one question, not four — but
       a single question with many long options can still overflow a short window,
       and the failure mode is bad enough to keep insured against. */
    <Glass variant="chip" radius={12} className="glass-accent glass-shadow rounded-xl animate-scale-in flex flex-col max-h-[min(60vh,32rem)]">
      {/* The clip lives one level in, not on the pane: the pane's hairlines sit
          half a pixel OUTSIDE its top and bottom edges, and `overflow: hidden`
          on the pane itself would cut them. The inner box inherits the radius
          so the scroller's edge and its scrollbar are still clipped to the arc. */}
      <div className="flex flex-col min-h-0 overflow-hidden rounded-[inherit]">
      {/* The scroller holds ONLY the question; the action row below stays out of
          it so Submit / Dismiss are reachable without scrolling to the end. */}
      <div className="flex-1 min-h-0 overflow-y-auto">
        <div className="px-4 py-2.5">
          {/* Narrow-first: the row wraps, and the pager takes a full row of its
              own under the text until `md:` hands it back its corner. The badge
              (up to 50 chars) and the pager (two 36px arrows plus "N/M") are both
              natural-width, so on one row at 320px they left the question a
              ~56px column; the badge may also shrink and wrap rather than push
              the question out, which on a desktop never triggers because it is
              never wider than the row. */}
          <div className="flex flex-wrap gap-2 items-start">
            {q.header && <span className="text-[11px] font-semibold uppercase tracking-wider text-accent bg-accent-subtle px-2 py-0.5 rounded mt-px">{q.header}</span>}
            <span className="flex-1 min-w-0 text-[13px] font-medium text-text">{q.question}</span>
            {/* Top-right pager. Only when there is more than one question: on a
                single question it would be a permanently disabled pair of arrows
                next to "1/1", which says nothing and takes the header's width. */}
            {paged && (
              <div className="w-full md:w-auto shrink-0 flex items-center justify-end gap-0.5 -mt-0.5 -mr-1">
                <button
                  ref={prevArrowRef}
                  type="button"
                  onClick={() => (page - 1 === 0 ? goToHandingFocus(page - 1, nextArrowRef) : goTo(page - 1))}
                  disabled={page === 0}
                  aria-label={i18nT('components.questionCard.previous_question')}
                  className="inline-flex items-center justify-center min-h-9 min-w-9 rounded-md bg-transparent border-none text-muted enabled:hover:text-text enabled:cursor-pointer disabled:opacity-30 transition-colors"
                >
                  <ChevronLeft size={14} />
                </button>
                {/* Announced on change: the question text swaps without any focus
                    move, so a screen-reader user otherwise gets no signal that
                    the card advanced. `atomic` so the position is read as one
                    phrase rather than a bare changed digit. */}
                <span
                  aria-live="polite"
                  aria-atomic="true"
                  className="text-[12px] tabular-nums text-muted px-0.5 select-none"
                >
                  <span className="sr-only">
                    {i18nT('components.questionCard.question_x_of_y', { current: page + 1, total: questions.length })}
                  </span>
                  <span aria-hidden="true">{page + 1}/{questions.length}</span>
                </span>
                <button
                  ref={nextArrowRef}
                  type="button"
                  onClick={() => (page + 1 === questions.length - 1 ? goToHandingFocus(page + 1, prevArrowRef) : goTo(page + 1))}
                  disabled={page === questions.length - 1}
                  aria-label={i18nT('components.questionCard.next_question')}
                  className="inline-flex items-center justify-center min-h-9 min-w-9 rounded-md bg-transparent border-none text-muted enabled:hover:text-text enabled:cursor-pointer disabled:opacity-30 transition-colors"
                >
                  <ChevronRight size={14} />
                </button>
              </div>
            )}
          </div>
          {/* `mode="wait"` so the leaving question is gone before the next one
              lands: overlapping absolutely-positioned pages would need a fixed
              height, which is the thing paging exists to avoid. */}
          <AnimatePresence initial={false} mode="wait" custom={direction}>
            <motion.div
              key={page}
              custom={direction}
              variants={pageSlide}
              initial="enter"
              animate="center"
              exit="exit"
              transition={{ duration: reduceMotion ? 0.1 : 0.16 }}
            >
              <div className="pt-2.5 flex flex-col gap-1.5">
                {q.options.map(opt => {
                  const isSelected = !!selections[page]?.has(opt.label)
                  return (
                    <button
                      key={opt.label}
                      onClick={() => toggleOption(page, opt.label, q.multiSelect ?? false)}
                      /* WCAG 4.1.2: the selected state must be programmatic, not
                         CSS-only. aria-pressed (toggle button) in BOTH modes: it
                         matches multiSelect's independent toggles exactly, and for
                         single-select it keeps the intended click-again-to-deselect
                         honest — role=radio would promise a control that cannot be
                         unchecked by re-activating it, which this one can. */
                      aria-pressed={isSelected}
                      className={`flex items-start gap-2 text-left px-3 py-2 rounded-lg text-[13px] cursor-pointer transition-all border ${
                        isSelected
                          ? 'border-accent text-text bg-accent-subtle/60'
                          : 'border-border text-muted hover:text-text hover:border-accent/40 bg-bg'
                      }`}
                    >
                      {/* A multi-select is visually identical to a single-select
                          until you try a second option and watch the first one
                          stay lit, so the box is the only thing on screen that
                          says more than one pick is allowed. Rendered ONLY for
                          multiSelect: an indicator on both modes would erase the
                          very distinction it exists to draw.

                          Decorative, hence aria-hidden. The button already
                          carries its state programmatically via aria-pressed
                          above; a second, differently-shaped state cue inside
                          the accessible name would announce a checkbox nested in
                          a pressed toggle and describe one control as two. */}
                      {q.multiSelect && (
                        <span
                          aria-hidden="true"
                          className={`mt-[3px] shrink-0 w-3.5 h-3.5 rounded-sm border flex items-center justify-center ${
                            isSelected ? 'border-accent bg-accent text-accent-fg' : 'border-border bg-bg'
                          }`}
                        >
                          {isSelected && <Check size={10} strokeWidth={3} />}
                        </span>
                      )}
                      <span className="min-w-0">
                        <span className="font-medium">{opt.label}</span>
                        {opt.description && <span className="text-muted text-[12px] ml-2">{opt.description}</span>}
                      </span>
                    </button>
                  )
                })}
              </div>
              <input
                ref={bindCustomInput}
                type="text"
                aria-label={i18nT('components.questionCard.custom_answer')}
                placeholder={i18nT('components.questionCard.or_type_a_custom_answer')}
                maxLength={2000}
                value={customInputs[page] || ''}
                onChange={e => {
                  if (page !== pageRef.current) return
                  setCustomInputs(prev => ({ ...prev, [page]: e.target.value }))
                  setSelections(prev => ({ ...prev, [page]: new Set() }))
                }}
                {...ime.bindComposition()}
                onKeyDown={e => {
                  if (page !== pageRef.current) return
                  if (e.key !== 'Enter') return
                  // Rule 1: single-line input; the readiness test stays outside.
                  if (ime.isComposing(e)) return
                  if (busy) return
                  /* `isLast` as well as `allAnswered`: Submit belongs at the end
                     of the walk, and a page that renders no Submit button must
                     not fire one from the keyboard either. Answering out of order
                     — Q2 via the arrows, back to Q1, retype, Enter — would
                     otherwise resume the agent from a page whose visible primary
                     action is Next. */
                  if (allAnswered && isLast) { handleSubmit(); return }
                  /* Otherwise Enter means "done with this one" and moves on —
                     but only once this question actually has an answer, so a
                     stray Enter in an empty box cannot skip past it. */
                  if (isAnswered(page) && !isLast) {
                    carryFocus.current = true
                    goTo(page + 1)
                  }
                }}
                className="mt-2 w-full px-3 py-2 rounded-lg border border-border bg-bg text-text text-[13px] placeholder:text-muted focus-visible:border-accent focus:outline-hidden"
              />
            </motion.div>
          </AnimatePresence>
        </div>
      </div>
      <div className="px-4 py-3 border-t border-border flex flex-col gap-1.5 shrink-0">
        {/* Its own row, not a sibling of Dismiss and Next: three peer controls in
            one horizontal group carry no ranking, so the row has to be read
            label-by-label before anything can be clicked, and it is the first
            thing to clip under width pressure. This one is also not the same KIND
            of control — it navigates rather than acting on the card. */}
        {showUnansweredJump && (
          <div className="flex">
            {unansweredIsOnScreen ? (
              <span className="px-2 py-1.5 text-[12px] font-medium text-muted">
                {i18nT('components.questionCard.n_still_unanswered', { count: unansweredCount })}
              </span>
            ) : (
              <button
                type="button"
                /* The landing page is the unanswered one, so on arrival this
                   button degrades to the plain-text span above and the focused
                   node UNMOUNTS — focus would fall to <body>, the same silent
                   end of the keyboard walk the corner arrows guard against. Hand
                   it, as they do, to the arrow pointing back the way the user
                   came: the jump never lands on the page it left, so a forward
                   jump has `‹` enabled and a backward one has `›`. */
                onClick={() => goToHandingFocus(firstUnanswered, firstUnanswered > page ? prevArrowRef : nextArrowRef)}
                className="inline-flex items-center gap-1 px-2 py-1.5 rounded-md text-[12px] font-medium cursor-pointer transition-all bg-transparent text-muted underline decoration-dotted underline-offset-2 hover:text-text hover:decoration-solid border-none"
              >
                <ChevronRight size={12} aria-hidden="true" />
                {i18nT('components.questionCard.n_still_unanswered', { count: unansweredCount })}
              </button>
            )}
          </div>
        )}
        <div className="flex justify-end items-center gap-2">
          {onDismiss && (
            <button
              onClick={onDismiss}
              disabled={busy}
              aria-label={i18nT('components.questionCard.dismiss_question_without_answering')}
              title={i18nT('components.questionCard.dismiss_hint')}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-medium cursor-pointer transition-all disabled:opacity-30 disabled:cursor-not-allowed bg-transparent text-muted hover:text-text border border-border"
            >
              {i18nT('components.questionCard.dismiss')}
            </button>
          )}
          {showSubmit ? (
            <button
              onClick={handleSubmit}
              disabled={!allAnswered || busy}
              className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-md text-[13px] font-medium cursor-pointer transition-all disabled:opacity-30 disabled:cursor-not-allowed bg-accent text-accent-fg hover:bg-accent-hover border-none"
            >
              <MessageSquare size={14} /> {submitLabel ?? i18nT('components.questionCard.submit')}
            </button>
          ) : (
            /* Gated on THIS question having an answer, so the card cannot be walked
               past a question without settling it — Submit's own `allAnswered` gate
               would otherwise be the first place a skipped question surfaced, at
               the far end of the card. The corner arrows stay ungated: they are
               review, not progress, and a user returning to check an earlier answer
               must not be held there.

               Focus is handed off only when the footer's primary control will be
               disabled on the page reached — Next there while its question is
               unanswered, or Submit on the last page while any question is.
               React reuses this node for Submit, so when the walk is complete
               focus rides along onto an enabled Submit and Enter finishes it;
               and on an answered middle page Next stays enabled under focus,
               which is what lets Enter keep walking. */
            <button
              onClick={() => {
                const landing = page + 1
                const primaryDisables = landing === questions.length - 1 ? !allAnswered : !isAnswered(landing)
                if (primaryDisables) goToHandingFocus(landing, prevArrowRef)
                else goTo(landing)
              }}
              disabled={busy || !isAnswered(page)}
              className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-md text-[13px] font-medium cursor-pointer transition-all disabled:opacity-30 disabled:cursor-not-allowed bg-accent text-accent-fg hover:bg-accent-hover border-none"
            >
              {i18nT('components.questionCard.next')} <ChevronRight size={14} />
            </button>
          )}
        </div>
      </div>
      {/* Dismiss is the only control that ends a question nobody is going to
          answer, so it has to say what it does: the label alone reads as "hide
          this for now" and a user who suspects it might throw the question away
          leaves a dead card parked above the composer instead. Rendered as a
          line rather than only as the button's title, because a tooltip does not
          exist for touch or for a keyboard user reading the row. */}
      {onDismiss && (
        <div className="px-4 pb-3 -mt-1.5 text-[12px] text-muted shrink-0 text-right">
          {i18nT('components.questionCard.dismiss_hint')}
        </div>
      )}
      </div>
    </Glass>
  )
}

export default memo(QuestionCard)
