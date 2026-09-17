/**
 * Perpetual mode, on the crewmate's detail page (the crew editor's "what wakes
 * this crew" pane), above the schedules.
 *
 * Perpetual mode is the crewmate's restart policy, so it is a SETTING and
 * lives in the HR file next to Built from and the schedules. The owner's
 * switch is the one control: ON = the crewmate keeps waking on its own thread
 * with no cycle or time cap (it sets its own wake interval); OFF = it works
 * only when asked. The same switch is offered where a manager reads the state
 * on the Crewmates page (the side panel's Perpetual mode block, `MembersPage`)
 * -- both render `CrewPerpetualControl`, one request, one registry -- and the
 * reason a loop is off is read in both places; the readouts (interval, wakes,
 * last / next) and the hint live here, with the schedules.
 *
 * A refusal renders in plain words through `ErrorNotice` (the sentence comes
 * from `useCrewPerpetualSwitch`). The host remounts this section per crew
 * (`key={editing}`), so a pending press or an error for one crewmate never
 * shows on another.
 */
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { Goal } from 'lucide-react'
import { Skeleton } from '../ui'
import ErrorNotice from '../ErrorNotice'
import { intervalText, nextCycle } from '../autoNudgeLoop'
import { timeAgo } from '../../utils/timeAgo'
import { fmtDateTimeNumeric } from '../../i18n/format'
import { CrewPerpetualControl, useCrewPerpetualSwitch } from './CrewPerpetualControl'

/** The plain-words sentence for each coded stop the loop record can carry.
 *  An unknown code falls back to the code itself rather than to a sentence
 *  nothing produced (the Crewmates page keeps the same table). */
const STOPPED_REASON_KEY: Record<string, string> = {
  manual: 'components.crewPerpetualSection.off_by_you',
  autonudge_stop: 'components.crewPerpetualSection.off_by_crewmate',
  cycle_cap: 'pages.membersPage.patrol_stopped_cycle_cap',
  runtime_budget: 'pages.membersPage.patrol_stopped_runtime_budget',
  approval_stalled: 'pages.membersPage.patrol_stopped_approval_stalled',
  interrupted: 'pages.membersPage.patrol_stopped_interrupted',
  // The code the service writes for a stop a restart imposed (``_load``).
  interrupted_cycle: 'pages.membersPage.patrol_stopped_interrupted',
}

/** Clock for the "next wake" countdown. Coarse on purpose: this is an
 *  at-a-glance status line, not the popover's per-second readout. */
const TICK_MS = 15_000

export default function CrewPerpetualSection({
  crew,
  askAgent = false,
}: {
  crew: string
  /** Offer the refusal's "Ask the agent" hand-off. `ErrorNotice`'s opt-in
   *  contract: the hand-off navigates to the chat and unmounts the editor,
   *  so the host passes `true` only while it holds no unsaved pane edit and
   *  no open schedule draft (the same gate its own roster notices use). This
   *  section cannot see the sibling panes' drafts, so it defaults to off. */
  askAgent?: boolean
}) {
  const { t } = useTranslation()
  const reduceMotion = useReducedMotion()
  const sw = useCrewPerpetualSwitch(crew)
  const { loop, state, loaded, failed, monitor, enabled, threadClosed, refusalText } = sw

  const [nowTs, setNowTs] = useState(() => Date.now() / 1000)
  const ticking = state === 'on'
  useEffect(() => {
    if (!ticking) return
    setNowTs(Date.now() / 1000)
    const timer = setInterval(() => setNowTs(Date.now() / 1000), TICK_MS)
    return () => clearInterval(timer)
  }, [ticking])

  const stoppedReason = state === 'off' ? loop?.stopped_reason : undefined

  return (
    <section className="flex flex-col gap-2" data-testid="crew-perpetual-section" data-state={state}>
      <div className="flex flex-wrap items-center gap-2">
        <Goal
          size={14}
          className={`lucide-inline shrink-0 ${state === 'on' ? 'text-accent' : 'text-muted'}`}
          aria-hidden="true"
        />
        <h3
          id="crew-perpetual-title"
          className="m-0 min-w-0 truncate text-[12px] font-semibold uppercase tracking-wider text-muted"
        >
          {t('components.crewPerpetualSection.title')}
        </h3>
        {/* A thread never opened is the one refusal the client can foresee:
            the control shows disabled with that sentence as its description
            instead of being pressed into a 409. */}
        <CrewPerpetualControl
          sw={sw}
          testIdPrefix="crew-perpetual"
          describedBy={threadClosed ? 'crew-perpetual-hint crew-perpetual-thread-closed' : 'crew-perpetual-hint'}
        />
      </div>
      {/* What the switch does, stated once for both positions: ON is no cycle
          or time cap by design with the interval the crewmate's to adjust; OFF
          is plainly no wake on its own until it is turned on again. The
          second key names the save model by naming both controls: this
          switch writes on press, the editor's footer "Save changes" covers the
          fields it stages -- two saving ideas on one screen, so it says which
          is which. */}
      <p id="crew-perpetual-hint" className="m-0 text-[11.5px] leading-relaxed text-muted" data-testid="crew-perpetual-hint">
        {t('components.crewPerpetualSection.hint')} {t('components.crewPerpetualSection.applies_immediately')}
      </p>
      {threadClosed && (
        <p id="crew-perpetual-thread-closed" className="m-0 text-[11.5px] leading-relaxed text-muted" data-testid="crew-perpetual-thread-closed">
          {t('components.crewPerpetualSection.refused_thread_not_open')}
        </p>
      )}
      {/* The same refusal as the Crewmates page side panel, with the same
          hand-off -- but only when the host says nothing is at stake: the
          schedules pane below can hold an open, unsaved schedule draft this
          notice cannot see (see `askAgent`). */}
      <ErrorNotice
        variant="inline"
        title={t('components.crewPerpetualSection.change_failed')}
        message={refusalText}
        askAgent={askAgent}
        testId="crew-perpetual-error"
      />
      {!loaded ? (
        <Skeleton className="h-10" data-testid="crew-perpetual-loading" />
      ) : failed ? (
        // A read that never answered: never the affirmative "nothing wakes
        // this crewmate".
        <>
          {/* No hand-off: the schedules pane below can hold an open, unsaved
              schedule draft this notice cannot see. */}
          <ErrorNotice
            variant="inline"
            message={t('components.crewPerpetualSection.load_failed')}
            testId="crew-perpetual-load-error"
          />
        </>
      ) : (
        <AnimatePresence initial={false} mode="wait">
          {/* The verdict cross-fades on a state change: a stop that lands while
              the page is open must read as a change, not a flicker. */}
          <motion.div
            key={state}
            initial={reduceMotion ? false : { opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={reduceMotion ? { opacity: 0, transition: { duration: 0 } } : { opacity: 0 }}
            transition={reduceMotion ? { duration: 0 } : { duration: 0.18, ease: [0.2, 0, 0, 1] }}
            className="rounded-md border border-border bg-bg-accent px-3 py-2.5 text-[11.5px] leading-relaxed"
            data-testid="crew-perpetual-status"
            data-state={state}
          >
            {state === 'on' && loop ? (
              <>
                <div className="font-medium text-text-strong" data-testid="crew-perpetual-verdict">
                  {t('components.crewPerpetualSection.on_verdict')}
                </div>
                <dl className="m-0 mt-1.5 space-y-1 text-[11px]">
                  <div className="flex flex-col gap-0.5 sm:flex-row sm:gap-2">
                    <dt className="text-muted sm:w-24 sm:flex-none">{t('pages.membersPage.patrol_interval')}</dt>
                    <dd className="m-0 min-w-0 truncate" data-testid="crew-perpetual-interval">
                      {intervalText(loop.idle_secs)}
                    </dd>
                  </div>
                  <div className="flex flex-col gap-0.5 sm:flex-row sm:gap-2">
                    <dt className="text-muted sm:w-24 sm:flex-none">{t('pages.membersPage.patrol_cycles')}</dt>
                    <dd className="m-0 min-w-0 truncate" data-testid="crew-perpetual-cycles">
                      {loop.max_cycles > 0
                        ? t('pages.membersPage.patrol_cycles_of', { n: loop.cycle_count, max: loop.max_cycles })
                        : t('pages.membersPage.patrol_cycles_unlimited', { n: loop.cycle_count })}
                    </dd>
                  </div>
                  <div className="flex flex-col gap-0.5 sm:flex-row sm:gap-2">
                    <dt className="text-muted sm:w-24 sm:flex-none">{t('pages.membersPage.patrol_last_wake')}</dt>
                    <dd
                      className="m-0 min-w-0 truncate"
                      title={loop.last_fire_ts ? fmtDateTimeNumeric(loop.last_fire_ts) : undefined}
                      data-testid="crew-perpetual-last"
                    >
                      {loop.last_fire_ts ? timeAgo(loop.last_fire_ts) : t('components.autoNudgePopover.never')}
                    </dd>
                  </div>
                  <div className="flex flex-col gap-0.5 sm:flex-row sm:gap-2">
                    <dt className="text-muted sm:w-24 sm:flex-none">{t('pages.membersPage.patrol_next_wake')}</dt>
                    <dd
                      className="m-0 min-w-0 truncate"
                      title={loop.next_due_ts > 0 ? fmtDateTimeNumeric(loop.next_due_ts) : undefined}
                      data-testid="crew-perpetual-next"
                    >
                      {(() => {
                        const next = nextCycle(loop, nowTs)
                        switch (next.kind) {
                          case 'in':
                            return t('pages.membersPage.patrol_next_in', { time: next.time })
                          case 'due':
                            return t('components.autoNudgePopover.next_cycle_due')
                          default:
                            return t('components.autoNudgePopover.next_cycle_unscheduled')
                        }
                      })()}
                    </dd>
                  </div>
                </dl>
              </>
            ) : (
              <div className="text-muted">
                <span className="font-medium text-text-strong" data-testid="crew-perpetual-verdict">
                  {t('components.crewPerpetualSection.off_verdict')}
                </span>
                <span className="mt-0.5 block" data-testid="crew-perpetual-reason">
                  {!enabled
                    ? t('components.crewPerpetualSection.refused_autonudge_disabled')
                    : state === 'off' && stoppedReason
                      ? STOPPED_REASON_KEY[stoppedReason]
                        ? t(STOPPED_REASON_KEY[stoppedReason])
                        : stoppedReason
                      : state === 'off'
                        // A paused record with no reason at all: a row written
                        // before the field existed, or a torn write. Nothing
                        // recorded WHO stopped it, so it is not called the owner's.
                        ? t('components.crewPerpetualSection.off_no_reason')
                        : monitor
                          ? t('components.crewPerpetualSection.off_monitor_running')
                          : t('components.crewPerpetualSection.off_never_armed')}
                </span>
                {/* The crewmate's own words for a stop it chose (redacted and
                    capped by the server), under the coded reason. */}
                {state === 'off' && loop?.stopped_detail && (
                  <span className="mt-0.5 block italic" data-testid="crew-perpetual-detail">
                    {loop.stopped_detail}
                  </span>
                )}
              </div>
            )}
          </motion.div>
        </AnimatePresence>
      )}
    </section>
  )
}
