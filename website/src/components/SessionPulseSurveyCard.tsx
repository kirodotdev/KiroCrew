import { useState, useEffect, useId } from 'react'
import { useQuery } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import { CheckCircle, ChevronRight, MessageSquare, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { ratingOptions } from './sessionPulseWireValues'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'

// Kiro Crew is self-hosted, open-source software — every install runs on its
// own arbitrary origin, which Aperture's browser-CORS allowlist model (a
// finite, known set of domains) cannot accommodate. These calls go to the
// Kiro Crew backend's own same-origin routes instead
// (src/kiro_crew/dashboard/handlers/feedback.py), which forward to Aperture
// server-to-server, where CORS does not apply.
const FEEDBACK_SUBMIT_URL = '/api/feedback/submit'
const FEEDBACK_ELIGIBLE_URL = '/api/feedback/eligible'

// Surface gate. The survey may only appear on an ORDINARY dashboard chat —
// never on an imported Slack thread, a task-runner slot, or an app/cron/system
// session.
//
// The discriminator is the slot's `origin`, stamped server-side by the only
// layer that knows how a session was created (`SlotOrigin` in
// dashboard/state.py) and serialized to the browser on the slot. Only "user"
// origin — a dashboard person creating a chat with no app token — qualifies.
// A caller-supplied slot key cannot spoof this: origin is set by the trusted
// request layer, not derived from the key shape.
const ORDINARY_CHAT_ORIGIN = 'user'

function isOrdinaryChatSession(origin: string | undefined): boolean {
  return origin === ORDINARY_CHAT_ORIGIN
}

const RATING_LABEL_KEYS: Record<string, string> = {
  'Very Poor': 'components.sessionPulseSurveyCard.rating_very_poor',
  Poor: 'components.sessionPulseSurveyCard.rating_poor',
  Fair: 'components.sessionPulseSurveyCard.rating_fair',
  Good: 'components.sessionPulseSurveyCard.rating_good',
  Excellent: 'components.sessionPulseSurveyCard.rating_excellent',
}

// How long the "Thanks for your feedback" confirmation stays visible after a
// successful submit, before the card fades away on its own.
const CONFIRMATION_DISPLAY_MS = 3000

// Aperture tracks its own per-user eligibility server-side, but its form-level
// cooldown isn't something we control from the client, and Mia wants a firm
// 30-day cooldown regardless of Aperture's configured default. We ask
// Aperture first (so it still gets accurate per-user, cross-device dedup
// data), then additionally require our own 30-day gate before showing —
// whichever check is stricter wins.
const COOLDOWN_KEY = 'kirocrew_survey_last_shown'
const COOLDOWN_DAYS = 30
// Minimum live (post-baseline) completed back-and-forth turns before the survey
// is eligible. A "turn" here is one user message answered by an assistant reply
// (counted in ChatPage as completedTurnCount), not a raw assistant message.
const MIN_LIVE_TURNS = 10

function localCooldownElapsed(): boolean {
  const lastShown = safeGetItem(COOLDOWN_KEY)
  if (!lastShown) return true
  const lastMs = new Date(lastShown).getTime()
  // A malformed/corrupt stored value must not disable the survey forever: a
  // NaN comparison is always false, which would permanently suppress it. Treat
  // an unparseable timestamp as "cooldown elapsed"; the next show overwrites it
  // with a clean value.
  if (Number.isNaN(lastMs)) return true
  const elapsed = Date.now() - lastMs
  return elapsed > COOLDOWN_DAYS * 24 * 60 * 60 * 1000
}

function markLocalCooldown(): boolean {
  return safeSetItem(COOLDOWN_KEY, new Date().toISOString())
}

// Identity is server-authoritative: the backend derives the per-install id
// (`beacon.install_id()`) and attaches it to every Aperture call, ignoring
// anything the client sends. The client therefore neither computes nor
// transmits an identity — it only asks whether the survey is due and submits
// answers. (The 30-day cooldown above is still per-browser localStorage; the
// server-side install id is what dedups a person across browsers/devices.)

/** Ask (via our own backend) whether Aperture considers this install due for
 * the survey. A failure of any kind (network, non-2xx) fails closed — don't
 * show the survey rather than guessing eligibility. */
async function checkSurveyEligible(): Promise<boolean> {
  try {
    const res = await fetch(FEEDBACK_ELIGIBLE_URL)
    if (!res.ok) return false
    const body = await res.json().catch(() => null)
    return body?.eligible === true
  } catch {
    return false
  }
}

interface SessionPulseSurveyCardProps {
  sessionId: string
  kiroCrewVersion: string
  turnCount: number
  /** The active slot's provenance (`ChatSlot.origin`). The survey only shows
   * when this is "user"; see `isOrdinaryChatSession`. Optional because a slot
   * mid-load may not have it yet, in which case the gate stays closed. */
  slotOrigin?: string
  /** Notifies the parent whenever the card's rendered height may have
   * changed — mount, unmount, expand/collapse, or the post-submit collapse
   * to the thank-you row — so it can re-anchor scroll position the same way
   * it does for other in-flow bands (see the activeTip re-anchor effect in
   * ChatPage). This component sits outside the virtualizer's measured rows,
   * so any height change here moves the scroll viewport's real content
   * height without the virtualizer knowing. Fires on every visible /
   * expanded / submitted transition, not just show/hide, because the
   * collapsed-by-default disclosure pattern below changes height on its own
   * while still mounted. */
  onLayoutChange?: () => void
}

export default function SessionPulseSurveyCard({
  sessionId,
  kiroCrewVersion,
  turnCount,
  slotOrigin,
  onLayoutChange,
}: SessionPulseSurveyCardProps) {
  const { t } = useTranslation()
  // Per-instance ids for the two labelled fields. A literal id would be unique
  // only for as long as at most one card is mounted, which today holds because
  // ChatPage gates the card on `!embedded && !popout`; the moment a second
  // surface may render one, duplicate ids would make each `htmlFor` /
  // `aria-labelledby` point at whichever copy mounted first. useId makes that
  // uniqueness structural instead of a property of the gate.
  const feedbackId = useId()
  const emailId = useId()
  const [visible, setVisible] = useState(false)
  // One-time latch: set the first time the card is shown, and never cleared
  // for the life of this mount. The eligibility query result (isEligible) is
  // cached `true` with staleTime Infinity, so without this latch the show
  // effect below re-fires every time `visible` flips back to false — which is
  // exactly what dismiss() and the post-submit auto-close do — and slams the
  // card straight back open. That made the dismiss (X) and the "Thanks"
  // auto-close appear dead, and re-showed the survey to the same identity.
  const [handled, setHandled] = useState(false)
  // Whether the card is showing its full form or just the slim trigger row.
  // Starts collapsed: the card should announce itself as one low-key line,
  // not open the entire rating/feedback/email form uninvited the instant it
  // becomes eligible (the "less invasive" redesign — see the collapsed
  // trigger row / expanded form / collapsed thank-you row below).
  const [expanded, setExpanded] = useState(false)
  const [selectedRating, setSelectedRating] = useState<string | null>(null)
  const [feedback, setFeedback] = useState('')
  const [email, setEmail] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submitted, setSubmitted] = useState(false)
  const [submitError, setSubmitError] = useState(false)
  // Baseline captured once per session mount (this component remounts on
  // session switch via `key={activeSlot}`). turnCount includes every
  // completed turn already loaded from history, so without a baseline,
  // reopening any session with prior turns past the threshold would pop the
  // survey on every visit — merely re-reading old messages, not a fresh
  // interaction. Only turns completed live past this baseline count toward
  // eligibility.
  const [baselineTurnCount] = useState(turnCount)
  const liveTurnCount = turnCount - baselineTurnCount
  // Without this guard, crossing the turn threshold re-fires the query on
  // every subsequent turn as long as the card stays hidden —
  // each one re-hitting /api/feedback/eligible for an answer that cannot
  // change mid-session. `enabled` covers that (React Query only fetches once
  // while the gate stays true and the result is cached), so no separate
  // "already checked" flag is needed the way the old effect-based version
  // required one.
  const eligibilityGate =
    isOrdinaryChatSession(slotOrigin) &&
    liveTurnCount >= MIN_LIVE_TURNS &&
    !visible &&
    localCooldownElapsed()
  const { data: isEligible } = useQuery({
    // Keyed on sessionId so an eligible result cached for one session is not
    // reused after switching to another (which would reopen the card and
    // attribute the submission to whichever session is active rather than the
    // one actually checked). Identity is server-side, so it is not in the key.
    queryKey: ['sessionPulseSurveyEligible', sessionId],
    queryFn: () => checkSurveyEligible(),
    enabled: eligibilityGate,
    staleTime: Infinity,
  })

  // Reacts to the query's own result rather than fetching itself, so there is
  // no cleanup-sets-cancelled race: React Query owns the fetch's lifecycle
  // (including in-flight cancellation on unmount/key change), and this effect
  // only ever reads settled data.
  useEffect(() => {
    // Gate the SHOW on the full eligibilityGate, not just the cached query
    // result. `isEligible` is cached with staleTime Infinity keyed by
    // session, so after a submit + session switch + return within the cache
    // lifetime the disabled query still yields a stale `true`; on remount
    // `handled` has reset, so without re-checking the gate here the card would
    // reopen and accept a second response inside the 30-day window. Requiring
    // `eligibilityGate` re-applies localCooldownElapsed() (and the turn/surface
    // checks) at show time, so a not-yet-elapsed cooldown keeps it shut.
    if (eligibilityGate && isEligible && !visible && !handled) {
      // Fail closed on unavailable storage: if the 30-day cooldown timestamp
      // cannot be written, do NOT show. A storage-denied browser would
      // otherwise reopen the card on every remount and burn no cooldown,
      // re-prompting the same person. `setHandled(true)` still fires so the
      // gate is not re-evaluated every render; the card simply stays closed.
      if (!markLocalCooldown()) {
        setHandled(true)
        return
      }
      setVisible(true)
      setHandled(true)
    }
  }, [eligibilityGate, isEligible, visible, handled])

  useEffect(() => {
    onLayoutChange?.()
  }, [visible, expanded, submitted, onLayoutChange])

  // Auto-hide the thank-you row after a successful submit. Keyed on
  // `submitted` so React clears the timer on unmount / session switch.
  useEffect(() => {
    if (!submitted) return
    const id = setTimeout(() => setVisible(false), CONFIRMATION_DISPLAY_MS)
    return () => clearTimeout(id)
  }, [submitted])

  const dismiss = () => setVisible(false)

  const submit = async () => {
    if (!selectedRating) return
    setSubmitting(true)
    setSubmitError(false)

    try {
      const res = await fetch(FEEDBACK_SUBMIT_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          rating: selectedRating,
          feedback: feedback.trim(),
          email: email.trim(),
          sessionId,
          kiroCrewVersion,
        }),
      })
      if (!res.ok) {
        // Aperture rejected the submission or is unavailable — keep the form
        // visible so the user can retry, rather than showing a false
        // confirmation and burning the 30-day cooldown on lost feedback.
        setSubmitting(false)
        setSubmitError(true)
        return
      }
    } catch {
      setSubmitting(false)
      setSubmitError(true)
      return
    }

    setSubmitting(false)
    setSubmitted(true)
    // Auto-hide is driven by an effect keyed on `submitted` (below) so its
    // timer is cleared on unmount — no setState-after-unmount if the user
    // switches sessions during the thank-you window.
  }

  return (
    <AnimatePresence>
      {visible && (
        <motion.div
          initial={{ opacity: 0, scale: 0.95 }}
          animate={{ opacity: 1, scale: 1 }}
          exit={{ opacity: 0, scale: 0.95 }}
          transition={{ type: 'spring', bounce: 0, duration: 0.28 }}
          className="border border-accent/30 rounded-xl bg-card shadow-sm mt-3 overflow-hidden"
        >
          {submitted ? (
            /* Collapsed thank-you row: the confirmation gets the same slim
             * footprint as the trigger row rather than lingering as a full
             * open card for CONFIRMATION_DISPLAY_MS. */
            <div className="flex items-center gap-2.5 px-4 py-2.5">
              <CheckCircle className="lucide-inline text-ok shrink-0" />
              <p className="flex-1 min-w-0 text-[13px] text-text">
                {t('components.sessionPulseSurveyCard.thanks')}
              </p>
              <button
                onClick={dismiss}
                aria-label={t('components.sessionPulseSurveyCard.dismiss')}
                className="bg-transparent border-none text-muted hover:text-text cursor-pointer shrink-0"
              >
                <X className="lucide-inline" />
              </button>
            </div>
          ) : (
            <>
              {/* Header row — also the expand/collapse toggle. This is the
               * whole card at rest: one line, same height as a line of
               * chat, until someone opts in by clicking it. */}
              <div className="flex items-center gap-2.5 px-4 py-2.5">
                <button
                  onClick={() => setExpanded((e) => !e)}
                  aria-expanded={expanded}
                  className="flex items-center gap-2.5 flex-1 min-w-0 text-left bg-transparent border-none cursor-pointer p-0"
                >
                  <MessageSquare className="lucide-inline text-accent shrink-0" />
                  <span className="flex-1 min-w-0 truncate text-[13px] font-medium text-text">
                    {t('components.sessionPulseSurveyCard.rating_question')}
                  </span>
                  <ChevronRight
                    className={`lucide-inline text-muted shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`}
                  />
                </button>
                <button
                  onClick={dismiss}
                  aria-label={t('components.sessionPulseSurveyCard.dismiss')}
                  className="bg-transparent border-none text-muted hover:text-text cursor-pointer shrink-0"
                >
                  <X className="lucide-inline" />
                </button>
              </div>

              {/* Expanded form. Collapsed by default (see `expanded` state
               * above) — clicking the header row again collapses it back
               * without submitting, so there's no separate Cancel button
               * duplicating that same action. */}
              <AnimatePresence initial={false}>
                {expanded && (
                  <motion.div
                    key="survey-form"
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: 'auto', opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    transition={{ duration: 0.2 }}
                    className="overflow-hidden"
                  >
                    <div className="px-4 pb-4">
                      {/* Rating */}
                      <div
                        className="mb-4 flex gap-2 flex-wrap"
                        role="radiogroup"
                        // The group owns the arrow keys, so it has to be able to
                        // hold focus; -1 keeps it out of the tab order, which
                        // belongs to the roving radio below.
                        tabIndex={-1}
                        aria-label={t('components.sessionPulseSurveyCard.rating_question')}
                        onKeyDown={(e) => {
                          const arrows = ['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp']
                          if (!arrows.includes(e.key)) return
                          e.preventDefault()
                          const forward = e.key === 'ArrowRight' || e.key === 'ArrowDown'
                          const idx = selectedRating ? ratingOptions.indexOf(selectedRating) : -1
                          const next =
                            idx < 0
                              ? forward
                                ? 0
                                : ratingOptions.length - 1
                              : (idx + (forward ? 1 : -1) + ratingOptions.length) %
                                ratingOptions.length
                          setSelectedRating(ratingOptions[next])
                          const radios =
                            e.currentTarget.querySelectorAll<HTMLButtonElement>('[role="radio"]')
                          radios[next]?.focus()
                        }}
                      >
                        {ratingOptions.map((option, i) => {
                          const checked = selectedRating === option
                          // Roving tabindex: Tab lands on the selected radio
                          // (or the first, when none is chosen yet); arrows move
                          // among the rest. This is the ARIA radiogroup contract
                          // the old `aria-pressed` toggle buttons only pretended
                          // to honour.
                          const roving = checked || (!selectedRating && i === 0) ? 0 : -1
                          return (
                            <button
                              key={option}
                              type="button"
                              role="radio"
                              aria-checked={checked}
                              tabIndex={roving}
                              onClick={() => setSelectedRating(option)}
                              className={`text-left px-3 py-2 rounded-lg text-[13px] cursor-pointer transition-all border font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-1 focus-visible:ring-offset-bg ${
                                checked
                                  ? 'border-accent text-text bg-accent-subtle/60'
                                  : 'border-border text-muted hover:text-text hover:border-accent/40 bg-bg font-normal'
                              }`}
                            >
                              {t(RATING_LABEL_KEYS[option])}
                            </button>
                          )
                        })}
                      </div>

                      {/* Data-egress disclosure, promoted to normal weight
                       * directly under the rating (not muted under the email
                       * field): on a self-hosted product a user must see that
                       * their rating + feedback leave for the Kiro Crew team
                       * BEFORE they submit, not read it as an email-only note. */}
                      <p className="mb-4 text-[12px] text-text">
                        {t('components.sessionPulseSurveyCard.email_disclosure')}
                      </p>

                      {/* Open feedback */}
                      <div className="mb-4">
                        {/* The question above IS the field's name: `htmlFor` makes
                            it click-to-focus, `aria-labelledby` makes it the
                            accessible name, so no second copy of the string. */}
                        <label
                          id={`${feedbackId}-label`}
                          htmlFor={feedbackId}
                          className="block text-[13px] text-text mb-2"
                        >
                          {t('components.sessionPulseSurveyCard.feedback_question')}
                        </label>
                        <textarea
                          id={feedbackId}
                          aria-labelledby={`${feedbackId}-label`}
                          value={feedback}
                          onChange={(e) => setFeedback(e.target.value)}
                          placeholder={t('components.sessionPulseSurveyCard.optional')}
                          className="w-full px-3 py-2 rounded-lg border border-border bg-bg text-text text-[13px] placeholder:text-muted focus:border-accent focus:outline-none resize-vertical min-h-[60px]"
                        />
                      </div>

                      {/* Email */}
                      <div className="mb-4">
                        <label
                          id={`${emailId}-label`}
                          htmlFor={emailId}
                          className="block text-[13px] text-text mb-2"
                        >
                          {t('components.sessionPulseSurveyCard.email_prompt')}
                        </label>
                        <input
                          id={emailId}
                          aria-labelledby={`${emailId}-label`}
                          type="email"
                          value={email}
                          onChange={(e) => setEmail(e.target.value)}
                          placeholder={t('components.sessionPulseSurveyCard.email_placeholder')}
                          className="w-full px-3 py-2 rounded-lg border border-border bg-bg text-text text-[13px] placeholder:text-muted focus:border-accent focus:outline-none"
                        />
                      </div>

                      {/* Submit */}
                      <div className="flex items-center justify-end gap-3">
                        {submitError && (
                          <p className="text-[12px] text-danger">
                            {t('components.sessionPulseSurveyCard.submit_error')}
                          </p>
                        )}
                        {!submitError && !selectedRating && (
                          <p className="text-[12px] text-muted">
                            {t('components.sessionPulseSurveyCard.select_rating_hint')}
                          </p>
                        )}
                        <button
                          onClick={submit}
                          disabled={!selectedRating || submitting}
                          className="px-3.5 py-1.5 rounded-md text-[13px] font-medium bg-accent text-accent-fg hover:bg-accent-hover border-none disabled:opacity-30 disabled:cursor-not-allowed cursor-pointer transition-all"
                        >
                          {submitting
                            ? t('components.sessionPulseSurveyCard.submitting')
                            : t('components.sessionPulseSurveyCard.submit')}
                        </button>
                      </div>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </>
          )}
        </motion.div>
      )}
    </AnimatePresence>
  )
}
