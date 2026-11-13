import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Repeat, PenLine } from 'lucide-react'
import { api, type RecurringFailure } from '../../api/client'
import { apiErrorCode, isPermissionRefusal } from '../../api/apiError'
import { STALE_OWNER_SESSION_CODE } from '../../api/staleOwnerSignal'
import { Card, CardTitle, Btn, Badge, EmptyState, Input } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { useScrollEdges } from '../../hooks/useScrollEdges'
import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import RepeatedFailuresNoticeToggle from './RepeatedFailuresNoticeToggle'

/**
 * Agent tool failures that keep coming back, read from the security event log
 * (`GET /api/sel/recurring-failures`), each one an offer to write a lesson.
 *
 * Nothing is written until the owner presses Save on a draft they have read and
 * can edit: the draft is built from the finding's tool name and error text, both
 * stored text the agent's own runs produced, so the person is the one who
 * decides it becomes a standing rule.
 */

const TREND_KEYS = {
  new: 'pages.overview.memoryFailuresCard.trend_new',
  growing: 'pages.overview.memoryFailuresCard.trend_growing',
  steady: 'pages.overview.memoryFailuresCard.trend_steady',
} as const

const TREND_VARIANT = { new: 'warn', growing: 'err', steady: 'muted' } as const

/** Literal class names, so Tailwind sees each one. */
const NOTE_CLASS = { ok: 'text-ok', warn: 'text-warn' } as const

/** The name a row goes by: the MCP tool or class error, else the rule, else the tool kind. */
const failureName = (row: RecurringFailure) => row.tool || row.rule_id || row.tool_kind

/** This card's element id, the landing spot for that focus. */
export const FAILURES_CARD_ID = 'memory-failures-card'

/** The query this card reads. */
const MEMORY_FAILURES_QUERY_KEY = ['sel-recurring-failures'] as const

/** The route is owner only. A teammate's 403 `owner_only` is an answer no
 *  retry changes, so it is cached as data (`null`), not as an error: a re-read
 *  on refresh cannot flash the error notice. */
async function readFailures() {
  try {
    return await api.recurringFailures()
  } catch (e) {
    if (isPermissionRefusal(e)) return null
    throw e
  }
}

/** A session minted before the owner claim gets 401 `stale_session_reauth`,
 *  which the client answers with its own re-auth banner. It stays a query
 *  error, so the banner's sign-in re-reads it, and the card hides meanwhile. */
const isStaleOwnerSession = (e: unknown) => apiErrorCode(e) === STALE_OWNER_SESSION_CODE

type SaveNote = { tone: 'ok' | 'warn'; text: string }
type SaveError = { title: string; message: string }

export default function MemoryFailuresCard({ refreshTrigger, onLessonSaved, onDirtyChange, focus = false }: {
  /** The Memory view's refresh signal: a change re-reads the findings. */
  refreshTrigger?: number
  onLessonSaved?: () => unknown
  /** True while a draft is open, so the page's leave guards can hold it. */
  onDirtyChange?: (dirty: boolean) => void
  /** Scroll this card into view once its findings have loaded. */
  focus?: boolean
}) {
  const failures = useQuery({
    queryKey: MEMORY_FAILURES_QUERY_KEY,
    queryFn: readFailures,
    // The shared client never marks a read stale: a remount after an off-page
    // refresh must read again, as MemoryDocCard does.
    refetchOnMount: 'always',
  })
  // The shared client never marks a read stale, so the page's refresh is what
  // re-reads it; the value on mount is the first read's, not a refresh.
  const queryClient = useQueryClient()
  const [attachScroller, tableEdges, , attachTable] = useScrollEdges<HTMLDivElement>()
  const mountTrigger = useRef(refreshTrigger)
  useEffect(() => {
    if (refreshTrigger !== mountTrigger.current) void queryClient.invalidateQueries({ queryKey: MEMORY_FAILURES_QUERY_KEY })
  }, [refreshTrigger, queryClient])
  const focused = useRef(false)
  useEffect(() => {
    if (!focus || !failures.isSuccess || failures.data === null || focused.current) return
    focused.current = true
    document.getElementById(FAILURES_CARD_ID)?.scrollIntoView({ block: 'start' })
  }, [focus, failures.isSuccess, failures.data])
  const [draft, setDraft] = useState<{ signature: string; name: string; text: string; saving?: boolean } | null>(null)
  const [note, setNote] = useState<{ signature: string } & SaveNote | null>(null)
  const [saveError, setSaveError] = useState<SaveError | null>(null)
  const dirty = draft !== null
  // In a ref, as MemoryDocCard does: the page passes a fresh arrow each render.
  const dirtySignal = useRef(onDirtyChange)
  dirtySignal.current = onDirtyChange
  useEffect(() => { dirtySignal.current?.(dirty) }, [dirty])
  useEffect(() => () => dirtySignal.current?.(false), [])

  const save = async () => {
    if (!draft) return
    const { signature, name, text } = draft
    setSaveError(null)
    setDraft({ signature, name, text, saving: true })
    let result: Awaited<ReturnType<typeof api.createLesson>>
    try {
      result = await api.createLesson(text.trim(), 'tool')
    } catch (e) {
      setDraft({ signature, name, text })
      setSaveError({ title: i18nT('pages.overview.memoryTab.lesson_request_failed'), message: e instanceof Error ? e.message : String(e) })
      return
    }
    if (result.outcome === 'inserted' || result.outcome === 'enriched') {
      setDraft(null)
      setNote({ signature, tone: 'ok', text: i18nT('pages.overview.memoryFailuresCard.lesson_saved') })
      // The lesson is stored; a failed re-read of the list gets its own notice.
      try {
        await onLessonSaved?.()
      } catch (e) {
        setSaveError({ title: i18nT('pages.overview.memoryFailuresCard.saved_refresh_failed'), message: e instanceof Error ? e.message : String(e) })
      }
      return
    }
    setDraft({ signature, name, text })
    if (result.outcome === 'unchanged' || result.outcome === 'deduped') {
      setNote({
        signature,
        ...(result.outcome === 'unchanged'
          ? { tone: 'ok', text: i18nT('pages.overview.memoryTab.lesson_already_stored') }
          : { tone: 'warn', text: i18nT('pages.overview.memoryTab.lesson_already_covered', { reason: result.reason }) }),
      })
      return
    }
    setSaveError({ title: i18nT('pages.overview.memoryTab.lesson_request_failed'), message: i18nT('pages.overview.memoryTab.lesson_not_saved', { reason: result.reason }) })
  }

  if (failures.data === null || isStaleOwnerSession(failures.error)) return null
  const rows = failures.data?.failures ?? []
  const th = 'text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium'
  const td = 'px-2.5 py-2 border-b border-border text-sm align-top'

  return (
    <Card id={FAILURES_CARD_ID}>
      <CardTitle>{i18nT('pages.overview.memoryFailuresCard.title')}</CardTitle>
      <p className="text-[12px] leading-relaxed text-muted mb-2">{i18nT('pages.overview.memoryFailuresCard.hint')}</p>
      <RepeatedFailuresNoticeToggle />
      {/* No hand-off: a draft lesson in this card may be unsaved. */}
      <ErrorNotice
        title={i18nT('pages.overview.memoryFailuresCard.load_failed')}
        message={failures.error ? (failures.error instanceof Error ? failures.error.message : String(failures.error)) : null}
        askAgent={false}
        className="mb-2"
      />
      {/* No hand-off: it would discard the draft being saved. */}
      <ErrorNotice
        title={saveError?.title ?? i18nT('pages.overview.memoryTab.lesson_request_failed')}
        message={saveError?.message ?? null}
        onDismiss={() => setSaveError(null)}
        askAgent={false}
        className="mb-2"
      />
      {failures.isPending && <p role="status" className="text-[13px] text-muted">{i18nT('pages.overview.memoryTab.loading')}</p>}
      {failures.isSuccess && (
        // The wrapper anchors the edge cue: a scroller's own absolute children scroll with it.
        <div className="relative"><div ref={attachScroller} className="overflow-x-auto"><table ref={attachTable} className="w-full border-collapse table-striped">
          <thead>
            <tr>
              <th className={th}>{i18nT('pages.overview.memoryFailuresCard.failure')}</th>
              <th className={th}>{i18nT('pages.overview.memoryFailuresCard.times')}</th>
              <th className={th}>{i18nT('pages.overview.memoryFailuresCard.chats')}</th>
              <th className={th}>{i18nT('pages.overview.memoryFailuresCard.days')}</th>
              <th className={th}>{i18nT('pages.overview.memoryFailuresCard.trend')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr><td colSpan={5}>
                <EmptyState
                  icon={<Repeat className="lucide-inline" />}
                  title={i18nT('pages.overview.memoryFailuresCard.empty_title')}
                  subtitle={i18nT('pages.overview.memoryFailuresCard.empty_hint')}
                />
              </td></tr>
            ) : rows.map(row => {
              const editing = draft?.signature === row.signature ? draft : null
              const rowNote = note?.signature === row.signature ? note : null
              return (
                <tr key={row.signature} className="hover:bg-bg-hover transition-colors" data-testid="recurring-failure-row">
                  <td className={td}>
                    {/* Stored text, rendered as React text children (never a key, never markup). */}
                    <span className="font-mono">{failureName(row)}</span>
                    {row.error && row.error !== row.tool && <span className="block text-[12px] text-muted break-words">{row.error}</span>}
                    {rowNote && (
                      <span role="status" className={`block mt-1 text-[13px] ${NOTE_CLASS[rowNote.tone]}`}>{rowNote.text}</span>
                    )}
                    {/* In the first column, so a narrow screen never scrolls the action out of view. */}
                    {!editing && (
                      <Btn
                        className="whitespace-nowrap mt-1.5"
                        // One draft at a time: opening another would replace the open one's text.
                        disabled={draft !== null}
                        onClick={() => {
                          setNote(null)
                          setSaveError(null)
                          setDraft({
                            signature: row.signature,
                            name: failureName(row),
                            text: i18nT('pages.overview.memoryFailuresCard.draft_text', { failure: failureName(row) }),
                          })
                        }}
                      >
                        <PenLine className="lucide-inline" aria-hidden="true" /> {i18nT('pages.overview.memoryFailuresCard.draft_lesson')}
                      </Btn>
                    )}
                  </td>
                  <td className={td}>{fmtNumber(row.hits)}</td>
                  <td className={td}>{fmtNumber(row.sessions)}</td>
                  <td className={td}>{fmtNumber(row.days)}</td>
                  <td className={td}><Badge variant={TREND_VARIANT[row.trend]}>{i18nT(TREND_KEYS[row.trend])}</Badge></td>
                </tr>
              )
            })}
          </tbody>
        </table></div>
          {/* The table runs past a narrow card: the fade says the columns continue. */}
          {tableEdges.right && <div aria-hidden="true" data-testid="recurring-failures-cue-right" className="pointer-events-none absolute right-0 top-0 bottom-0 w-6 bg-gradient-to-l from-card to-transparent" />}
        </div>
      )}
      {/* The open draft sits under the table, outside its sideways scroller, so its
          input, Save and Cancel always fit the card. */}
      {draft && (
        <div className="mt-3 flex gap-2 flex-wrap items-center" data-testid="recurring-failure-draft">
          <span className="basis-full text-[12px] text-muted">
            {i18nT('pages.overview.memoryFailuresCard.draft_for')} <span className="font-mono">{draft.name}</span>
          </span>
          <Input
            className="basis-full"
            aria-label={i18nT('pages.overview.memoryFailuresCard.lesson_text')}
            autoFocus
            disabled={draft.saving}
            value={draft.text}
            onChange={e => setDraft({ ...draft, text: e.target.value })}
          />
          <Btn primary disabled={!draft.text.trim() || draft.saving} onClick={() => { void save() }}>{i18nT('pages.overview.memoryFailuresCard.save_lesson')}</Btn>
          <Btn disabled={draft.saving} onClick={() => { setDraft(null); setSaveError(null) }}>{i18nT('pages.overview.memoryTab.cancel')}</Btn>
        </div>
      )}
    </Card>
  )
}
