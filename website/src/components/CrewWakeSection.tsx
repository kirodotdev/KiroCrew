/**
 * The "what wakes this crew" section of the crew editor.
 *
 * Distinct from the Routing section's `triggers` field directly above it: that
 * field decides when the orchestrator PICKS this crew for a task a human already
 * started, while everything listed here starts a turn with no human present.
 * Users conflate the two, so the section carries a one-line disambiguator.
 *
 * Only clock triggers are listed. Webhook tokens carry their own crew binding
 * and are the webhook pane's answer (CrewWebhookSection), not a second row kind
 * here; a dashboard nudge loop is keyed by slot, not by crew, so listing it
 * would still be inventing an attribution the backend cannot answer.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Clock, Pause, Play, Zap, ExternalLink, AlarmClockOff, Plus, X } from 'lucide-react'
import { api } from '../api/client'
import { Badge, Btn, IconButton, SendBtn, Skeleton } from './ui'
import ErrorNotice from './ErrorNotice'
import { timeAgo } from '../utils/timeAgo'
import { fmtRelative } from '../i18n/format'
import type { CronJob } from '../types'
import { useCronActions } from '../hooks/useCronActions'
import { wakesCrew, crewWakeQueryKey } from './crew/wakesCrew'
import JobForm from './JobForm'
import { SaveCreateLabel } from '../utils/cronUtils'

import { i18nT } from '../i18n/t'

/** One schedule row. */
function WakeRow({ job, onChanged, dense = false }: { job: CronJob; onChanged: () => void; dense?: boolean }) {
  const { running, runNow, toggleEnabled, actionError } = useCronActions(onChanged)
  const isRunning = running.has(job.id) || !!job.is_running

  const last = job.last_run_ts ? timeAgo(job.last_run_ts) : null
  const next = job.enabled && job.next_run_ts ? fmtRelative(job.next_run_ts) : null
  const pauseLabel = job.enabled
    ? i18nT('components.crewWakeSection.pause_named', { name: job.name })
    : i18nT('components.crewWakeSection.resume_named', { name: job.name })
  const runLabel = i18nT('components.crewWakeSection.run_named_now', { name: job.name })
  // A paused job cannot be run, matching the Schedule page. Its own copy says
  // why, so the disabled control is not silent about the reason.
  const runTitle = job.enabled ? runLabel : i18nT('pages.schedulePage.resume_to_run')
  const rowError = actionError?.id === job.id ? actionError.msg : ''

  return (
    <div className="border-t border-border py-2 first:border-t-0" data-testid="wake-row">
      {/* Narrow-first: a 320px dialog cannot fit the badges, the schedule and two
          controls on one line, and the editor clips rather than scrolls. The two
          wrappers become `display: contents` at `sm`, so the wide layout is the
          same single flex row it was without them.
          `dense` pins the stacked layout ON, whatever the viewport. `sm:` asks
          about the VIEWPORT, and this section now also mounts in a ~460px panel
          column on a wide screen — where every `sm:` promotion fires and the
          one-line layout squeezes the job name down to about ten characters
          ("Nightly bac…"). A container query would answer the right question, but
          the row's siblings are all viewport-keyed, so a host that KNOWS it is
          narrow says so instead. */}
      <div className={dense ? 'flex flex-col gap-1.5' : 'flex flex-col gap-1.5 sm:flex-row sm:items-center sm:gap-3'}>
        <div className={dense ? 'flex w-full items-center gap-2' : 'flex w-full items-center gap-2 sm:contents'}>
          <Badge variant="muted" className="shrink-0 font-mono">
            <Clock className="lucide-inline" aria-hidden="true" />
            {i18nT('components.crewWakeSection.schedule')}
          </Badge>
          <div className="min-w-0 flex-1">
            <div className="truncate text-[12.5px] text-text-strong">{job.name}</div>
            {!job.member_id && <span className="text-[11px] text-muted">{i18nT('pages.kiroCrewAgentsPage.uses_global_memory_v1')}</span>}
            {(last || next) && (
              <div className="text-[10.5px] text-muted">
                {[last, next].filter(Boolean).join(' · ')}
              </div>
            )}
          </div>
        </div>
        <div className={dense ? 'flex w-full items-center gap-2' : 'flex w-full items-center gap-2 sm:contents'}>
          <span
            className={`min-w-0 flex-1 truncate font-mono text-[11px] text-muted${dense ? '' : ' sm:w-24 sm:flex-none'}`}
            title={job.schedule}
          >
            {job.schedule}
          </span>
          <Badge variant={isRunning ? 'aim' : job.enabled ? 'ok' : 'muted'} className="shrink-0">
            {isRunning
              ? i18nT('components.crewWakeSection.running')
              : job.enabled
                ? i18nT('components.crewWakeSection.active')
                : i18nT('components.crewWakeSection.paused')}
          </Badge>
          <div className="flex shrink-0 gap-1">
            <IconButton
              aria-label={pauseLabel}
              title={pauseLabel}
              onClick={() => toggleEnabled(job.id, !job.enabled)}
            >
              {job.enabled
                ? <Pause className="lucide-inline" aria-hidden="true" />
                : <Play className="lucide-inline" aria-hidden="true" />}
            </IconButton>
            <IconButton
              aria-label={runLabel}
              title={runTitle}
              disabled={!job.enabled || isRunning}
              onClick={() => runNow(job.id)}
            >
              <Zap className="lucide-inline" aria-hidden="true" />
            </IconButton>
          </div>
        </div>
      </div>
      {/* No hand-off: the section can host the inline create JobForm (`creating`
          in CrewWakeSection), whose unsaved fields this row cannot see. */}
      <ErrorNotice
        variant="inline"
        className="mt-1 pl-1"
        testId="crew-wake-row-error"
        message={rowError}
      />
    </div>
  )
}

/** Which schedules the section lists, as ONE question the host must answer.
 *
 *  A schedule carrying no `member_id` and no bound agent belongs to nobody, and
 *  `wakesCrew`'s last fallback hands it to the DEFAULT crew. Whether that is the
 *  right answer depends on what the host is asking:
 *
 *  - The crew editor asks what this crew will RUN, unowned work included, so it
 *    answers `isDefaultCrew` and gets the fallback.
 *  - The Crewmates panel asks what wakes THIS crewmate. A schedule with no
 *    crewmate is nobody's, so it is never claimed there whatever the default crew
 *    is; it stays on `/schedule`, the cross-crewmate view. That host sets
 *    `ownedOnly` instead.
 *
 *  A union rather than two independent booleans: a host that forgets
 *  `isDefaultCrew` would silently drop the default crew's unowned schedules, and
 *  a host that sets both would be stating two different scopes at once. */
type WakeScope =
  | { ownedOnly: true; isDefaultCrew?: never }
  | { ownedOnly?: false; isDefaultCrew: boolean }

export default function CrewWakeSection({ crew, memberId, agentTemplate, isDefaultCrew, ownedOnly = false, dense = false, heading, blurb, emptyLine, onDraftChange, onSavingChange, onRequestCancel }: WakeScope & {
  crew: string
  /** The crew's IMMUTABLE id (its slug), which is what a private schedule's
   *  `member_id` holds and what a new one is bound to. Separate from `crew`, the
   *  mutable display name, because the two differ for any crewmate whose name is not
   *  already its own slug — and conflating them made every such crewmate read as
   *  having no schedules. See `wakesCrew`. */
  memberId: string
  agentTemplate?: string
  /** Pin the rows' stacked layout on, for a host that is narrow on a wide screen
   *  (the Crewmates panel column). See `WakeRow`'s own note on `dense`. Also collapses the two
   *  header actions to icon-only, which `md:` would otherwise expand at exactly the
   *  width where they crowd out the heading. */
  dense?: boolean
  /** Section heading, when the host's word for this thing is not "agent". The crew
   *  editor is editing an AGENT and says so; the Crewmates panel is looking at a
   *  CREWMATE, and borrowing the editor's noun there makes the panel read as if it
   *  had wandered into a different product. */
  heading?: string
  /** The line under the heading. Overridden with it, for the same reason: the
   *  editor's copy disambiguates against the Triggers field sitting directly above
   *  it, which does not exist in the panel, so there it explains nothing. */
  blurb?: string
  /** What the empty state says. Same reason again, and the most-seen of the three:
   *  most crewmates have no schedules, so on the panel this is the line the reader
   *  usually gets, and the editor's wording calls the crewmate an "agent". */
  emptyLine?: string
  /** Reports whether the create form holds unsaved TYPED work, so the host
   *  editor can fold it into its own unsaved-state accounting (dirty dot,
   *  Save gating, discard confirms). Keyed on the form's own dirtiness, not
   *  on mere open-ness: opening the form to look and backing out is not work,
   *  and a "what you typed will be lost" confirm over nothing typed trains
   *  users to click through the confirm that guards real drafts. */
  onDraftChange?: (open: boolean) => void
  /** Reports whether the draft's create request is IN FLIGHT, so the host can
   *  lock its own draft-destruction paths (the discard confirm) for the same
   *  reason this section locks its toggle: a discard that unmounts the form
   *  does not cancel the request, so the "discarded" schedule would persist. */
  onSavingChange?: (saving: boolean) => void
  /** Asks the host to confirm cancelling a DIRTY draft before this section
   *  collapses it. The toggle is the one destruction path the host cannot
   *  see — at narrow widths it renders as a bare icon-only X, where a single
   *  misclick would otherwise erase everything typed with no confirm while
   *  every sibling path (rail, Escape, chat) asks first. `proceed` performs
   *  the collapse; the host calls it only when the user confirms. Without a
   *  host (Schedule-page-less embeds, tests), the toggle collapses directly. */
  onRequestCancel?: (proceed: () => void) => void
}) {
  const navigate = useNavigate()
  const [creating, setCreatingState] = useState(false)
  const [savingDraft, setSavingDraftState] = useState(false)
  const submitRef = useRef<(() => void) | null>(null)
  const addBtnRef = useRef<HTMLButtonElement | null>(null)
  // Open/close is NOT reported as draft state: the host hears about typed
  // work through JobForm's onDirtyChange below, so a pristine form never
  // arms a discard confirm. The flag is teed locally too: the toggle needs
  // to know whether collapsing would destroy typed work.
  const setCreating = setCreatingState
  const draftDirty = useRef(false)
  const reportDirty = useCallback((d: boolean) => {
    draftDirty.current = d
    onDraftChange?.(d)
  }, [onDraftChange])
  const setSavingDraft = useCallback((v: boolean) => {
    setSavingDraftState(v)
    onSavingChange?.(v)
  }, [onSavingChange])
  // Focus follows the surface that appeared: into the form's first field on
  // expand, back to the toggle on collapse — otherwise a collapse-after-save
  // unmounts the focused Create button and focus falls to the body.
  const everOpened = useRef(false)
  useEffect(() => {
    if (creating) { everOpened.current = true; document.getElementById('jobform-name')?.focus() }
    else if (everOpened.current) addBtnRef.current?.focus()
  }, [creating])
  // Switching panes unmounts this section and its form state with it — the
  // draft no longer exists, so the host must not keep accounting for it (a
  // stale flag would leave the editor's Save disabled with nothing to finish).
  // The callback rides a ref so this runs on UNMOUNT only: keyed on the
  // callback's identity, an inline-arrow host would re-run the cleanup every
  // render and falsely clear a live draft. The saving flag clears for the
  // same reason: stranded true, it would lock the host's discard paths with
  // no request left to protect.
  const draftChangeRef = useRef(onDraftChange)
  draftChangeRef.current = onDraftChange
  const savingChangeRef = useRef(onSavingChange)
  savingChangeRef.current = onSavingChange
  useEffect(() => () => { draftChangeRef.current?.(false); savingChangeRef.current?.(false) }, [])
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: crewWakeQueryKey(crew),
    queryFn: () => api.crons(),
  })
  const jobs: CronJob[] = (data?.jobs || []).filter((j: CronJob) => wakesCrew(j, crew, !ownedOnly && !!isDefaultCrew, memberId))
  const onChanged = useCallback(() => { void refetch() }, [refetch])
  const onCreated = useCallback(() => {
    // The saved job should be visible where it was made: close the form and
    // let the refreshed list carry the evidence that the save happened. The
    // form clears its own saving flag before calling this (it reports
    // saving=false on every outcome), so there is nothing to unlearn here.
    setCreating(false)
    void refetch()
  }, [refetch, setCreating])

  // A failed fetch leaves `jobs` empty, which would otherwise render the
  // affirmative "nothing wakes this crew" — a false statement about the crew
  // rather than a report about the request. Absence of an answer and an answer
  // of "none" are different things and must not render the same.
  const body = isLoading
    ? <Skeleton className="h-12" />
    : isError
      ? (
        // No hand-off while `creating`: the inline JobForm's unsaved fields
        // would be lost. With the form closed nothing in the section is a
        // draft, so the read failure gets the hand-off. Retry sits beside it,
        // matching the sibling webhooks section on the same page.
        <div className="flex items-center gap-2">
          <ErrorNotice
            askAgent={!creating}
            testId="crew-wake-load-error"
            className="flex-1"
            message={i18nT('components.crewWakeSection.could_not_load_this_crew_s_schedules_so_what_wak')}
          />
          <Btn onClick={() => { void refetch() }}>{i18nT('components.crewWakeSection.retry')}</Btn>
        </div>
      )
      : jobs.length === 0
        ? (
          <div className="flex items-center gap-2 rounded-md border border-border bg-bg-accent px-3 py-2.5 text-[11.5px] leading-relaxed text-muted">
            <AlarmClockOff className="lucide-inline shrink-0" aria-hidden="true" />
            {emptyLine ?? i18nT('components.crewWakeSection.no_schedules_run_this_crew_automatically')}
          </div>
        )
        : <div>{jobs.map(j => <WakeRow key={j.id} job={j} onChanged={onChanged} dense={dense} />)}</div>

  return (
    <section className="flex flex-col gap-3" data-testid="crew-wake-section">
      <div className="flex items-center gap-2">
        <h3 className="text-[12px] font-semibold uppercase tracking-wider text-muted">{heading ?? i18nT('components.crewWakeSection.what_wakes_this_crew')}</h3>
        <div className="ml-auto flex items-center gap-1.5">
          {/* Below `md` the editor pane runs as narrow as ~216px (320px
              viewport), where the heading plus two intrinsic-width buttons
              overflow the pane's `overflow-x-hidden` — so both actions
              collapse to icon-only there, with the full name riding
              aria-label and title. The open-form label is the FULL "Cancel
              new schedule": the dialog footer's own Cancel (close the whole
              editor) is on screen at the same time, and two controls named
              identically with different blast radii is the trap. */}
          <Btn
            ref={addBtnRef}
            onClick={() => {
              // Collapsing a DIRTY draft is destruction: route it through the
              // host's confirm like every sibling path. A clean form (or a
              // hostless mount) collapses directly.
              if (creating && draftDirty.current && onRequestCancel) {
                onRequestCancel(() => setCreating(false))
                return
              }
              setCreating(!creating)
            }}
            data-testid="crew-wake-add"
            aria-expanded={creating}
            aria-controls={creating ? 'crew-wake-create' : undefined}
            // Cancelling while the create request is IN FLIGHT would unmount
            // the form without cancelling the POST: the "discarded" schedule
            // then persists server-side. Every destruction path locks on
            // savingDraft (this toggle here; the host's discard confirm via
            // onSavingChange).
            disabled={creating && savingDraft}
            aria-label={creating
              ? i18nT('components.crewWakeSection.cancel_new_schedule')
              : i18nT('components.crewWakeSection.new_schedule')}
            title={creating && savingDraft
              ? i18nT('components.jobForm.saving')
              : creating
                ? i18nT('components.crewWakeSection.cancel_new_schedule')
                : i18nT('components.crewWakeSection.new_schedule')}
          >
            {creating
              ? <X className="lucide-inline" aria-hidden="true" />
              : <Plus className="lucide-inline" aria-hidden="true" />}
            <span className={dense ? 'hidden' : 'hidden md:inline'}>
              {creating
                ? i18nT('components.crewWakeSection.cancel_new_schedule')
                : i18nT('components.crewWakeSection.new_schedule')}
            </span>
          </Btn>
          {/* One creation path at a time: while the inline form is open the
              jump to the Schedule page is hidden — it navigates away and would
              silently discard everything typed. */}
          {!creating && (
            <Btn
              onClick={() => navigate('/schedule')}
              aria-label={i18nT('components.crewWakeSection.open_schedule')}
              title={i18nT('components.crewWakeSection.open_schedule')}
            >
              <ExternalLink className="lucide-inline" aria-hidden="true" />
              <span className={dense ? 'hidden' : 'hidden md:inline'}>
                {i18nT('components.crewWakeSection.open_schedule')}
              </span>
            </Btn>
          )}
        </div>
      </div>
      <p className="m-0 text-[11.5px] leading-relaxed text-muted">{blurb ?? i18nT('components.crewWakeSection.schedules_that_run_this_crew_without_you_asking')}</p>
      {creating && (
        <div
          id="crew-wake-create"
          className="rounded-md border border-border bg-bg-accent p-3"
          data-testid="crew-wake-create"
        >
          {/* Create sits in the card header, always visible: the form is taller
              than the pane, and a submit that only exists below the fold loses
              to the sticky dialog footer's disabled Save changes as the thing
              the eye lands on. */}
          <div className="mb-3 flex items-center justify-between gap-2">
            <span className="text-[12px] font-medium text-text-strong">
              {i18nT('components.crewWakeSection.new_schedule')}
            </span>
            <SendBtn onClick={() => submitRef.current?.()} disabled={savingDraft} data-testid="crew-wake-create-submit">
              <SaveCreateLabel isEdit={false} saving={savingDraft} />
            </SendBtn>
          </div>
          {/* The Schedule page's own create form, pinned to this crew: same
              fields, same validation, same POST — only the crew picker is a
              fixed value, because filing the job on another crew from inside
              this crew's editor would be the mistake, not a choice. */}
          <JobForm
            layout="vertical"
            agents={[]}
            defaultAgent=""
            lockedAgent={crew}
            /* The NAME, not `memberId`. `memberId` is the read-side matcher: it is
               the crewmate's immutable id, which is what a stored `member_id` holds
               once a job has run. But for a crewmate whose id was never persisted,
               that id is DERIVED from the name and slugification is lossy, so two
               crewmates can share one derived id. A created job keeps the client's
               string until its first run, and member resolution matches an agent NAME
               before a stored id -- so submitting a derived id could file the schedule
               against a different crewmate that happens to be named it. The name is
               the roster row's own identity and cannot collide that way. Unconditional:
               JobForm applies the default-crew rule itself. */
            memberId={crew}
            providerAgent={agentTemplate}
            onSaved={onCreated}
            externalSubmit
            submitRef={submitRef}
            onSavingChange={setSavingDraft}
            onDirtyChange={reportDirty}
          />
        </div>
      )}
      {body}
    </section>
  )
}
