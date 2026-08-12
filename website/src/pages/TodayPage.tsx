import { useMemo, useCallback, useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'
import { AlertCircle, Loader2, CheckCircle2, ArrowRight, Sparkles, Check, X, Plus } from 'lucide-react'
import { useAppSelector, useAppDispatch } from '../store'
import { createSlot, switchSlot, resolveByApprovalId } from '../store/chatSlice'
import { fetchSlots } from '../store/dashboardSlice'
import { api } from '../api/client'
import { PageHeader, Card, CardTitle, Badge, Btn, EmptyState } from '../components/ui'
import Clickable from '../components/Clickable'
import type { ChatSlot } from '../types'

/**
 * Today — mission-control view (Core Experience redesign, Story 1).
 *
 * Read-only attention surface that reframes existing chat slots into three
 * buckets: Needs You → Working → Completed — with semantic status labels
 * (never "Thinking…"). Includes a Delegate Work button (⌘K) as the Phase-1
 * stand-in for the full Delegate Composer.
 *
 * Derives everything from `dashboard.slots` — no new store, no backend.
 *
 * Phase 2 adds inline Approve/Deny without leaving Today, plus a focused
 * drawer for acting on Needs-You items.
 */

type Bucket = 'needsYou' | 'working' | 'completed'

const isApprovalSlot = (s: ChatSlot) => !!(s.pending_approval || s.pending_approval_info)

/** Semantic status — never "Thinking…". */
function classify(slot: ChatSlot): { bucket: Bucket; label: string } {
  const needsYou = (slot.pending_approval || slot.pending_approval_info || slot.waiting_for_input || slot.has_options)
  if (needsYou && isApprovalSlot(slot)) return { bucket: 'needsYou', label: 'Needs approval' }
  if (needsYou) return { bucket: 'needsYou', label: 'Decision required' }
  if (slot.stop_state === 'killing' || slot.stopping) return { bucket: 'working', label: 'Stopping' }
  if (slot.subagents_running) return { bucket: 'working', label: 'Working · agents' }
  if (slot.running) return { bucket: 'working', label: 'Working' }
  return { bucket: 'completed', label: 'Idle' }
}

const badgeVariant: Record<Bucket, 'warn' | 'aim' | 'ok'> = { needsYou: 'warn', working: 'aim', completed: 'ok' }

function tsOf(s: ChatSlot): number {
  const t = s.last_activity_ts || s.last_ts || s.created
  return t ? Date.parse(t) : 0
}

function originOf(s: ChatSlot): string {
  if (s.slack_linked) return `Slack · ${s.slack_channel ?? 'thread'}`
  return s.agent || 'Crew'
}

const SECTIONS: { bucket: Bucket; heading: string; icon: React.ReactNode }[] = [
  { bucket: 'needsYou', heading: 'Needs You', icon: <AlertCircle className="lucide-inline" /> },
  { bucket: 'working', heading: 'Working', icon: <Loader2 className="lucide-inline" /> },
  { bucket: 'completed', heading: 'Completed', icon: <CheckCircle2 className="lucide-inline" /> },
]

export default function TodayPage() {
  const navigate = useNavigate()
  const dispatch = useAppDispatch()
  const slots = useAppSelector(s => s.dashboard.slots)

  const [selected, setSelected] = useState<ChatSlot | null>(null)
  const [busy, setBusy] = useState<Record<string, 'approve' | 'reject' | undefined>>({})
  const [errKey, setErrKey] = useState<string | null>(null)

  // Delegate Work — creates a new chat session and navigates to /chat.
  // useMutation isPending guard prevents duplicate session creation on rapid clicks.
  const delegateMutation = useMutation({
    mutationFn: () => dispatch(createSlot(undefined)).unwrap(),
    onSuccess: () => {
      navigate('/chat')
      requestAnimationFrame(() =>
        document.querySelector<HTMLTextAreaElement>('textarea[aria-label="Message input"]')?.focus(),
      )
    },
  })

  // Today-scoped ⌘K handler: intercepts at the capture phase (before the
  // global useCommandPalette listener on the bubble phase) so that ⌘K triggers
  // Delegate Work when the user is on /today, without disabling the global
  // command palette on other routes.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (
        (e.key === 'k' || e.key === 'K') &&
        (e.metaKey || e.ctrlKey) &&
        !e.altKey &&
        !e.shiftKey
      ) {
        e.preventDefault()
        e.stopPropagation()
        if (!delegateMutation.isPending) delegateMutation.mutate()
      }
    }
    window.addEventListener('keydown', handler, true) // capture phase
    return () => window.removeEventListener('keydown', handler, true)
  }, [delegateMutation])

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
    try {
      const list = await api.approvals()
      const entry = list.find(a => a.slot === slot.key)
      if (!entry) throw new Error('No pending approval found for this session.')
      await api.resolveApproval(entry.id, action)
      dispatch(resolveByApprovalId({ id: entry.id, decision: action }))
      await dispatch(fetchSlots())
      setSelected(cur => (cur?.key === slot.key ? null : cur))
    } catch (err) {
      console.error('Today approve/deny failed:', err) // eslint-disable-line no-console
      setErrKey(slot.key)
    } finally {
      setBusy(b => ({ ...b, [slot.key]: undefined }))
    }
  }, [dispatch])

  const grouped = useMemo(() => {
    const g: Record<Bucket, { slot: ChatSlot; label: string }[]> = { needsYou: [], working: [], completed: [] }
    for (const slot of slots) {
      const { bucket, label } = classify(slot)
      g[bucket].push({ slot, label })
    }
    for (const k of Object.keys(g) as Bucket[]) g[k].sort((a, b) => tsOf(b.slot) - tsOf(a.slot))
    return g
  }, [slots])

  const needsYou = grouped.needsYou.length

  function ActionButtons({ slot, size }: { slot: ChatSlot; size: 'row' | 'drawer' }) {
    const state = busy[slot.key]
    const disabled = !!state
    if (!isApprovalSlot(slot)) {
      return <Btn onClick={(e) => { e.stopPropagation(); openFullSession(slot.key) }} className={size === 'row' ? 'text-[12px] px-2 py-1' : ''}>Open to respond</Btn>
    }
    return (
      <div className="flex items-center gap-1.5" onClick={(e) => e.stopPropagation()}>
        <Btn primary disabled={disabled} onClick={() => resolve(slot, 'approve')} className={`flex items-center gap-1 ${size === 'row' ? 'text-[12px] px-2 py-1' : ''}`}>
          {state === 'approve' ? <Loader2 className="lucide-inline animate-spin" /> : <Check className="lucide-inline" />} Approve
        </Btn>
        <Btn danger disabled={disabled} onClick={() => resolve(slot, 'reject')} className={`flex items-center gap-1 ${size === 'row' ? 'text-[12px] px-2 py-1' : ''}`}>
          {state === 'reject' ? <Loader2 className="lucide-inline animate-spin" /> : <X className="lucide-inline" />} Deny
        </Btn>
      </div>
    )
  }

  function WorkRow({ slot, label, bucket }: { slot: ChatSlot; label: string; bucket: Bucket }) {
    const title = slot.title?.trim() || slot.last_message?.trim() || slot.prompt_preview?.trim() || 'Untitled session'
    return (
      <Clickable
        onClick={(e) => (bucket === 'completed' ? openFullSession(slot.key, e) : setSelected(slot))}
        className="group flex items-center gap-3 py-2.5 px-3 rounded-lg hover:bg-bg-hover w-full text-left"
      >
        <div className="min-w-0 flex-1">
          <div className="truncate text-[14px] text-text-strong">{title}</div>
          <div className="truncate text-[12px] text-muted">{originOf(slot)}</div>
          {errKey === slot.key && <div className="text-[11px] text-danger mt-0.5">Couldn't resolve — open the session to respond.</div>}
        </div>
        <Badge variant={badgeVariant[bucket]}>{label}</Badge>
        {bucket === 'needsYou'
          ? <ActionButtons slot={slot} size="row" />
          : <ArrowRight className="lucide-inline shrink-0 text-muted opacity-0 group-hover:opacity-100 transition-opacity" />}
      </Clickable>
    )
  }

  return (
    <>
      <PageHeader
        title="Today"
        subtitle={needsYou ? `${needsYou} ${needsYou === 1 ? 'item needs' : 'items need'} you` : 'Nothing needs you right now'}
        actions={
          <Btn
            primary
            disabled={delegateMutation.isPending}
            onClick={() => delegateMutation.mutate()}
            className="flex items-center gap-1.5"
            aria-label="Delegate Work"
          >
            {delegateMutation.isPending
              ? <Loader2 className="lucide-inline animate-spin" />
              : <Plus className="lucide-inline" />}
            <span>Delegate Work</span>
            <kbd className="ml-1 text-[10px] opacity-60 font-mono">⌘K</kbd>
          </Btn>
        }
      />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        {slots.length === 0 ? (
          <EmptyState
            icon={<Sparkles className="lucide-inline" />}
            title="Nothing needs you right now."
            subtitle="Work you delegate from Chat or a Slack thread shows up in Today."
          />
        ) : (
          <div className="flex flex-col gap-4">
            {SECTIONS.map(({ bucket, heading, icon }) => {
              const rows = grouped[bucket]
              if (bucket === 'completed' && rows.length === 0) return null
              return (
                <Card key={bucket}>
                  <CardTitle className="flex items-center gap-2">
                    {icon}<span>{heading}</span>
                    <span className="text-muted text-[12px] font-normal">· {rows.length}</span>
                  </CardTitle>
                  {rows.length === 0
                    ? <div className="text-[13px] text-muted px-3 py-2">None.</div>
                    : <div className="flex flex-col mt-1">{rows.map(({ slot, label }) => <WorkRow key={slot.key} slot={slot} label={label} bucket={bucket} />)}</div>}
                </Card>
              )
            })}
          </div>
        )}
      </div>

      {/* Focused drawer — act without leaving Today. */}
      {selected && (
        <>
          <div className="fixed inset-0 bg-black/30 z-40" onClick={() => setSelected(null)} />
          <div className="fixed top-0 right-0 h-full w-[420px] max-w-[90vw] bg-card border-l border-border shadow-xl z-50 flex flex-col">
            <div className="flex items-start justify-between gap-3 px-5 py-4 border-b border-border">
              <div className="min-w-0">
                <div className="text-[15px] font-semibold text-text-strong truncate">
                  {selected.title?.trim() || selected.last_message?.trim() || 'Untitled session'}
                </div>
                <div className="text-[12px] text-muted mt-0.5 truncate">{originOf(selected)}</div>
              </div>
              <Clickable onClick={() => setSelected(null)} className="shrink-0 p-1 rounded-md hover:bg-bg-hover text-muted" aria-label="Close">
                <X className="lucide-inline" />
              </Clickable>
            </div>
            <div className="px-5 py-4 flex-1 min-h-0 overflow-y-auto flex flex-col gap-3">
              <div>
                <div className="text-[11px] uppercase tracking-wide text-muted mb-1">Status</div>
                <Badge variant={badgeVariant[classify(selected).bucket]}>{classify(selected).label}</Badge>
              </div>
              <div className="text-[13px] text-muted">
                {isApprovalSlot(selected)
                  ? 'Review the request and approve or deny without leaving Today.'
                  : 'This session is waiting on a decision. Open it to see the options and respond.'}
              </div>
              {errKey === selected.key && <div className="text-[12px] text-danger">Couldn't resolve here — open the session to respond.</div>}
            </div>
            <div className="px-5 py-4 border-t border-border flex items-center justify-between gap-2">
              <ActionButtons slot={selected} size="drawer" />
              <Btn onClick={() => openFullSession(selected.key)} className="flex items-center gap-1">
                <ArrowRight className="lucide-inline" /> Open session
              </Btn>
            </div>
          </div>
        </>
      )}
    </>
  )
}
