import { useMemo, useCallback, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { AlertCircle, Loader2, CheckCircle2, ArrowRight, Sparkles, Check, X, CheckSquare, Square } from 'lucide-react'
import { useAppSelector, useAppDispatch } from '../store'
import { switchSlot, resolveByApprovalId } from '../store/chatSlice'
import { fetchSlots } from '../store/dashboardSlice'
import { api } from '../api/client'
import { PageHeader, Card, CardTitle, Badge, Btn, EmptyState } from '../components/ui'
import Clickable from '../components/Clickable'
import { i18nT } from '../i18n/t'
import type { ChatSlot } from '../types'

/**
 * Today — mission-control view (Core Experience redesign, Story 1).
 *
 * Read-only attention surface that reframes existing chat slots into three
 * buckets: Needs You → Working → Completed — with semantic status labels
 * (never "Thinking…") and inline approve/deny for tool-approval items.
 *
 * Derives everything from `dashboard.slots` — no new store, no backend.
 */

type Bucket = 'needsYou' | 'working' | 'completed'

const isApprovalSlot = (s: ChatSlot) => !!(s.pending_approval || s.pending_approval_info)

/** Semantic status — never "Thinking…". Exported for unit testing. */
export function classify(slot: ChatSlot): { bucket: Bucket; label: string } {
  const needsYou = (slot.pending_approval || slot.pending_approval_info || slot.needs_input)
  if (needsYou && isApprovalSlot(slot)) return { bucket: 'needsYou', label: i18nT('pages.todayPage.needs_approval') }
  if (needsYou) return { bucket: 'needsYou', label: i18nT('pages.todayPage.decision_required') }
  if (slot.stop_state === 'killing' || slot.stopping) return { bucket: 'working', label: i18nT('pages.todayPage.stopping') }
  if (slot.subagents_running) return { bucket: 'working', label: i18nT('pages.todayPage.working_agents') }
  if (slot.running) return { bucket: 'working', label: i18nT('pages.todayPage.working') }
  return { bucket: 'completed', label: i18nT('pages.todayPage.idle') }
}

const badgeVariant: Record<Bucket, 'warn' | 'aim' | 'ok'> = { needsYou: 'warn', working: 'aim', completed: 'ok' }

function tsOf(s: ChatSlot): number {
  const t = s.last_activity_ts || s.last_ts || s.created
  return t ? Date.parse(t) : 0
}

function originOf(s: ChatSlot): string {
  if (s.slack_linked) return i18nT('pages.todayPage.slack_origin', { channel: s.slack_channel ?? i18nT('pages.todayPage.thread') })
  return s.agent || i18nT('pages.todayPage.crew_fallback')
}

const SECTIONS: { bucket: Bucket; heading: () => string; icon: React.ReactNode }[] = [
  { bucket: 'needsYou', heading: () => i18nT('pages.todayPage.needs_you'), icon: <AlertCircle className="lucide-inline" /> },
  { bucket: 'working', heading: () => i18nT('pages.todayPage.working_heading'), icon: <Loader2 className="lucide-inline" /> },
  { bucket: 'completed', heading: () => i18nT('pages.todayPage.completed'), icon: <CheckCircle2 className="lucide-inline" /> },
]

export default function TodayPage() {
  const navigate = useNavigate()
  const dispatch = useAppDispatch()
  const slots = useAppSelector(s => s.dashboard.slots)

  const [selected, setSelected] = useState<ChatSlot | null>(null)
  const [busy, setBusy] = useState<Record<string, 'approve' | 'reject' | undefined>>({})
  const [errKey, setErrKey] = useState<string | null>(null)
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [batchBusy, setBatchBusy] = useState(false)

  const grouped = useMemo(() => {
    const g: Record<Bucket, { slot: ChatSlot; label: string }[]> = { needsYou: [], working: [], completed: [] }
    for (const slot of slots) {
      const { bucket, label } = classify(slot)
      g[bucket].push({ slot, label })
    }
    for (const k of Object.keys(g) as Bucket[]) g[k].sort((a, b) => tsOf(b.slot) - tsOf(a.slot))
    return g
  }, [slots])

  const openFullSession = useCallback((key: string, e?: React.MouseEvent | React.KeyboardEvent) => {
    if (e && ((e as React.MouseEvent).metaKey || (e as React.MouseEvent).ctrlKey)) {
      window.open(`/chat/${encodeURIComponent(key)}?sid=${encodeURIComponent(key)}`, '_blank', 'noopener,noreferrer')
      return
    }
    setSelected(null)
    dispatch(switchSlot(key))
    navigate('/chat')
  }, [dispatch, navigate])

  // Act without leaving Today: resolve the slot's pending tool-approval.
  const resolve = useCallback(async (slot: ChatSlot, action: 'approve' | 'reject') => {
    setBusy(b => ({ ...b, [slot.key]: action }))
    setErrKey(null)
    // The reducer stores `decision` verbatim and renderers gate rejection on
    // `resolved === 'rejected'`, so translate the API verb to the marker value.
    const decision = action === 'approve' ? 'approved' : 'rejected'
    try {
      // Use the slot-scoped endpoint (approveChatSlot) rather than the global
      // resolveApproval — ACP request_ids are connection-scoped and can collide
      // across unrelated sessions.
      const approvalId = (slot.pending_approval_info as { request_id?: string } | null)?.request_id
      const extra: Record<string, string> = approvalId ? { request_id: approvalId } : {}
      await api.approveChatSlot(slot.key, action === 'approve' ? 'approved' : 'rejected', extra)
      if (approvalId) {
        dispatch(resolveByApprovalId({ id: approvalId, decision }))
      }
      await dispatch(fetchSlots())
      setSelected(cur => (cur?.key === slot.key ? null : cur))
    } catch (err) {
      console.error('Today approve/deny failed:', err) // eslint-disable-line no-console
      setErrKey(slot.key)
    } finally {
      setBusy(b => ({ ...b, [slot.key]: undefined }))
    }
  }, [dispatch])

  // Batch approve/deny all checked approval slots
  const batchResolve = useCallback(async (action: 'approve' | 'reject') => {
    if (checked.size === 0) return
    setBatchBusy(true)
    const targets = grouped.needsYou.filter(({ slot }) => checked.has(slot.key) && isApprovalSlot(slot))
    const decision = action === 'approve' ? 'approved' : 'rejected'
    for (const { slot } of targets) {
      try {
        const approvalId = (slot.pending_approval_info as { request_id?: string } | null)?.request_id
        const extra: Record<string, string> = approvalId ? { request_id: approvalId } : {}
        await api.approveChatSlot(slot.key, action === 'approve' ? 'approved' : 'rejected', extra)
        if (approvalId) {
          dispatch(resolveByApprovalId({ id: approvalId, decision }))
        }
      } catch { /* individual failures don't block the batch */ }
    }
    await dispatch(fetchSlots())
    setChecked(new Set())
    setBatchBusy(false)
  }, [checked, grouped.needsYou, dispatch])

  const toggleCheck = useCallback((key: string) => {
    setChecked(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  const approvalSlotKeys = useMemo(
    () => grouped.needsYou.filter(({ slot }) => isApprovalSlot(slot)).map(({ slot }) => slot.key),
    [grouped.needsYou],
  )

  const toggleAll = useCallback(() => {
    setChecked(prev =>
      prev.size === approvalSlotKeys.length ? new Set() : new Set(approvalSlotKeys),
    )
  }, [approvalSlotKeys])

  const needsYou = grouped.needsYou.length

  function ActionButtons({ slot, size }: { slot: ChatSlot; size: 'row' | 'drawer' }) {
    const state = busy[slot.key]
    const disabled = !!state
    if (!isApprovalSlot(slot)) {
      return <Btn onClick={(e) => { e.stopPropagation(); openFullSession(slot.key) }} className={size === 'row' ? 'text-[12px] px-2 py-1' : ''}>{i18nT('pages.todayPage.open_to_respond')}</Btn>
    }
    return (
      // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions -- stopPropagation prevents row click from opening session
      <div className="flex items-center gap-1.5" role="group" aria-label={i18nT('pages.todayPage.approval_actions')} onClick={(e) => e.stopPropagation()}>
        <Btn primary disabled={disabled} onClick={() => resolve(slot, 'approve')} className={`flex items-center gap-1 ${size === 'row' ? 'text-[12px] px-2 py-1' : ''}`}>
          {state === 'approve' ? <Loader2 className="lucide-inline animate-spin" /> : <Check className="lucide-inline" />} {i18nT('pages.todayPage.approve')}
        </Btn>
        <Btn danger disabled={disabled} onClick={() => resolve(slot, 'reject')} className={`flex items-center gap-1 ${size === 'row' ? 'text-[12px] px-2 py-1' : ''}`}>
          {state === 'reject' ? <Loader2 className="lucide-inline animate-spin" /> : <X className="lucide-inline" />} {i18nT('pages.todayPage.deny')}
        </Btn>
      </div>
    )
  }

  function WorkRow({ slot, label, bucket }: { slot: ChatSlot; label: string; bucket: Bucket }) {
    const title = slot.title?.trim() || slot.last_message?.trim() || slot.prompt_preview?.trim() || i18nT('pages.todayPage.untitled_session')
    const showCheckbox = bucket === 'needsYou' && isApprovalSlot(slot)
    return (
      <Clickable
        onClick={(e) => {
          // Modifier-click always opens in new tab regardless of bucket
          if ((e as React.MouseEvent).metaKey || (e as React.MouseEvent).ctrlKey) {
            openFullSession(slot.key, e)
            return
          }
          if (bucket === 'needsYou') {
            setSelected(slot)
          } else {
            openFullSession(slot.key, e)
          }
        }}
        className="group flex items-center gap-3 py-2.5 px-3 rounded-lg hover:bg-bg-hover w-full text-left"
      >
        {showCheckbox && (
          <button
            type="button"
            className="shrink-0 text-muted hover:text-text cursor-pointer bg-transparent border-none p-0"
            aria-label={checked.has(slot.key) ? i18nT('pages.todayPage.deselect') : i18nT('pages.todayPage.select')}
            onClick={(e) => { e.stopPropagation(); toggleCheck(slot.key) }}
          >
            {checked.has(slot.key)
              ? <CheckSquare className="lucide-inline" size={16} />
              : <Square className="lucide-inline" size={16} />}
          </button>
        )}
        <div className="min-w-0 flex-1">
          <div className="truncate text-[14px] text-text-strong">{title}</div>
          <div className="truncate text-[12px] text-muted">{originOf(slot)}</div>
          {errKey === slot.key && <div className="text-[11px] text-danger mt-0.5">{i18nT('pages.todayPage.resolve_error')}</div>}
        </div>
        <Badge variant={badgeVariant[bucket]}>{label}</Badge>
        <ArrowRight className="lucide-inline shrink-0 text-muted opacity-0 group-hover:opacity-100 transition-opacity" />
      </Clickable>
    )
  }

  return (
    <>
      <PageHeader
        title={i18nT('pages.todayPage.title')}
        subtitle={needsYou ? i18nT('pages.todayPage.items_need_you', { count: needsYou }) : i18nT('pages.todayPage.nothing_needs_you')}
      />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        {slots.length === 0 ? (
          <EmptyState
            icon={<Sparkles className="lucide-inline" />}
            title={i18nT('pages.todayPage.empty_title')}
            subtitle={i18nT('pages.todayPage.empty_subtitle')}
          />
        ) : (
          <div className="flex flex-col gap-4">
            {SECTIONS.map(({ bucket, heading, icon }) => {
              const rows = grouped[bucket]
              if (bucket === 'completed' && rows.length === 0) return null
              return (
                <Card key={bucket}>
                  <CardTitle className="flex items-center gap-2">
                    {icon}<span>{heading()}</span>
                    <span className="text-muted text-[12px] font-normal">· {rows.length}</span>
                    {bucket === 'needsYou' && approvalSlotKeys.length > 0 && (
                      <button
                        type="button"
                        className="ml-auto text-[11px] text-muted hover:text-text cursor-pointer bg-transparent border-none p-0 font-body"
                        onClick={toggleAll}
                      >
                        {checked.size === approvalSlotKeys.length ? i18nT('pages.todayPage.deselect_all') : i18nT('pages.todayPage.select_all')}
                      </button>
                    )}
                  </CardTitle>
                  {rows.length === 0
                    ? <EmptyState icon={null} title={i18nT('pages.todayPage.none_in_bucket')} />
                    : <div className="flex flex-col mt-1">{rows.map(({ slot, label }) => <WorkRow key={slot.key} slot={slot} label={label} bucket={bucket} />)}</div>}
                </Card>
              )
            })}
          </div>
        )}
      </div>

      {/* Floating batch action bar */}
      {checked.size > 0 && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 flex flex-col gap-2 bg-card border border-border shadow-xl rounded-xl px-5 py-3 animate-scale-in">
          <div className="flex items-center justify-between gap-3">
            <span className="text-[13px] text-text-strong font-medium">{i18nT('pages.todayPage.batch_selected', { count: checked.size })}</span>
            <button
              type="button"
              className="text-[12px] text-muted hover:text-text cursor-pointer bg-transparent border-none p-0"
              onClick={() => setChecked(new Set())}
            >{i18nT('pages.todayPage.cancel')}</button>
          </div>
          <div className="flex items-center gap-3">
            <Btn primary disabled={batchBusy} onClick={() => batchResolve('approve')} className="flex items-center gap-1">
              {batchBusy ? <Loader2 className="lucide-inline animate-spin" /> : <Check className="lucide-inline" />}
              {i18nT('pages.todayPage.approve_all')}
            </Btn>
            <Btn danger disabled={batchBusy} onClick={() => batchResolve('reject')} className="flex items-center gap-1">
              <X className="lucide-inline" /> {i18nT('pages.todayPage.deny_all')}
            </Btn>
          </div>
        </div>
      )}

      {/* Focused drawer — act without leaving Today. */}
      {selected && (
        <>
          <Clickable
            onClick={() => setSelected(null)}
            className="fixed inset-0 bg-[var(--bg)]/60 backdrop-blur-sm z-40"
            aria-label={i18nT('pages.todayPage.close_drawer')}
          />
          <div className="fixed top-0 right-0 h-full w-[420px] max-w-[90vw] bg-card border-l border-border shadow-xl z-50 flex flex-col" role="dialog" aria-label={i18nT('pages.todayPage.session_details')}>
            <div className="flex items-start justify-between gap-3 px-5 py-4 border-b border-border">
              <div className="min-w-0">
                <div className="text-[15px] font-semibold text-text-strong truncate">
                  {selected.title?.trim() || selected.last_message?.trim() || i18nT('pages.todayPage.untitled_session')}
                </div>
                <div className="text-[12px] text-muted mt-0.5 truncate">{originOf(selected)}</div>
              </div>
              <Clickable onClick={() => setSelected(null)} className="shrink-0 p-1 rounded-md hover:bg-bg-hover text-muted" aria-label={i18nT('pages.todayPage.close')}>
                <X className="lucide-inline" />
              </Clickable>
            </div>
            <div className="px-5 py-4 flex-1 min-h-0 overflow-y-auto flex flex-col gap-3">
              <div>
                <div className="text-[11px] uppercase tracking-wide text-muted mb-1">{i18nT('pages.todayPage.status')}</div>
                <Badge variant={badgeVariant[classify(selected).bucket]}>{classify(selected).label}</Badge>
              </div>
              {/* Show what's being approved — tool name + input preview */}
              {isApprovalSlot(selected) && selected.pending_approval_info && (
                <div className="rounded-md border border-border bg-bg-elevated px-3 py-2">
                  <div className="text-[11px] uppercase tracking-wide text-muted mb-1">{i18nT('pages.todayPage.tool_request')}</div>
                  <div className="text-[13px] text-text-strong font-medium truncate">
                    {(selected.pending_approval_info as { tool?: string }).tool || i18nT('pages.todayPage.unknown_tool')}
                  </div>
                  {(selected.pending_approval_info as { tool_input?: string }).tool_input && (
                    <pre className="mt-1.5 text-[12px] text-muted whitespace-pre-wrap break-all max-h-[120px] overflow-y-auto font-mono">
                      {(selected.pending_approval_info as { tool_input?: string }).tool_input}
                    </pre>
                  )}
                </div>
              )}
              <div className="text-[13px] text-muted">
                {isApprovalSlot(selected)
                  ? i18nT('pages.todayPage.approval_prompt')
                  : i18nT('pages.todayPage.decision_prompt')}
              </div>
              {errKey === selected.key && <div className="text-[12px] text-danger">{i18nT('pages.todayPage.resolve_error_drawer')}</div>}
            </div>
            <div className="px-5 py-4 border-t border-border flex flex-col gap-2">
              <ActionButtons slot={selected} size="drawer" />
              <Btn onClick={() => openFullSession(selected.key)} className="flex items-center justify-center gap-1 w-full">
                <ArrowRight className="lucide-inline" /> {i18nT('pages.todayPage.open_session')}
              </Btn>
            </div>
          </div>
        </>
      )}
    </>
  )
}
