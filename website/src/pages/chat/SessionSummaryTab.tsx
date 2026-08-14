import { useCallback, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ChevronRight, ListChecks, ListTree, RefreshCw, Clock, RotateCcw, MoveUpRight, Sparkles, Loader2 } from 'lucide-react'

import { api, ApiError } from '../../api/client'
import { parseErrorCode } from '../../utils/errorReport'
import { i18nT } from '../../i18n/t'
import { fmtRelative } from '../../i18n/format'
import { safeGetItem, safeSetItem } from '../../utils/safeStorage'
import { Btn, PanelSectionHeader } from '../../components/ui'
import { useAppSelector } from '../../store'
import { selectSlotStreamState } from '../../store/chatSlice'
import {
  collectTriage,
  TRIAGE_VISIBLE,
  formatRanges,
  resumptionCount,
  type IntentState,
  type SessionIntent,
  type SessionSummary,
  type TriageItem,
} from '../../types/sessionSummary'

/**
 * The session summary panel: a goal-level view of a session so returning to it
 * does not mean re-reading the transcript.
 *
 * Two ordering decisions carry the design, and both come from prototyping the
 * summary against real transcripts:
 *
 * - **Intents are ordered by last touched, descending** — not chronologically.
 *   Chronology is what a reader wants; recency is what a returning worker wants,
 *   and it buries the live intent at the bottom. Ordering by state also means a
 *   resumed intent simply rises, with no special case.
 * - **What needs the person is hoisted above the list**, across every intent, so
 *   "does this session need me?" is answered without scrolling.
 *
 * The panel never triggers generation — see `api.sessionSummary`.
 */

const OPEN_KEY_PREFIX = 'mc-summary-open:'
const NOTES_KEY_PREFIX = 'mc-summary-notes:'
const TRIAGE_KEY_PREFIX = 'mc-summary-triage:'

/** Per-slot disclosure state. Persisted so re-opening the panel does not undo
 *  the reader's own collapsing, matching how the panel tab strip persists.
 *  Reads go through `safeGetItem`: a bare `localStorage.getItem` throws
 *  SecurityError when storage is denied (Safari private mode, blocked
 *  third-party cookies), and inside a useState initializer that throw takes
 *  the whole panel down rather than degrading to the default. */
function loadOpen(slot: string): Record<string, boolean> {
  try {
    const raw = safeGetItem(OPEN_KEY_PREFIX + slot)
    const parsed = raw ? JSON.parse(raw) : null
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function loadNotesOpen(slot: string): boolean {
  // Collapsed by default: the notes are durable background facts, not the
  // answer to "does this session need me?". Open they push the first intent
  // card down the panel, so the reader pays for them on every visit while
  // needing them on few. The count in the header is what advertises them.
  return safeGetItem(NOTES_KEY_PREFIX + slot) === '1'
}

/** Separator for the composite triage key. NUL cannot occur in a title or a
 *  step, so no pair of real values can collide by straddling it. */
const KEY_SEP = '\u0000'

/** Per-item triage disclosure, same persistence model as the intent cards.
 *  Keyed by source intent AND step text: two intents can legitimately carry the
 *  same next step ("run the tests"), and keying on the text alone would make one
 *  headline's chevron expand both. Keys for steps that later vanish are inert,
 *  exactly as the intent map already retains a dropped intent's title. */
function triageKey(item: TriageItem): string {
  return [item.fromIntent, item.what].join(KEY_SEP)
}

function loadTriageOpen(slot: string): Record<string, boolean> {
  try {
    const raw = safeGetItem(TRIAGE_KEY_PREFIX + slot)
    const parsed = raw ? JSON.parse(raw) : null
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

const STATE_LABEL: Record<IntentState, string> = {
  'done': 'pages.chat.sessionSummary.state_done',
  'needs-you': 'pages.chat.sessionSummary.state_needs_you',
  'in-progress': 'pages.chat.sessionSummary.state_active',
  'dropped': 'pages.chat.sessionSummary.state_dropped',
}

/** One derived word per intent, never two competing badges — progress and
 *  verification are two fields in the data resolved into one state server-side. */
function StateChip({ state }: { state: IntentState }) {
  const tone =
    state === 'needs-you'
      ? 'bg-warn-subtle border-warn/40 text-warn'
      : state === 'done'
        ? 'bg-ok-subtle border-ok/45 text-ok'
        : state === 'in-progress'
          ? 'bg-accent-subtle border-accent/45 text-accent'
          : 'border-border-strong text-muted-strong'
  return (
    <span
      className={`inline-flex items-center gap-1 text-[11px] px-[7px] py-px rounded-full border whitespace-nowrap ${tone}`}
    >
      {i18nT(STATE_LABEL[state])}
    </span>
  )
}

/** The person's own words sit behind a neutral rule; anything the summarizer
 *  inferred sits behind an accent one. Every next step in the prototype was an
 *  inference, so unlabelled the panel would put words in the reader's mouth. */
function AskedBlock({ text }: { text: string }) {
  return <div className="border-l-2 border-border-strong pl-[9px] text-text">{text}</div>
}

function SuggestedStep({ what, why, expect }: { what: string; why: string; expect: string }) {
  return (
    <div className="border-l-2 border-accent pl-[9px]">
      <div className="text-[13px] text-text">{what}</div>
      {why && <div className="text-[12px] text-muted mt-0.5">{why}</div>}
      {expect && <div className="text-[11px] text-muted-strong mt-[3px] italic">{expect}</div>}
    </div>
  )
}

function IntentCard({
  intent,
  open,
  onToggle,
}: {
  intent: SessionIntent
  open: boolean
  onToggle: () => void
}) {
  const resumed = resumptionCount(intent)
  const gist = intent.progress[0] || intent.initial_intent
  return (
    <div
      className={`border rounded-md mb-[7px] ${open ? 'bg-card border-border-strong' : 'bg-bg-accent border-border'}`}
    >
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="w-full text-left px-[11px] py-[9px] flex items-start gap-2 rounded-md hover:bg-bg-hover"
      >
        <span className="flex-1 min-w-0">
          <span className="block text-[13px] font-semibold text-text-strong leading-[1.35]">
            {intent.title}
          </span>
          <span className="flex items-center gap-2 mt-[3px] flex-wrap">
            <StateChip state={intent.state} />
            <span className="text-[11px] text-muted-strong font-mono">
              {i18nT('pages.chat.sessionSummary.turns', { ranges: formatRanges(intent.ranges) })}
            </span>
            {resumed > 0 && (
              <span className="text-[11px] text-accent flex items-center gap-1">
                <RotateCcw className="lucide-inline" />
                {i18nT('pages.chat.sessionSummary.returned_to', { times: resumed })}
              </span>
            )}
          </span>
          {!open && gist && <span className="block text-[12px] text-muted mt-1">{gist}</span>}
        </span>
        <ChevronRight
          className={`lucide-inline shrink-0 mt-0.5 text-muted-strong transition-transform ${open ? 'rotate-90' : ''}`}
        />
      </button>

      {open && (
        <div className="px-[11px] pb-[11px]">
          {intent.initial_intent && (
            <div className="mt-0.5">
              <PanelSectionHeader label={i18nT('pages.chat.sessionSummary.you_asked_for')} />
              <div className="mt-1">
                <AskedBlock text={intent.initial_intent} />
              </div>
              {intent.origin_turn !== null && (
                <div className="text-[11px] text-muted-strong mt-[5px] flex items-start gap-[5px] italic">
                  <MoveUpRight className="lucide-inline shrink-0" />
                  {i18nT('pages.chat.sessionSummary.pivoted_from_turn', { turn: intent.origin_turn })}
                </div>
              )}
            </div>
          )}

          {intent.progress.length > 0 && (
            <div className="mt-2.5">
              <PanelSectionHeader label={i18nT('pages.chat.sessionSummary.where_it_stands')} />
              <ul className="mt-1.5 pl-4 list-disc">
                {intent.progress.map((line, i) => (
                  <li key={i} className="text-[13px] my-[3px] text-text">
                    {line}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {intent.next_steps.length > 0 ? (
            <div className="mt-2.5">
              <PanelSectionHeader label={i18nT('pages.chat.sessionSummary.suggested_next')} />
              <div className="mt-1.5 space-y-2.5">
                {intent.next_steps.map((step, i) => (
                  <SuggestedStep key={i} {...step} />
                ))}
              </div>
            </div>
          ) : (
            <div className="mt-2.5 text-[12px] text-muted">
              {i18nT('pages.chat.sessionSummary.nothing_outstanding')}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function SessionSummaryTab({ slot }: { slot: string }) {
  const { data, isLoading, error, refetch, isFetching } = useQuery<SessionSummary>({
    queryKey: ['session-summary', slot],
    queryFn: () => api.sessionSummary(slot),
    enabled: !!slot,
    // No polling: the summary is regenerated at turn end and pushed over the
    // websocket. A poll here would reward refreshing, which is the behaviour
    // this panel exists to remove.
    refetchOnWindowFocus: false,
    retry: false,
  })

  const [openMap, setOpenMap] = useState<Record<string, boolean>>(() => loadOpen(slot))
  const [notesOpen, setNotesOpen] = useState<boolean>(() => loadNotesOpen(slot))
  const [triageOpen, setTriageOpen] = useState<Record<string, boolean>>(() => loadTriageOpen(slot))
  // Generation is the one thing this panel does that spends money, so its
  // in-flight and failure states are local rather than folded into the query's:
  // `isFetching` already means "re-reading the sidecar", which is free, and a
  // spinner that means two different things is how a person learns to distrust
  // it. Keyed to nothing — a slot switch unmounts the tab.
  const [generating, setGenerating] = useState(false)
  const [generateError, setGenerateError] = useState<string | null>(null)
  // A turn in flight has no boundary worth summarizing, and the backend refuses
  // one. Read it from the store rather than the payload: the summary query is
  // invalidated only when a summary is WRITTEN, so a server-sent flag would
  // still say "running" after a turn that ended without producing one. This
  // selector is live, so the button re-enables the moment the turn ends.
  const turnRunning = useAppSelector(s => selectSlotStreamState(s, slot) !== 'idle')
  const qc = useQueryClient()

  const onGenerate = useCallback(async () => {
    setGenerating(true)
    setGenerateError(null)
    try {
      // Seed the query from the POST's own body BEFORE reconciling. The person
      // has already paid for this summary, so it must not depend on a second
      // network call succeeding: if the refetch below fails, an unseeded cache
      // leaves the query in its error state and the panel renders "could not
      // load the summary" for a summary that exists and was just generated. The
      // POST returns the GET's body shape precisely so this is safe.
      const fresh = await api.generateSessionSummary(slot)
      qc.setQueryData(['session-summary', slot], fresh)
    } catch (e) {
      const code = e instanceof ApiError ? parseErrorCode(e.body) : undefined
      // Map the backend's machine-readable code to a localized string. The
      // response also carries English prose, but showing that would put an
      // untranslatable sentence in a dashboard that ships in 12 languages.
      // `summary_turn_running` is reachable despite the disabled button: the
      // store's per-slot state falls back to idle for a slot whose turn started
      // somewhere this client never saw, so the server stays the authority and
      // the panel reports the same reason the tooltip gives.
      setGenerateError(
        code === 'summary_in_flight'
          ? i18nT('pages.chat.sessionSummary.generate_in_flight')
          : code === 'summary_turn_running'
            ? i18nT('pages.chat.sessionSummary.generate_turn_running')
            : i18nT('pages.chat.sessionSummary.generate_failed'),
      )
    } finally {
      setGenerating(false)
      // Re-read on SETTLEMENT, not just on success -- one refetch covers both
      // outcomes. On success it is how the new summary arrives: refetching
      // rather than writing the POST's body into the cache keeps the GET as the
      // single shape the rest of the panel reads, which survives a future field
      // being added to only one of the two. On failure it is equally news about
      // server state -- `summary_disabled` means the feature was turned off
      // while this panel sat here, and refetching only on success would leave
      // the button offering an action the backend now rejects. The failure line
      // still says what happened; this makes the affordance agree with it.
      // Swallow a refetch error: the generate outcome is what the person asked
      // about, and the query keeps its own error state for the rest.
      try {
        await refetch()
      } catch {
        /* the query keeps its own error state */
      }
    }
  }, [slot, refetch, qc])

  const toggleIntent = useCallback(
    (key: string, currentlyOpen: boolean) => {
      // The caller passes the EFFECTIVE open state, because the render default
      // ("first card is open") is not the same as an absent map entry. Deriving
      // it here from the map alone would make the first click on the first card
      // a no-op: it would flip an absent entry to `true` while the card was
      // already rendering open.
      setOpenMap(prev => {
        const next = { ...prev, [key]: !currentlyOpen }
        safeSetItem(OPEN_KEY_PREFIX + slot, JSON.stringify(next))
        return next
      })
    },
    [slot],
  )

  const toggleNotes = useCallback(() => {
    setNotesOpen(prev => {
      safeSetItem(NOTES_KEY_PREFIX + slot, prev ? '0' : '1')
      return !prev
    })
  }, [slot])

  const toggleTriage = useCallback(
    (key: string) => {
      // Absent means collapsed here, so unlike the intent cards the current
      // state is derivable from the map alone and needs no caller-passed hint.
      setTriageOpen(prev => {
        const next = { ...prev, [key]: prev[key] !== true }
        safeSetItem(TRIAGE_KEY_PREFIX + slot, JSON.stringify(next))
        return next
      })
    },
    [slot],
  )

  // Memoized so the `??` fallback does not produce a new array identity every
  // render, which would defeat the triage memo below.
  const intents = useMemo(() => data?.intents ?? [], [data])
  // Collect ALL open items, then cap what is rendered. The chip counts the full
  // set: capping the count made the header understate the busiest sessions, and
  // the block is read as the complete answer to "does this need me?".
  const allOpen = useMemo(
    () => collectTriage(intents, Number.POSITIVE_INFINITY),
    [intents],
  )
  const triage = useMemo(() => allOpen.slice(0, TRIAGE_VISIBLE), [allOpen])
  const hiddenOpen = allOpen.length - triage.length

  if (isLoading) {
    return (
      <Centered>
        <RefreshCw className="lucide-inline animate-spin" />
        {i18nT('pages.chat.sessionSummary.loading')}
      </Centered>
    )
  }

  // `error && !data` — NOT `error` alone. React Query keeps the last good `data`
  // when a REFETCH fails, so testing the error flag by itself replaces a summary
  // that is sitting right there with a load-failure screen. That is reachable
  // three ways: a websocket invalidation refetching while the network is down, a
  // reconciling read after a successful generate, and the manual reload button.
  // In all three the honest state is the summary the person already has. The
  // failure screen belongs to the case with genuinely nothing to render.
  if (error && !data) {
    // Give the failure the same icon + title + body shape the off and
    // not-generated states use, and a Retry. A failure needs at least the weight
    // of the two harmless empty states: it is the one state with something to
    // recover. The header's reload button is not a substitute — this branch
    // returns before the header renders, so this Retry is the only control that
    // can fix it.
    return (
      <Centered>
        <AlertTriangle className="lucide-inline text-danger" />
        <div className="text-text">{i18nT('pages.chat.sessionSummary.failed_title')}</div>
        <div className="text-[12px] text-muted max-w-[280px] text-center">
          {i18nT('pages.chat.sessionSummary.failed')}
        </div>
        <Btn
          onClick={() => refetch()}
          className="mt-1 text-[12px] border-border-strong bg-card"
        >
          <RefreshCw className={`lucide-inline ${isFetching ? 'animate-spin' : ''}`} />
          {i18nT('pages.chat.sessionSummary.retry')}
        </Btn>
      </Centered>
    )
  }

  if (data && !data.enabled) {
    return (
      <Centered>
        <ListTree className="lucide-inline" />
        <div className="text-text">{i18nT('pages.chat.sessionSummary.off_title')}</div>
        <div className="text-[12px] text-muted max-w-[280px] text-center">
          {i18nT('pages.chat.sessionSummary.off_body')}
        </div>
      </Centered>
    )
  }

  if (intents.length === 0) {
    // Three states, because "no summary" has three different causes and only one
    // of them is actionable. `generate_state` is the server's verdict, absent on
    // a gateway that predates the POST route — treated as `unavailable`, which
    // degrades to the read-only behaviour this panel shipped with.
    const gen = data?.generate_state ?? 'unavailable'
    return (
      <Centered>
        <ListTree className="lucide-inline" />
        <div className="text-text">{i18nT('pages.chat.sessionSummary.empty_title')}</div>
        {gen === 'ready' ? (
          <>
            <div className="text-[12px] text-muted max-w-[280px] text-center">
              {i18nT('pages.chat.sessionSummary.empty_generate_body')}
            </div>
            {/* Sparkles, not the refresh glyph the sibling button uses: this
                click CREATES a summary rather than re-reading one, and an icon
                that says "reload" next to a label that says "summarize" makes
                the reader trust neither. Loader2 while it runs, matching how the
                rest of the dashboard renders an in-flight action. */}
            {/* The tooltip is on a WRAPPER, not on the button. A disabled button
                receives no pointer events, so Chrome and Safari never surface a
                `title` set on it — the span is the hover target that survives
                the disabled state. Set only while blocked: an always-on tooltip
                on a button that works is noise. */}
            <span
              className="mt-1 inline-flex"
              title={
                turnRunning
                  ? i18nT('pages.chat.sessionSummary.generate_turn_running')
                  : undefined
              }
            >
              <Btn
                onClick={onGenerate}
                disabled={generating || turnRunning}
                className="text-[12px] border-border-strong bg-card"
              >
                {generating
                  ? <Loader2 className="lucide-inline animate-spin" />
                  : <Sparkles className="lucide-inline" />}
                {generating
                  ? i18nT('pages.chat.sessionSummary.generating')
                  : i18nT('pages.chat.sessionSummary.generate')}
              </Btn>
            </span>
          </>
        ) : gen === 'too_few_turns' ? (
          // No button at all: the only honest affordance for a session with
          // nothing to summarize is a sentence saying so. A disabled button
          // invites hunting for the thing that would enable it.
          <div className="text-[12px] text-muted max-w-[280px] text-center">
            {i18nT('pages.chat.sessionSummary.empty_too_few')}
          </div>
        ) : (
          <>
            <div className="text-[12px] text-muted max-w-[280px] text-center">
              {i18nT('pages.chat.sessionSummary.empty_body')}
            </div>
            {/* Refresh, not generate: recovers the case where a summary was
                written while the panel sat here and the invalidation was
                missed. */}
            <Btn
              onClick={() => refetch()}
              className="mt-1 text-[12px] border-border-strong bg-card"
            >
              <RefreshCw className={`lucide-inline ${isFetching ? 'animate-spin' : ''}`} />
              {i18nT('pages.chat.sessionSummary.reload')}
            </Btn>
          </>
        )}
        {generateError && (
          <div className="text-[11px] text-warn-strong max-w-[280px] text-center">
            {generateError}
          </div>
        )}
      </Centered>
    )
  }

  return (
    <div className="absolute inset-0 flex flex-col">
      <div className="flex items-center gap-2 px-3 py-2.5 border-b border-border shrink-0">
        <span className="text-[13px] font-semibold text-text-strong">
          {i18nT('pages.chat.sessionSummary.title')}
        </span>
        <span className="flex-1" />
        {triage.length > 0 && (
          <span className="inline-flex items-center gap-1 text-[11px] px-[7px] py-px rounded-full border bg-bg-accent border-border-strong text-muted-strong whitespace-nowrap">
            {i18nT('pages.chat.sessionSummary.open_items', { count: allOpen.length })}
          </span>
        )}
        <button
          type="button"
          onClick={() => refetch()}
          aria-label={i18nT('pages.chat.sessionSummary.reload')}
          className="w-7 h-7 rounded-md grid place-items-center text-muted hover:bg-bg-hover hover:text-text"
        >
          <RefreshCw className={`lucide-inline ${isFetching ? 'animate-spin' : ''}`} />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-3">
        {triage.length > 0 && (
          <div className="bg-bg-accent border border-border-strong rounded-md p-[11px] mb-3">
            <div className="flex items-center gap-[7px] text-[13px] font-semibold text-text-strong mb-2">
              <ListChecks className="lucide-inline" />
              {i18nT('pages.chat.sessionSummary.open_items_heading')}
            </div>
            {triage.map((item, i) => {
              const key = triageKey(item)
              const open = triageOpen[key] === true
              return (
                <div key={key} className={i > 0 ? 'pt-[7px] mt-[7px] border-t border-border' : ''}>
                  <button
                    type="button"
                    onClick={() => toggleTriage(key)}
                    aria-expanded={open}
                    className="w-full text-left flex items-start gap-2 rounded hover:bg-bg-hover"
                  >
                    <span className="flex-1 min-w-0 text-[13px] text-text-strong">{item.what}</span>
                    <ChevronRight
                      className={`lucide-inline shrink-0 mt-[3px] text-muted-strong transition-transform ${open ? 'rotate-90' : ''}`}
                    />
                  </button>
                  {open && (
                    <div className="mt-[3px]">
                      {item.why && <div className="text-[12px] text-muted">{item.why}</div>}
                      {item.expect && (
                        <div className="text-[11px] text-muted-strong mt-[3px] italic">
                          {item.expect}
                        </div>
                      )}
                      <div className="text-[11px] text-muted-strong mt-1 flex items-center gap-1">
                        <ChevronRight className="lucide-inline" />
                        {i18nT('pages.chat.sessionSummary.from_intent', { intent: item.fromIntent })}
                      </div>
                    </div>
                  )}
                </div>
              )
            })}
            {hiddenOpen > 0 && (
              // Without this the block silently withholds items on exactly the
              // sessions that have the most of them.
              <div className="pt-[7px] mt-[7px] border-t border-border text-[12px] text-muted-strong">
                {i18nT('pages.chat.sessionSummary.open_items_more', { n: hiddenOpen })}
              </div>
            )}
          </div>
        )}

        <p className="text-[11px] text-muted-strong mb-2 flex items-center gap-[5px]">
          <Clock className="lucide-inline" />
          {i18nT('pages.chat.sessionSummary.most_recent_first')}
        </p>

        {intents.map((intent, i) => {
          const key = intentKey(intent)
          const open = isOpenFor(openMap, key, i === 0)
          return (
            <IntentCard
              key={key}
              intent={intent}
              open={open}
              onToggle={() => toggleIntent(key, open)}
            />
          )
        })}
      </div>

      {(data?.constraints?.length ?? 0) > 0 && (
        <div className="shrink-0 border-t border-border-strong bg-card">
          <button
            type="button"
            onClick={toggleNotes}
            aria-expanded={notesOpen}
            className="w-full text-left px-3 py-2 flex items-center gap-2 hover:bg-bg-hover"
          >
            <span className="text-[11px] font-semibold tracking-[0.02em] text-muted">
              {i18nT('pages.chat.sessionSummary.project_notes')}
            </span>
            <span className="text-[11px] font-mono text-muted-strong border border-border-strong rounded-full px-1.5">
              {data?.constraints.length}
            </span>
            <span className="flex-1" />
            <ChevronRight
              className={`lucide-inline text-muted-strong transition-transform ${notesOpen ? 'rotate-90' : ''}`}
            />
          </button>
          {notesOpen && (
            <ul className="pl-7 pr-3 pb-2.5 list-disc">
              {data?.constraints.map((note, i) => (
                <li key={i} className="text-[12px] text-muted my-[3px]">
                  {note}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <div className="shrink-0 border-t border-border-strong px-3 py-2 bg-bg-elevated flex items-center">
        <span className="text-[11px] text-muted-strong font-mono">
          {/* ONE freshness verdict, in one place. Two markers at opposite
              corners make the reader reconcile them, and a marker in the header
              wraps and displaces the count chip. Rendered as a single
              interpolated
              sentence so translators own the punctuation. */}
          {data?.generated_at
            ? data.stale
              ? i18nT('pages.chat.sessionSummary.updated_behind', {
                  when: fmtRelative(new Date(data.generated_at * 1000)),
                })
              : `${i18nT('pages.chat.sessionSummary.updated')} ${fmtRelative(new Date(data.generated_at * 1000))}`
            : i18nT('pages.chat.sessionSummary.not_generated')}
        </span>
      </div>
    </div>
  )
}

/** Identity for one intent's card: title AND the turn it starts at.
 *
 *  Titles are LLM-generated and NOT unique — two intents can carry the same
 *  one, which on a title-only key made both cards share a single disclosure
 *  entry (toggling either moved both) and collide as React keys. The first turn
 *  of the first range disambiguates them and, unlike the list index, does not
 *  change when the list re-sorts by recency, so a saved collapse survives an
 *  intent being worked on again. A stable server-side intent id would be better
 *  still, but the payload does not carry one. */
function intentKey(intent: SessionIntent): string {
  return [intent.title, String(intent.ranges[0]?.[0] ?? '')].join(KEY_SEP)
}

/** Disclosure default: the most recently worked intent is open, the rest are
 *  collapsed — but an explicit choice by the reader always wins. */
function isOpenFor(map: Record<string, boolean>, key: string, fallback: boolean): boolean {
  return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : fallback
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-muted text-[13px] p-6">
      {children}
    </div>
  )
}
