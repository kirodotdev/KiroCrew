import { compareText } from '../i18n/format'
import { Fragment, useCallback, useState, useMemo, useRef } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { AlertTriangle, Anchor, ChevronDown, Link2, Lock, MoreHorizontal, Pencil, Play } from 'lucide-react'
import { api } from '../api/client'
import { useProvider } from '../providers'
import SkillsMultiSelect from '../components/HookSkillsSelect'
import { Card, CardTitle, PageHeader, StatCard, Btn, SendBtn, Input, Badge, SearchInput, EmptyState } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '../components/ui/dropdown-menu'
import InfoTip from '../components/InfoTip'
import SimpleSelect from '../components/SimpleSelect'
import { esc } from '../api/helpers'
import { timeAgo as _timeAgo } from '../utils/timeAgo'
import { useSortableTable } from '../hooks/useSortableTable'
import { useScrollEdges } from '../hooks/useScrollEdges'
import { useArmedDelete } from '../hooks/useArmedDelete'
import SortableHeader from '../components/SortableHeader'
import { EVENTS, KAS_ONLY_EVENTS, AGENT_REQUESTED_EVENTS } from './hookEventWireValues'

import { i18nT } from '../i18n/t'
interface Hook {
  id: string; name: string; event: string; matcher: string
  matcher_mode: string; command: string; skills: string[]
  timeout: number; enabled: boolean
  last_run: number; last_status: string; last_error: string; run_count: number
}

/** Result payload from POST /api/hooks/:id/test. */
interface HookTestResult {
  exit_code?: number
  duration_ms?: number
  error?: string
  stdout?: string
  stderr?: string
}

const MATCHER_MODES = ['glob', 'regex', 'contains']

const EVENT_STYLE: Record<string, string> = {
  AgentSpawn: 'bg-accent/15 text-accent border-accent/30',
  UserPromptSubmit: 'bg-ok-subtle text-ok border-ok/30',
  PreToolUse: 'bg-aim-subtle text-aim border-aim/30',
  PostToolUse: 'bg-aim-subtle text-aim border-aim/30',
  Stop: 'bg-warn-subtle text-warn border-warn/30',
  PreTaskExecution: 'bg-aim-subtle text-aim border-aim/30',
  PostTaskExecution: 'bg-aim-subtle text-aim border-aim/30',
  FileCreated: 'bg-ok-subtle text-ok border-ok/30',
  FileEdited: 'bg-ok-subtle text-ok border-ok/30',
  FileDeleted: 'bg-warn-subtle text-warn border-warn/30',
  UserTriggered: 'bg-accent/15 text-accent border-accent/30',
}

const EVENT_BADGE: Record<string, 'ok' | 'err' | 'warn' | 'aim'> = {
  AgentSpawn: 'ok', UserPromptSubmit: 'ok',
  PreToolUse: 'aim', PostToolUse: 'aim', Stop: 'warn',
  PreTaskExecution: 'aim', PostTaskExecution: 'aim',
  FileCreated: 'ok', FileEdited: 'ok', FileDeleted: 'warn',
  UserTriggered: 'ok',
}

const EVENT_ORDER = Object.fromEntries(EVENTS.map((e, i) => [e, i]))

/** The padding and rule every hooks-table cell carries. */
const BASE_CELL = 'px-2.5 py-2 border-b border-border'

/** The mark an event carries when no event fires it, or undefined when one does.
 *
 *  Two marks, not one: the distance to running differs. A task trigger is asked
 *  for by an agent and waits only on this side answering; a file or manual
 *  trigger is not asked for at all. One mark for both would be a promise to
 *  four of them that nothing has made. */
const dormantMark = (event: string): string | undefined =>
  !KAS_ONLY_EVENTS.includes(event) ? undefined
    : AGENT_REQUESTED_EVENTS.includes(event)
      ? i18nT('pages.hooksPage.awaiting_agent')
      : i18nT('pages.hooksPage.stored_only')

/** What the mark means, in user terms, for the badge's own tooltip.
 *
 *  Two hints, because the two marks differ on the only thing a reader cares
 *  about: whether it will ever run by itself. One shared sentence made a reader
 *  read the help twice and still not know which of their hooks would run. */
const dormantHint = (event: string): string | undefined =>
  !KAS_ONLY_EVENTS.includes(event) ? undefined
    : AGENT_REQUESTED_EVENTS.includes(event)
      ? i18nT('pages.hooksPage.runs_on_no_event_yet')
      : i18nT('pages.hooksPage.runs_only_via_test')

/** Extra badge classes that tell the two marks apart at pill size.
 *
 *  The words reached their limit: `not fired yet` and `never fires` state their own
 *  facts, which fixed reading `stored` as "saved" -- and left a pair sharing one verb
 *  that a reader scanning STATUS said they would still mix up. Renaming again was the
 *  fourth round of churn on this element, so the difference moved to the pill itself.
 *
 *  SOLID for the two an agent asks for, HOLLOW and dashed for the four it does not:
 *  an outline reads as "less real than the filled one" without either pill becoming a
 *  fault. Both stay `muted` -- amber beside a green OK read as an error, which a
 *  designed dormant state is not. */
const dormantBadgeClass = (event: string): string =>
  AGENT_REQUESTED_EVENTS.includes(event)
    ? ''
    : 'bg-transparent border border-dashed border-border'

/** What an ON switch means on a row nothing fires, per mark.
 *
 *  Split for the same reason the save note is: one shared sentence ending in "yet"
 *  told the four triggers nothing asks for that something would fire them later,
 *  which is the single promise they must never make. The two an agent does ask for
 *  keep the "yet", because for them it is true. */
const dormantOnNote = (event: string): string | undefined =>
  !KAS_ONLY_EVENTS.includes(event) ? undefined
    : AGENT_REQUESTED_EVENTS.includes(event)
      ? i18nT('pages.hooksPage.on_but_nothing_fires')
      : i18nT('pages.hooksPage.on_but_never_fires')

/** One row cell's classes, faded when the hook is switched off.
 *
 *  The fade says "this hook is off", so it belongs on the cells that DESCRIBE the
 *  hook and not on what you can still do to it. On the `<tr>` it dimmed the
 *  ACTIONS cell along with everything else, and opacity is inherited -- a child
 *  cannot undo its parent's -- so Test and Delete rendered as disabled while
 *  staying clickable. The six triggers no event fires pay the most for that,
 *  because Test is their whole run path, and the visible way to restore the
 *  contrast is flipping the switch on: the pre-authorisation the off state exists
 *  to prevent. So the fade sits on the describing cells, in one place. */
const rowCell = (enabled: boolean, extra = ''): string =>
  `${BASE_CELL}${extra ? ' ' + extra : ''}${enabled ? '' : ' opacity-50'}`

const normalizeEvent = (e: string) => e.charAt(0).toUpperCase() + e.slice(1)

function timeAgo(ts: number): string {
  if (!ts) return 'never'
  return _timeAgo(ts)
}

function HookForm({ hook, onSave, onCancel }: {
  hook?: Hook; onSave: (data: Partial<Hook>) => void; onCancel: () => void
}) {
  const [name, setName] = useState(hook?.name || '')
  const [event, setEvent] = useState(hook?.event || 'UserPromptSubmit')
  const [matcher, setMatcher] = useState(hook?.matcher || '')
  const [matcherMode, setMatcherMode] = useState(hook?.matcher_mode || 'glob')
  const [command, setCommand] = useState(hook?.command || '')
  const [skills, setSkills] = useState<string[]>(hook?.skills || [])
  const [timeout, setTimeout_] = useState(hook?.timeout || 30)
  const isToolHook = event === 'PreToolUse' || event === 'PostToolUse'
  // Skills fire only for a standalone skills hook (no command) on
  // UserPromptSubmit/AgentSpawn — a command makes them inert, and other events
  // have no consumer for the injected directive.
  const isSkillsCapable =
    (event === 'UserPromptSubmit' || event === 'AgentSpawn') && !command.trim()
  // A legacy/edited hook can arrive with skills that can no longer fire. Show
  // them read-only with a warning (never silently delete on mount — that would
  // be data loss the user never asked for). The save sends skills unchanged;
  // the backend rejects the invalid pairing with an actionable field-level
  // error, and the warning banner tells the user what to change.
  const inertSkills = !isSkillsCapable && skills.length > 0

  // Dynamic placeholder text per matcher mode
  const matcherPlaceholder = isToolHook
    ? i18nT('pages.hooksPage.matcher_tool_filter_e_g_fs_write_git')
    : matcherMode === 'regex'
      ? i18nT('pages.hooksPage.matcher_placeholder_regex')
      : matcherMode === 'contains'
        ? i18nT('pages.hooksPage.matcher_placeholder_contains')
        : i18nT('pages.hooksPage.matcher_optional_e_g_deploy')

  return (
    <Card>
      <CardTitle>{hook ? i18nT('pages.hooksPage.edit_hook') : i18nT('pages.hooksPage.new_hook_2')} <InfoTip text={i18nT('pages.hooksPage.script_hooks_fire_shell_commands_on_chat_lifecyc')} /></CardTitle>
      <div className="flex flex-col gap-3">
        <div className="flex gap-2 items-center flex-wrap">
          <Input placeholder={i18nT('pages.hooksPage.hook_name')} value={name} onChange={e => setName(e.target.value)} />
          <SimpleSelect
            options={EVENTS}
            // The panel is the trigger's width by default, and the trigger hugs a
            // short value like `Stop` — so `PostTaskExecution` plus its mark clipped
            // to "waiti". Wide enough for the longest option and badge together.
            contentClassName="min-w-[19rem]"
            // The mark rides the OPTION, which is where the choice is made; the
            // option's value and accessible name stay the bare wire value, and the
            // touch path spells the same fact as `name -- mark`.
            optionBadges={EVENTS.map(e => {
              const mark = dormantMark(e)
              // The hint rides the badge, not just the table's copy of it:
              // the moment of choice is here, and a reader who has never
              // seen the table cannot tell `waiting` from `stored`.
              return mark
                ? { label: mark, source: 'kirocrew', hint: dormantHint(e) }
                : undefined
            })}
            // Choosing a dormant trigger does NOT clear `matcher`, it only hides
            // the field and drops the value AT SAVE. Clearing it on selection let
            // an ordinary two-click exploration — PreToolUse to FileEdited and back —
            // silently widen a stored `fs_write` hook to every tool call, with no
            // restore path and nothing on screen saying so. Nothing is discarded
            // invisibly either, which was the original objection: while the field is
            // gone the line in its place says the trigger takes no matcher.
            onChange={setEvent}
            value={event}
            // A hook stored with an event this picker no longer offers (legacy
            // or hand-edited config) matches no row. A native <select> silently
            // displayed the FIRST option while state held the stale value; show
            // the stored value instead.
            triggerFallback={event}
            aria-label={i18nT('pages.hooksPage.event')}
          />
        </div>
        <div>
          <Input className="w-full font-mono" placeholder={i18nT('pages.hooksPage.echo_hook_fired')} value={command} onChange={e => setCommand(e.target.value)} />
        </div>
        <div className="flex gap-2 items-center flex-wrap">
          {/* The shared Input is `flex-1 min-w-0`, i.e. `flex-basis: 0%`, so its
              hypothetical main size is ZERO. Flex line-breaking uses that size,
              so the three non-shrinking siblings below never wrap to their own
              line — they all stay on line 1 and this field absorbs the entire
              shortfall. Measured across pane widths 320-760px: 45px at a 360px
              pane, 105px at 420px, under 120px at six widths. `basis-full` while
              narrow and `basis-auto` above restores intrinsic-size-aware
              breaking, so a sibling that does not fit wraps instead: 231px worst
              case, never below 120px. Same idiom as the tokens row in
              WebhooksPage, which had the identical defect. */}
          {/* No matcher for a trigger no event fires: the store refuses one there,
              because a matcher filters something in the event's payload and these
              events have no payload yet. Offering the field would be a form that
              cannot save. Timeout stays — Test honours it. */}
          {!dormantMark(event) && (
            <Input className="basis-full sm:basis-auto" placeholder={matcherPlaceholder} value={matcher} onChange={e => setMatcher(e.target.value)} />
          )}
          {!isToolHook && !dormantMark(event) && (
            <SimpleSelect
              options={MATCHER_MODES}
              value={matcherMode}
              onChange={setMatcherMode}
              aria-label={i18nT('pages.hooksPage.matcher_mode')}
            />
          )}
          {/* Two fields disappearing with no word for it left the reader
              guessing at a bug. One line where they were says which fact
              removed them. */}
          {dormantMark(event) && (
            <span className="basis-full sm:basis-auto text-[13px] text-muted">
              {i18nT('pages.hooksPage.no_matcher_for_trigger')}
            </span>
          )}
          <div className="flex items-center gap-1.5 text-[13px] text-muted shrink-0">
            <span>{i18nT('pages.hooksPage.timeout')}</span>
            <Input type="number" min={1} max={300} className="w-16" value={timeout} onChange={e => setTimeout_(parseInt(e.target.value, 10) || 30)} />
            <span>{i18nT('pages.hooksPage.s')}</span>
          </div>
        </div>
        {isSkillsCapable && (
          <div>
            <SkillsMultiSelect selected={skills} onChange={setSkills} />
          </div>
        )}
        {/* The skills row disappearing with no word for it read as a bug, the same
            way the matcher's did. The sentence states the REAL rule and never blames
            the trigger, because dormancy is not the cause -- skills also vanish on
            `Stop` and on a prompt hook that has a command.

            Shown on the dormant path only, even so. Rendering it for the five as well
            changed what an untouched form says on events this PR is not about, which
            is a ride-along however small. The `Stop` case keeps its existing silence
            and belongs to whoever fixes it deliberately. Suppressed when `inertSkills`
            already has its own warning, which says more. */}
        {dormantMark(event) && !isSkillsCapable && !inertSkills && (
          <div className="text-[13px] text-muted">
            {i18nT('pages.hooksPage.skills_only_for_prompt_hooks')}
          </div>
        )}
        {inertSkills && (
          <div className="flex items-start gap-2 text-[13px] text-warn bg-warn-subtle border border-warn/30 rounded-lg px-3 py-2">
            <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
            <span>
              {i18nT('pages.hooksPage.skills_inert_warning', { skills: skills.join(', ') })}
            </span>
          </div>
        )}
        {/* A `title` tooltip is the wrong and only home for the decode: no touch
            device and no keyboard reaches it, and a reader who does not hover never
            learns what `waiting` and `stored` mean. Here it is ordinary text, beside
            the very mark it explains, at the moment the trigger is chosen. */}
        {dormantMark(event) && (
          <div className="flex items-center gap-2 text-[13px] text-muted">
            <Badge variant="muted" className={dormantBadgeClass(event)}>{dormantMark(event)}</Badge>
            <span>{dormantHint(event)}</span>
          </div>
        )}
        {/* A NEW hook on a dormant trigger is stored switched off, and a row that
            came back dimmed with its switch off read as a failed save — whose
            natural repair, flipping it on, is exactly the pre-authorisation the
            off state exists to prevent. Say it before Save, not after.

            Shown for a create, and for an edit that MOVES a hook off a live
            event onto one of the six -- the store switches it off on that
            transition too. Not for a hook already on one of the six: that one
            keeps the state it has, so the line would be false there. */}
        {dormantMark(event) && (!hook || !dormantMark(hook.event)) && (
          <div className="text-[13px] text-muted">
            {/* Per mark, not one shared sentence: beside "Never runs on its own",
                a trailing "nothing fires this trigger YET" read as "it might
                later", which is the promise these four must never make. */}
            {AGENT_REQUESTED_EVENTS.includes(event)
              ? i18nT('pages.hooksPage.saved_switched_off')
              : i18nT('pages.hooksPage.saved_switched_off_never')}
          </div>
        )}
        <div className="flex gap-2 items-center">
          {/* The matcher is dropped HERE, for a trigger whose payload cannot be
              filtered, so the value survives a round trip through the picker. */}
          <SendBtn onClick={() => onSave({ name, event, matcher: dormantMark(event) ? '' : matcher, matcher_mode: matcherMode, command, skills, timeout })}>{i18nT('pages.hooksPage.save')}</SendBtn>
          <Btn onClick={onCancel} className="h-9 px-4 text-sm font-semibold rounded-lg">{i18nT('pages.hooksPage.cancel')}</Btn>
        </div>
      </div>
    </Card>
  )
}

export default function HooksPage({ embedded }: { embedded?: boolean } = {}) {
  const provider = useProvider()
  const { data: hooks = [], isLoading: loading, error: hooksErr, refetch: refresh } = useQuery<Hook[]>({
    queryKey: ['hooks'],
    queryFn: () => api.hooks().then((r: { hooks?: Hook[] }) => r.hooks || []),
  })
  const error = hooksErr ? i18nT('pages.hooksPage.failed_to_load_hooks', { error: hooksErr instanceof Error ? hooksErr.message : String(hooksErr) }) : null
  const { data: providerHooks = {}, error: providerHookErr, refetch: refetchProviderHooks } = useQuery({
    queryKey: ['provider-hooks', provider.id],
    queryFn: () => provider.fetchProviderHooks(),
    enabled: provider.capabilities.hooks,
  })
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  // A HookForm holds unsaved name/command/matcher text while open, and the
  // agent hand-off unmounts this page — so every ErrorNotice below hands off
  // only when no form is open.
  const handoffSafe = !creating && !editing
  // Which row has its persisted last_error expanded beneath it (one at a time).
  const [openErrorId, setOpenErrorId] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<{ id: string; data: HookTestResult } | null>(null)
  // Inline failure state for a rejected hook-test request (network error, 5xx,
  // etc.). This is distinct from testResult (a completed run, which can itself
  // report a non-zero exit) and from the global mutError banner: the request
  // never produced a HookTestResult, so the panel would otherwise render empty.
  const [testError, setTestError] = useState<{ id: string; message: string } | null>(null)
  const [filter, setFilter] = useState('')
  // React Query's pending render is asynchronous. Claim request ownership in a
  // ref before mutate() so two same-tick clicks cannot launch overlapping tests
  // whose callbacks would race to populate one shared result panel.
  const testInFlight = useRef(false)

  const mutOpts = { onSuccess: () => refresh(), onError: (e: Error) => e }
  const createMut = useMutation({ mutationFn: (data: Partial<Hook>) => api.createHook(data), ...mutOpts, onSuccess: () => { setCreating(false); refresh() } })
  const updateMut = useMutation({ mutationFn: ({ id, data }: { id: string; data: Partial<Hook> }) => api.updateHook(id, data), ...mutOpts, onSuccess: () => { setEditing(null); refresh() } })
  const deleteMut = useMutation({
    mutationFn: (id: string) => api.deleteHook(id),
    // Refetch BEFORE the armed-delete hook re-enables the row (mutateAsync
    // resolves only after this settles, and useArmedDelete removes the
    // pending id after deleteFn resolves): a deleted row must disappear
    // rather than flash a re-enabled Delete. Deliberately NOT ...mutOpts —
    // its `onSuccess: () => refresh()` does not await, so it cannot carry
    // this ordering.
    onSettled: async () => { await refresh() },
  })
  const toggleMut = useMutation({ mutationFn: (id: string) => api.toggleHook(id), ...mutOpts })
  const testMut = useMutation({
    mutationFn: (id: string) => api.testHook(id),
    onSuccess: (r: { result: HookTestResult }, id: string) => { setTestError(null); setTestResult({ id, data: r.result }); refresh() },
    // Populate the inline panel on rejection so a failed request is not shown as
    // an empty result. The global mutError banner still fires (testMut.error is
    // read into it below), so the global error path is preserved, not replaced.
    onError: (e: Error, id: string) => { setTestError({ id, message: e instanceof Error ? e.message : String(e) }) },
    onSettled: () => { testInFlight.current = false },
  })

  // Delete is the shared arm→Confirm→decay machine (useArmedDelete, the
  // CronRowActions convention — SchedulePage consumes it the same way). A
  // menu that closes on select cannot host the armed state, which is why
  // Delete stays out of the ⋯ overflow below. Failures surface through
  // deleteMut.error via the mutError banner, so the rejection confirm
  // swallows is already reported. mutateAsync is referentially stable, so it
  // is handed over directly.
  const { armedId: confirmDeleteId, arm: armDelete, confirm: confirmDelete, isDeleting } = useArmedDelete(deleteMut.mutateAsync)

  // `testMut` is deliberately absent: a failed hook test is owned by the
  // titled `hook-test-error` notice beside the row, and a second, bare copy
  // at the top of the page reads as a page-wide outage.
  const mutError = createMut.error?.message || updateMut.error?.message || deleteMut.error?.message || toggleMut.error?.message || null
  const handleCreate = (data: Partial<Hook>) => createMut.mutate(data)
  const handleUpdate = (id: string, data: Partial<Hook>) => updateMut.mutate({ id, data })
  const handleToggle = (id: string) => toggleMut.mutate(id)
  const handleTest = (id: string) => {
    if (testInFlight.current) return
    testInFlight.current = true
    setTestResult(null)
    setTestError(null)
    testMut.mutate(id)
  }

  const enabled = hooks.filter(h => h.enabled).length
  const totalRuns = hooks.reduce((s, h) => s + h.run_count, 0)
  const lastErr = hooks.filter(h => h.last_status === 'error').length
  const filtered = useMemo(
    () => hooks.filter(h => !filter || (h.name + ' ' + h.event + ' ' + h.command + ' ' + h.matcher)
      .toLowerCase().includes(filter.toLowerCase())),
    [hooks, filter],
  )
  const hookComparators = useMemo(() => ({
    name: (a: Hook, b: Hook) => compareText(a.name, b.name),
    event: (a: Hook, b: Hook) => compareText(a.event, b.event),
    runs: (a: Hook, b: Hook) => a.run_count - b.run_count,
    status: (a: Hook, b: Hook) => compareText(a.last_status || '', b.last_status || ''),
    lastRun: (a: Hook, b: Hook) => (a.last_run || 0) - (b.last_run || 0),
  }), [])
  const { sorted: sortedHooks, sort: hookSort, toggle: toggleHookSort } = useSortableTable(filtered, 'hooks', hookComparators, { key: 'name', dir: 'asc' })
  // Measured overflow state for the hooks table's scroller — gates the pinned
  // Actions column's seam (border + fade). Measured, not breakpoint-inferred:
  // the table overflows whenever its container is narrower than the declared
  // column widths, which a resizable nav rail can cause at any viewport size.
  const [attachHooksScroller, hooksTableEdges, , attachHooksTable] = useScrollEdges<HTMLDivElement>()
  // The scroller's visible width, for the expanded last_error row: the table
  // can be wider than its scroller (sticky Actions column, #4296), and a row
  // that simply spanned the columns would paint its far end under that column
  // and scroll out of view. The row's notice is instead a sticky-left block
  // exactly as wide as the viewport onto the table, so it reads in full at
  // any scroll position.
  const [hooksScrollerWidth, setHooksScrollerWidth] = useState(0)
  const hooksScrollerRo = useRef<ResizeObserver | null>(null)
  const attachHooksScrollerMeasured = useCallback((node: HTMLDivElement | null) => {
    attachHooksScroller(node)
    hooksScrollerRo.current?.disconnect()
    hooksScrollerRo.current = null
    if (!node) return
    setHooksScrollerWidth(node.clientWidth)
    if (typeof ResizeObserver !== 'undefined') {
      const ro = new ResizeObserver(() => setHooksScrollerWidth(node.clientWidth))
      ro.observe(node)
      hooksScrollerRo.current = ro
    }
  }, [attachHooksScroller])

  if (loading) return <div className="p-6 text-muted">{i18nT('pages.hooksPage.loading')}</div>

  const content = (
    <>
      <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
        {[
          { label: i18nT('pages.hooksPage.total'), value: hooks.length, accent: true },
          { label: i18nT('pages.hooksPage.enabled'), value: enabled },
          { label: i18nT('pages.hooksPage.total_runs'), value: totalRuns },
          { label: i18nT('pages.hooksPage.errors'), value: lastErr },
        ].map((s, i) => (
          <StatCard key={s.label} label={s.label} value={s.value} delay={i * 60} accent={s.accent} />
        ))}
        </div>

        {(error || mutError) && (
          <div className="mb-4 flex items-start gap-2 animate-rise">
            {/* No hand-off while a HookForm is open (`creating` / `editing`) —
                its fields are unsaved. With no form open the failures here are
                the hooks read or an action on an already-saved hook. A failed
                READ offers Retry and no dismiss (dismissing a query error would
                only hide a list that is still missing); a failed action offers
                dismiss and no Retry — so the row never holds more than two
                controls (max-two-buttons-per-row). */}
            <ErrorNotice
              message={error || mutError}
              onDismiss={hooksErr ? undefined : () => { createMut.reset(); updateMut.reset(); deleteMut.reset(); toggleMut.reset(); testMut.reset() }}
              askAgent={handoffSafe}
              className="flex-1"
              testId="hooks-error"
            />
            {hooksErr && <Btn onClick={() => refresh()} className="shrink-0">{i18nT('pages.hooksPage.retry')}</Btn>}
          </div>
        )}

        {creating ? (
          <HookForm onSave={handleCreate} onCancel={() => setCreating(false)} />
        ) : (
          <div className="flex items-center gap-2 mb-4">
            <SendBtn onClick={() => { setCreating(true); setEditing(null) }}>{i18nT('pages.hooksPage.new_hook')}</SendBtn>
          </div>
        )}

        {editing && (() => {
          const h = hooks.find(x => x.id === editing)
          return h ? <HookForm hook={h} onSave={data => handleUpdate(h.id, data)} onCancel={() => setEditing(null)} /> : null
        })()}

        <Card>
          <CardTitle>{i18nT('pages.hooksPage.hooks')} <InfoTip text={i18nT('pages.hooksPage.hooks_run_shell_commands_on_chat_events_agentspa')} /></CardTitle>
          <div className="mb-3"><SearchInput placeholder={i18nT('pages.hooksPage.filter_hooks')} value={filter} onChange={e => setFilter(e.target.value)} /></div>
          {hooks.length === 0 ? (
            <EmptyState icon={<Anchor className="lucide-inline" />} title={i18nT('pages.hooksPage.no_hooks_yet')} subtitle={i18nT('pages.hooksPage.create_a_hook_to_run_scripts_on_chat_events')} />
          ) : (
            <div ref={attachHooksScrollerMeasured} className="overflow-x-auto">
              {/* This table is AUTO layout (`w-full border-collapse`, no
                  table-fixed), so column edges depend on content and a
                  wrapper-anchored cue cannot know where the pinned column
                  starts. The seam therefore lives INSIDE the pinned cells —
                  but NOT as a cell border: under Preflight's
                  `border-collapse: collapse` a cell border belongs to the
                  collapsed table grid and paints at the cell's LAYOUT slot,
                  so it stays behind while the sticky cell travels. It is a
                  1px child div instead (`left-0 w-px bg-border`), which the
                  sticky cell carries, painted alongside a `right-full`
                  gradient child hanging just left of it — both gated on the
                  measured overflow flag, so a table that fits renders
                  neither. Same treatment as the Schedule jobs table. The
                  table itself is the observed content node: auto layout means
                  the ROWS set scrollWidth, which the scroller's own box never
                  reports. */}
              <table ref={attachHooksTable} className="w-full border-collapse table-striped">
                <thead>
                  <tr>
                    <th aria-label={i18nT('pages.hooksPage.enabled')} className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[52px]"></th>
                    <SortableHeader label={i18nT('pages.hooksPage.name')} sortKey="name" sort={hookSort} onToggle={toggleHookSort} className="w-[120px]" />
                    <SortableHeader label={i18nT('pages.hooksPage.event')} sortKey="event" sort={hookSort} onToggle={toggleHookSort} className="w-[130px]" />
                    {/* 160px, not 200: the six long event names widen EVENT and STATUS
                        by 32px between them, which pushed this AUTO-layout table 18px
                        past its scroller and slid LAST RUN under the `sticky right-0`
                        ACTIONS column, unreadable as "1m ag…". COMMAND gives the room
                        back because it is the only column that already truncates behind
                        a `title`, so nothing here becomes unrecoverable. The capture asserts the
                        overlap is zero, which is what caught 176px being 1px short on a
                        row whose Status holds both a mark and a result. */}
                    <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium min-w-[160px]">{i18nT('pages.hooksPage.command')}</th>
                    <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[120px]">{i18nT('pages.hooksPage.matcher')}</th>
                    <SortableHeader label={i18nT('pages.hooksPage.runs')} sortKey="runs" sort={hookSort} onToggle={toggleHookSort} className="w-[60px]" />
                    <SortableHeader label={i18nT('pages.hooksPage.status')} sortKey="status" sort={hookSort} onToggle={toggleHookSort} className="w-[80px]" />
                    <SortableHeader label={i18nT('pages.hooksPage.last_run')} sortKey="lastRun" sort={hookSort} onToggle={toggleHookSort} className="w-[90px]" />
                    {/* Pinned sticky-right per #4296. No width hint: under auto
                        table layout a specified width is only a preferred width
                        — the nowrap content's minimum still wins when larger —
                        and this cell's widest state (the armed "Delete?" label)
                        varies by locale, so a hint is either redundant or
                        overridden. */}
                    <th className="sticky right-0 bg-card text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">
                      {hooksTableEdges.right && <div aria-hidden="true" className="pointer-events-none absolute left-0 top-0 bottom-0 w-px bg-border" />}
                      {hooksTableEdges.right && <div aria-hidden="true" className="pointer-events-none absolute right-full top-0 bottom-0 w-6 bg-gradient-to-l from-card to-transparent" />}
                      {i18nT('pages.hooksPage.actions')}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.length === 0 ? (
                    <tr><td colSpan={9} className="text-muted italic px-2.5 py-3.5 text-sm">{i18nT('pages.hooksPage.no_matching_hooks')}</td></tr>
                  ) : sortedHooks.map((h, i) => (
                    <Fragment key={h.id}>
                    <tr className="group/hookrow hover:bg-bg-hover transition-colors">
                      <td className={rowCell(h.enabled)}>
                        <button
                          className={`w-9 h-5 rounded-full relative transition-colors cursor-pointer ${h.enabled ? 'bg-accent' : 'bg-border'}`}
                          onClick={() => handleToggle(h.id)}
                          // "Everything is enabled" and "this never runs on its own" read
                          // as a contradiction on the same row. They are both true: the
                          // switch is the author's intent, the mark is what the gateway
                          // does, and for these triggers the two do not meet yet.
                          //
                          // In the NAME, not only the `title`: a tooltip reaches a pointer
                          // and nothing else, so a keyboard or touch user met an ON switch
                          // beside `never fires` with nothing reconciling them. The action
                          // stays first, so the control still announces what it does.
                          aria-label={[
                            h.enabled ? i18nT('pages.hooksPage.disable_hook') : i18nT('pages.hooksPage.enable_hook'),
                            h.enabled ? dormantOnNote(h.event) : '',
                          ].filter(Boolean).join(' — ')}
                          title={h.enabled ? dormantOnNote(h.event) : undefined}
                        >
                          <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform ${h.enabled ? 'left-[18px]' : 'left-0.5'}`} />
                        </button>
                      </td>
                      <td className={rowCell(h.enabled, 'text-sm font-medium text-text')}>{esc(h.name)}</td>
                      <td className={rowCell(h.enabled, 'text-sm')}><span className={`px-1.5 py-[2px] rounded-full text-[11px] font-bold border font-mono ${EVENT_STYLE[h.event] || 'bg-bg-elevated text-muted border-border'}`}>{h.event}</span></td>
                      <td className={rowCell(h.enabled, 'text-sm font-mono text-text/80 truncate max-w-[300px]')} title={h.command}>{esc(h.command)}</td>
                      <td className={rowCell(h.enabled, 'text-sm text-muted')}>{h.matcher ? esc(h.matcher) : <span className="italic">—</span>}</td>
                      <td className={rowCell(h.enabled, 'text-sm font-mono')}>{h.run_count}</td>
                      <td className={rowCell(h.enabled, 'text-sm')}>
                        {/* The persisted last_error is not tooltip-only: the
                            chevron expands it beneath the row as an ErrorNotice
                            (the status column is too narrow to hold it inline). */}
                        {/* The mark and the result are different kinds of fact —
                            whether anything fires this trigger, and how the last run
                            went — so they SHARE the cell rather than replace each
                            other. A Tested dormant hook otherwise lost the row's only
                            "this never fires on its own" statement for good, and the
                            reader was left with a bare OK on a hook nothing runs.

                            `muted`, not `warn`: amber beside a green OK read as a
                            fault, and a designed dormant state is not one. The mark
                            is short enough to fit the column, so it carries its own
                            decode in `title` rather than sending the reader to the
                            panel's "?".

                            STACKED, not side by side: this table is AUTO layout inside
                            an `overflow-x-auto` scroller whose ACTIONS column is `sticky
                            right-0`, so a pixel the cell adds is a pixel the sticky column
                            takes from LAST RUN rather than one the reader can scroll to.
                            Measured at 1440px, the MARK alone takes Status from 67px to
                            92px; a Tested dormant row adding its result beside it would
                            take more again, and stacking caps the column at the wider of
                            the two. The 18px the marks cost overall is given back by
                            COMMAND, which truncates with a tooltip. */}
                        <span className="inline-flex flex-col items-start gap-0.5">
                          {dormantMark(h.event) && (
                            // The mark WRAPS here rather than widening the column. Marks
                            // that state the fact ("not fired yet") are longer than the
                            // words they replaced, and on one line they cost 45px -- enough
                            // to slide LAST RUN back under the sticky ACTIONS column. Two
                            // short lines in a capped badge cost less than the single line
                            // did before.
                            <Badge
                              variant="muted"
                              title={dormantHint(h.event)}
                              // A `title` reaches a pointer and nothing else. The
                              // accessible name carries the whole sentence so a
                              // screen reader hears which of the two marks this is
                              // and why, while the visible text stays the short
                              // mark the column can hold.
                              aria-label={`${dormantMark(h.event)} — ${dormantHint(h.event)}`}
                              className={`whitespace-normal text-[11px] leading-[1.15] max-w-[4.75rem] ${dormantBadgeClass(h.event)}`}
                            >{dormantMark(h.event)}</Badge>
                          )}
                          {/* An em dash reads as "has not run yet", which is the
                              wrong story for a row nothing will ever run — so the
                              mark stands alone there instead. */}
                          {/* A result under `never fires` reads as a contradiction
                              until the row says where the run came from: Test is the
                              only thing that can have run it. */}
                          {h.last_status && dormantMark(h.event) && (
                            <span className="text-muted text-[11px]">{i18nT('pages.hooksPage.via_test')}</span>
                          )}
                          {!h.last_status
                          ? (dormantMark(h.event) ? null : <span className="text-muted italic">—</span>)
                          : h.last_status === 'ok' ? <Badge variant="ok">{i18nT('pages.hooksPage.ok')}</Badge>
                          : (
                            <span className="inline-flex items-center gap-1">
                              <Badge variant={h.last_status === 'error' ? 'err' : 'warn'}>{h.last_status === 'error' ? i18nT('pages.hooksPage.error') : h.last_status}</Badge>
                              {h.last_error && (
                                <Btn
                                  type="button"
                                  className="px-1 py-0 border-transparent text-muted hover:text-text"
                                  aria-expanded={openErrorId === h.id}
                                  aria-label={i18nT('pages.hooksPage.show_last_error', { name: h.name })}
                                  onClick={() => setOpenErrorId(openErrorId === h.id ? null : h.id)}
                                >
                                  <ChevronDown size={13} className={`transition-transform ${openErrorId === h.id ? 'rotate-180' : ''}`} aria-hidden="true" />
                                </Btn>
                              )}
                            </span>
                          )}
                        </span>
                      </td>
                      {/* `timeAgo(0)` is the word "never", which one column from a
                          `never fires` badge reads as the same fact twice — and it is
                          not: one is "no run recorded", the other "no event fires
                          this". An em dash for a dormant row that has not run says
                          the first without borrowing the second's word. */}
                      <td className={rowCell(h.enabled, 'text-sm text-muted')}>
                        {!h.last_run && dormantMark(h.event)
                          ? <span className="italic">—</span>
                          : timeAgo(h.last_run)}
                      </td>
                      {/* Pinned like the header cell, on an OPAQUE `bg-card`.
                          The row states live on the <tr>, which the opaque base
                          would hide, so the overlay re-applies them: even rows
                          mirror `.table-striped`'s translucent `--card-hl` zebra
                          (which outranks the row's hover utility by specificity,
                          so hover is deliberately NOT mirrored there), odd rows
                          mirror the hover tint via the named row group. No
                          aria-label: the header already names the column, and a
                          cell label would triple-name the ⋯ trigger for screen
                          readers. */}
                      <td className="sticky right-0 bg-card px-2.5 py-2 border-b border-border text-sm whitespace-nowrap">
                        <div aria-hidden className={`absolute inset-0 -z-10 transition-colors ${i % 2 === 1 ? 'bg-[var(--card-hl)]' : 'group-hover/hookrow:bg-bg-hover'}`} />
                        {hooksTableEdges.right && <div aria-hidden="true" className="pointer-events-none absolute left-0 top-0 bottom-0 w-px bg-border" />}
                        {/* The fade must ramp toward the surface it abuts: on a
                            hovered odd row that is the hover tint, not the card
                            (even rows keep from-card — zebra outranks the row's
                            hover utility, so their surface never changes). */}
                        {hooksTableEdges.right && <div aria-hidden="true" className={`pointer-events-none absolute right-full top-0 bottom-0 w-6 bg-gradient-to-l from-card to-transparent ${i % 2 === 1 ? '' : 'group-hover/hookrow:from-bg-hover'}`} />}
                        {/* Two controls plus the overflow menu (max-two-buttons-per-row,
                            following CronRowActions). Test stays in the row as the
                            per-glance action; Edit lives in the ⋯ menu. Delete stays a
                            row-level button — its arm→Confirm state needs the button
                            visible, and a menu that closes on select cannot host the
                            armed state. The armed label explains itself IN THE LABEL:
                            the `title` tooltip is hover-only, so on touch it does not
                            exist. The visible text is also the accessible name — no
                            aria-label, which would override the label a sighted user
                            reads (WCAG 2.5.3, Label in Name); the row names the hook. */}
                        <div className="flex items-center gap-1.5">
                          {/* For a row nothing fires, Test is not a rehearsal -- it is
                              the only way the hook runs at all, which a reader who
                              expects a dry run should know BEFORE pressing it. The
                              mark's own hint is already that sentence, so it is
                              reused rather than translated again. */}
                          <Btn disabled={testMut.isPending} title={dormantHint(h.event)} onClick={() => handleTest(h.id)} className="bg-accent/10 text-accent border-accent/30 hover:bg-accent/20">{i18nT('pages.hooksPage.test')}</Btn>
                          <Btn
                            danger
                            disabled={isDeleting(h.id)}
                            title={confirmDeleteId === h.id ? i18nT('pages.hooksPage.click_again_to_confirm') : i18nT('pages.hooksPage.delete_hook', { name: h.name })}
                            onClick={() => { if (confirmDeleteId === h.id) void confirmDelete(h.id); else armDelete(h.id) }}
                          >{isDeleting(h.id) ? '...' : confirmDeleteId === h.id ? i18nT('pages.hooksPage.confirm_delete_hook') : i18nT('pages.hooksPage.delete')}</Btn>
                          <DropdownMenu>
                            <DropdownMenuTrigger asChild>
                              <Btn
                                className="!px-1.5"
                                aria-label={i18nT('pages.hooksPage.more_actions')}
                                title={i18nT('pages.hooksPage.more_actions')}
                              >
                                <MoreHorizontal size={14} />
                              </Btn>
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align="end" className="min-w-[160px]">
                              {/* Test is also in the row; repeated here so the menu is a
                                  complete account of what can be done to the hook. */}
                              <DropdownMenuItem disabled={testMut.isPending} onSelect={() => handleTest(h.id)}>
                                <Play size={13} className="shrink-0 text-accent" />
                                <span>{i18nT('pages.hooksPage.test')}</span>
                              </DropdownMenuItem>
                              <DropdownMenuItem onSelect={() => { setEditing(h.id); setCreating(false) }}>
                                <Pencil size={13} className="shrink-0 text-muted" />
                                <span>{i18nT('pages.hooksPage.edit')}</span>
                              </DropdownMenuItem>
                            </DropdownMenuContent>
                          </DropdownMenu>
                        </div>
                      </td>
                    </tr>
                    {openErrorId === h.id && h.last_error && (
                      <tr>
                        <td colSpan={9} className="p-0 border-b border-border">
                          {/* Sticky-left, scroller-wide: see hooksScrollerWidth. */}
                          <div className="sticky left-0 px-2.5 py-2" style={hooksScrollerWidth ? { width: hooksScrollerWidth } : undefined}>
                            {/* No hand-off while a HookForm is open (`creating` /
                                `editing`) — its fields are unsaved. Otherwise the
                                last_error is persisted server-side; nothing to lose. */}
                            <ErrorNotice message={h.last_error} askAgent={handoffSafe} />
                          </div>
                        </td>
                      </tr>
                    )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {testResult && (() => {
            const h = hooks.find(x => x.id === testResult.id)
            return (
              <div className="mt-3 bg-bg-elevated border border-border rounded-lg p-4 animate-scale-in">
                <div className="flex items-center gap-3 mb-2">
                  <span className="min-w-0 break-words text-sm font-medium text-text">{i18nT('pages.hooksPage.test_result')}{h ? `: ${h.name}` : ''}</span>
                  <Badge variant={testResult.data.exit_code === 0 ? 'ok' : 'err'}>{testResult.data.exit_code === 0 ? 'OK' : `exit ${testResult.data.exit_code}`}</Badge>
                  <span className="text-[12px] text-muted font-mono">{testResult.data.duration_ms}{i18nT('pages.hooksPage.ms')}</span>
                  <Btn aria-label={i18nT('app.dismiss')} onClick={() => setTestResult(null)} className="ml-auto shrink-0">×</Btn>
                </div>
                {/* No hand-off while a HookForm is open (`creating` / `editing`) —
                    its fields are unsaved. Otherwise the test ran against a saved
                    hook and nothing is lost. */}
                <ErrorNotice variant="inline" message={testResult.data.error} askAgent={handoffSafe} className="mb-1" />
                {testResult.data.stdout && <pre className="whitespace-pre-wrap text-[12px] font-mono text-text/80 bg-bg border border-border rounded-md p-3 max-h-[200px] overflow-auto">{testResult.data.stdout}</pre>}
                {testResult.data.stderr && <pre className="whitespace-pre-wrap text-[12px] font-mono text-warn bg-bg border border-border rounded-md p-3 max-h-[100px] overflow-auto mt-2">{testResult.data.stderr}</pre>}
              </div>
            )
          })()}
          {testError && (() => {
            const h = hooks.find(x => x.id === testError.id)
            return (
              <>
                {/* Same draft decision as the banner: no hand-off while a HookForm
                    (`creating` / `editing`) is open, otherwise safe. */}
                <ErrorNotice
                  title={h ? i18nT('pages.hooksPage.test_failed_for', { name: h.name }) : i18nT('pages.hooksPage.test_failed')}
                  message={testError.message}
                  onDismiss={() => setTestError(null)}
                  askAgent={handoffSafe}
                  className="mt-3 animate-scale-in"
                  testId="hook-test-error"
                />
              </>
            )
          })()}
        </Card>
        {provider.capabilities.hooks && (
        <Card>
          <CardTitle>{provider.labels.hooksSection} <InfoTip text={i18nT('pages.hooksPage.read_only_view_of_provider_hooks', { path: provider.labels.configFile || i18nT('pages.hooksPage.config') })} /></CardTitle>
          {providerHookErr ? (
            <div className="flex items-start gap-2">
              {/* A read failure dressed as an empty state hid the cause. No
                  hand-off while a HookForm (`creating` / `editing`) is open;
                  otherwise this read-only view holds nothing to lose. */}
              <ErrorNotice
                title={i18nT('pages.hooksPage.failed_to_load', { section: provider.labels.hooksSection.toLowerCase() })}
                message={providerHookErr instanceof Error ? providerHookErr.message : String(providerHookErr)}
                askAgent={handoffSafe}
                className="flex-1"
              />
              <Btn onClick={() => refetchProviderHooks()} className="shrink-0">{i18nT('pages.hooksPage.retry')}</Btn>
            </div>
          ) : Object.values(providerHooks).some(entries => entries.length > 0) ? (
            // Focusable, named scrollport. This table is read-only — every cell
            // is plain text — and its columns reserve 700px, so at phone width
            // ~412px of the Command column is clipped and can only be reached by
            // scrolling. Two separate problems, and the tabIndex is not the whole
            // fix for either:
            //   Reach. Chromium >=130 already focuses a scroller that has no
            //   focusable children, so Chrome alone is fine. That behaviour came
            //   through blink-dev and no other engine has shipped it, and the
            //   accessibility rule engines (axe scrollable-region-focusable, IBM,
            //   BrowserStack) still require the explicit stop, so we keep it.
            //   Name. Even where the scroller IS auto-focused, it lands focus on
            //   an anonymous <div>. role + aria-label are what stop it announcing
            //   as nothing in particular, in every engine including Chrome.
            // Do NOT copy this onto the hooks table above. Its rows hold tabbable
            // controls, which is exactly the case Chromium excludes and where
            // focus already arrives via the control; a stop there would insert a
            // redundant Tab press between every row.
            <div
              className="overflow-x-auto"
              // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- the tab stop IS the a11y fix, per the note above: a scrollport whose content holds no focusable child is unreachable by keyboard in every engine but Chromium >=130, and axe/IBM still require the explicit stop. `role="region"` + `aria-label` are what keep it from announcing as an anonymous div.
              tabIndex={0}
              role="region"
              aria-label={provider.labels.hooksSection}
            >
              <table className="w-full border-collapse table-striped">
                <thead>
                  <tr>
                    {[{ h: '#', w: 'w-[40px]' }, { h: i18nT('pages.hooksPage.event'), w: 'w-[150px]' }, { h: i18nT('pages.hooksPage.source'), w: 'w-[90px]' }, { h: i18nT('pages.hooksPage.matcher'), w: 'w-[120px]' }, { h: i18nT('pages.hooksPage.command'), w: 'min-w-[300px]' }].map(c => (
                      <th key={c.h} className={`text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium ${c.w}`}>{c.h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {(() => {
                    let order = 0
                    return Object.entries(providerHooks).sort(([a], [b]) => (EVENT_ORDER[normalizeEvent(a)] ?? 999) - (EVENT_ORDER[normalizeEvent(b)] ?? 999)).map(([event, entries]) =>
                      entries.map((entry, i) => {
                        order++
                        return (
                          <tr key={`${event}-${i}`} className={`hover:bg-bg-hover transition-colors ${entry.source === 'bundled' ? 'bg-bg-elevated/50' : ''}`}>
                            <td className="px-2.5 py-2 border-b border-border text-[12px] text-muted font-mono">{order}</td>
                            <td className="px-2.5 py-2 border-b border-border text-sm"><Badge variant={EVENT_BADGE[normalizeEvent(event)] || 'warn'}>{normalizeEvent(event)}</Badge></td>
                            <td className="px-2.5 py-2 border-b border-border text-sm">{entry.source === 'bundled' ? <span className="inline-flex items-center gap-1"><Lock className="w-3 h-3 text-muted" /><Badge variant="ok">{i18nT('pages.hooksPage.bundled')}</Badge></span> : <Badge variant="warn">{i18nT('pages.hooksPage.user')}</Badge>}</td>
                            <td className="px-2.5 py-2 border-b border-border text-sm text-muted">{entry.matcher ? entry.matcher : <span className="italic">—</span>}</td>
                            <td className="px-2.5 py-2 border-b border-border text-sm font-mono text-text/80" title={entry.command}><div className="truncate max-w-[400px]">{entry.command}</div></td>
                          </tr>
                        )
                      })
                    )
                  })()}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState icon={<Link2 className="lucide-inline" />} title={i18nT('pages.hooksPage.none_configured', { section: provider.labels.hooksSection.toLowerCase() })} subtitle={provider.labels.configFile ? i18nT('pages.hooksPage.configure_via', { path: provider.labels.configFile }) : ''} />
          )}
        </Card>
        )}
      </>
  )

  if (embedded) return content

  return (
    <>
      <PageHeader title={i18nT('pages.hooksPage.hooks')} subtitle={i18nT('pages.hooksPage.shell_commands_that_run_automatically_on_agent_e')} />
      <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        {content}
      </div>
    </>
  )
}
