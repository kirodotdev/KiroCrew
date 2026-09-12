import { useEffect, useRef, useState } from 'react'
import { EyeOff, Loader, Pen, Sparkles, VenetianMask } from 'lucide-react'
import { useImeGuard } from '../../hooks/useImeGuard'
import { Btn, Input } from '../../components/ui'
import Clickable from '../../components/Clickable'
import TypewriterText from '../../components/TypewriterText'
import { useQueryClient } from '@tanstack/react-query'
import { useAppDispatch, useAppSelector, useAppStore } from '../../store'
import { sseSlotTitle } from '../../store/dashboardSlice'
import { api } from '../../api/client'
import { errMessage } from '../../utils/thunkError'
import { i18nT } from '../../i18n/t'

/**
 * SessionTitleControl — the session title as ONE editable control: the
 * memory-mode glyph, the title (click / Enter to rename inline), the Pen that
 * reveals on `group/header` hover, and the Sparkles button that asks the LLM
 * for a new title (spinner while it runs).
 *
 * Shared by the single-session header (ChatPage) and every split-view pane
 * header (ChatPane) so the two cannot drift again — the pane header used to
 * render a bare title span with neither affordance (#9727). The HOST supplies
 * the `group/header` hover target; this control only renders the row.
 *
 * `editing` / `onEditingChange` are optional controlled state: ChatPage pins
 * its editor to the slot it opened on (`editingTitleSlot`) and opens it from
 * the header menu too, so it owns the flag; a pane leaves both unset and the
 * control keeps the flag itself.
 *
 * Failures are reported through `onError(message, title)`, never rendered
 * here: each host owns its own error surface (ChatPage's action banner, the
 * pane's inline ErrorNotice).
 */

/**
 * Per-slot recovery state for a refused rename (#10203, ported from the main
 * header so every host shares one failure semantics). `gen` is a monotonic
 * attempt generation: a recovery may apply ONLY while its own attempt is still
 * the slot's latest, so a delayed recovery can never overwrite anything a newer
 * attempt (failed or successful) did — title equality alone cannot tell a stale
 * optimistic value from a newer confirmed rename to the identical string.
 * `baseline` is the last CONFIRMED title; `inflight` holds this slot's own
 * un-settled optimistic titles, so a store title outside that set refreshes the
 * baseline at commit time (a success here, or another client's rename delivered
 * over SSE). The entry is dropped when the last pending attempt settles.
 *
 * Module-level and keyed by slot, not per control instance: the main header
 * re-targets one instance across slots and split view mounts one per pane, and
 * the guard is about the SLOT, so every mount must see the same record.
 */
type RenameRecovery = { baseline: string; inflight: Set<string>; gen: number }
const renameRecovery = new Map<string, RenameRecovery>()
export default function SessionTitleControl({
  slotKey,
  title,
  compact,
  editing: editingProp,
  onEditingChange,
  onError,
}: {
  slotKey: string
  title: string
  /** Pane typography (13px, strong text, smaller glyphs) instead of the main header's. */
  compact?: boolean
  editing?: boolean
  onEditingChange?: (editing: boolean) => void
  /** Rename / regenerate failure: `message` is the error text, `title` the i18n lead. */
  onError?: (message: string, title: string) => void
}) {
  const dispatch = useAppDispatch()
  const store = useAppStore()
  const queryClient = useQueryClient()
  const memoryMode = useAppSelector((s) => s.dashboard.slots.find((x) => x.key === slotKey)?.memory_mode)
  const [localEditing, setLocalEditing] = useState(false)
  const editing = editingProp ?? localEditing
  const setEditing = (next: boolean) => { setLocalEditing(next); onEditingChange?.(next) }
  // Leaving the slot abandons the draft (the editor below is mounted per edit,
  // so closing it drops the text). Mirrors ChatPage's `activeSlot` effect for
  // the uncontrolled case; a controlled host resets its own flag.
  useEffect(() => { setLocalEditing(false) }, [slotKey])
  // Keyed by slot, not a bare boolean: the main header re-targets this same
  // instance when the user switches sessions mid-generation, and the spinner
  // must follow the slot that is generating, not the one in front.
  const [generatingSlots, setGeneratingSlots] = useState<Set<string>>(new Set())
  const generating = generatingSlots.has(slotKey)

  const report = (e: unknown, leadKey: string) =>
    onError?.(errMessage(e) || i18nT('pages.chatPage.unknown_error'), i18nT(leadKey))

  const commit = (draft: string) => {
    const refused = draft.trim()
    if (!refused || refused === title) return
    const key = slotKey
    const slotTitle = () => store.getState().dashboard.slots.find((s) => s.key === key)?.title
    const rec = renameRecovery.get(key) ?? { baseline: title, inflight: new Set<string>(), gen: 0 }
    const current = slotTitle() ?? title
    if (!rec.inflight.has(current)) rec.baseline = current
    rec.inflight.add(refused)
    rec.gen++
    const myGen = rec.gen
    renameRecovery.set(key, rec)
    const settle = () => {
      rec.inflight.delete(refused)
      if (rec.inflight.size === 0 && rec.gen === myGen) renameRecovery.delete(key)
    }
    // A recovery applies only while THIS attempt is the slot's latest AND the
    // store still shows its refused value — anything else means a newer write
    // (a later rename here, another client, a generated title) already landed.
    const mayRecover = () => rec.gen === myGen && slotTitle() === refused
    // Optimistic: the bar shows the new title at once.
    dispatch(sseSlotTitle({ key, title: refused }))
    api.renameSlot(key, refused).then(() => {
      if (rec.gen === myGen) rec.baseline = refused
      settle()
    }, async (e) => {
      report(e, 'pages.chatPage.could_not_rename_session')
      // A refused rename must also revert the optimistic title. Re-read the
      // server truth (deduped through queryClient.fetchQuery so overlapping
      // failures share one request) and apply ONLY this slot's title — never
      // the whole snapshot, whose late fulfillment could clobber a newer
      // concurrent write of another slot. When the re-read fails too (a
      // transport or auth failure takes renameSlot and chatSlots down
      // together) fall back to a local revert to the recovery baseline.
      try {
        const server = (await queryClient.fetchQuery({
          queryKey: ['chat-slots'],
          queryFn: () => api.chatSlots(),
          staleTime: 0,
          gcTime: 0,
        })).find((s: { key: string; title?: string }) => s.key === key)
        if (server?.title !== undefined && rec.gen === myGen) rec.baseline = server.title
        if (mayRecover()) dispatch(sseSlotTitle({ key, title: server?.title ?? rec.baseline }))
      } catch {
        if (mayRecover()) dispatch(sseSlotTitle({ key, title: rec.baseline }))
      } finally {
        settle()
      }
    })
  }

  const regenerate = () => {
    if (generating) return
    const slot = slotKey
    setGeneratingSlots((prev) => new Set(prev).add(slot))
    api.generateTitle(slot).then((r) => {
      /* title is redacted server-side via redact_exfiltration_urls + redact_credentials */
      if (r.title) dispatch(sseSlotTitle({ key: slot, title: r.title }))
    }).catch((e) => {
      report(e, 'pages.chatPage.could_not_generate_title')
    }).finally(() => setGeneratingSlots((prev) => { const next = new Set(prev); next.delete(slot); return next }))
  }

  const glyphs = (
    <>
      {memoryMode === 'incognito' && <span title={i18nT('pages.chatPage.incognito_memory_writes_disabled')}><EyeOff size={13} className="shrink-0 text-warn" /></span>}
      {memoryMode === 'temporary' && <span title={i18nT('pages.chatPage.temporary_no_memory_reads_or_writes')}><VenetianMask size={13} className="shrink-0 text-aim" /></span>}
    </>
  )

  if (editing) {
    return (
      <div className="flex min-w-0 flex-1 items-center gap-1 px-1.5 py-0.5 rounded-l-[2px] rounded-r-md bg-bg-hover">
        {glyphs}
        <TitleEditor
          initial={title}
          className={compact
            ? 'text-[13px] font-semibold text-text-strong font-body bg-transparent border-0 rounded-none p-0 m-0 min-w-0 flex-1 outline-none focus:!shadow-none focus-visible:border-b focus-visible:border-accent'
            : 'session-header-title text-sm font-semibold text-muted font-body bg-transparent border-0 rounded-none p-0 m-0 min-w-0 flex-1 outline-none md:max-w-[50vw] focus:!shadow-none focus-visible:border-b focus-visible:border-accent'}
          onCommit={commit}
          onClose={() => setEditing(false)}
        />
      </div>
    )
  }

  return (
    <div className="cursor-text flex min-w-0 items-center gap-1 px-1.5 py-0.5 rounded-l-[2px] rounded-r-md group-hover/header:bg-bg-hover transition-colors">
      <Clickable className="flex min-w-0 items-center gap-1" onClick={() => { if (generating) return; setEditing(true) }}>
        {glyphs}
        <TypewriterText
          text={title}
          className={compact
            ? 'text-[13px] font-semibold text-text-strong font-body truncate min-w-0'
            : 'session-header-title text-sm font-semibold text-muted font-body truncate min-w-0 md:max-w-[50vw]'}
        />
        <Pen size={compact ? 12 : 13} className="shrink-0 text-muted opacity-0 group-hover/header:opacity-60 transition-opacity" />
      </Clickable>
      {generating
        ? <Loader size={compact ? 14 : 16} className="shrink-0 text-accent animate-spin" />
        : (
          <Btn
            aria-label={i18nT('pages.chatPage.regenerate_title_with_llm')}
            title={i18nT('pages.chatPage.regenerate_title_with_llm')}
            className="shrink-0 text-muted opacity-0 group-hover/header:opacity-40 hover:!opacity-100 hover:text-accent transition-all cursor-pointer bg-transparent border-none p-0"
            onClick={(e) => { e.stopPropagation(); regenerate() }}
          >
            <Sparkles size={compact ? 14 : 16} />
          </Btn>
        )}
    </div>
  )
}

/**
 * The inline editor, mounted per edit so mounting IS seeding: the draft starts
 * from the title the editor opened on and dies with it, so no stale draft can
 * survive a close and commit against a title that changed in the meantime.
 */
function TitleEditor({ initial, className, onCommit, onClose }: {
  initial: string
  className: string
  onCommit: (draft: string) => void
  onClose: () => void
}) {
  // Both seeded ONCE at mount: `initial` is the live title prop and moves when
  // a remote / generated rename lands while the editor is open, but the seed
  // this draft started from must not.
  const [seed] = useState(initial)
  const [draft, setDraft] = useState(initial)
  // Escape closes without committing. The flag (not just the close) is needed
  // because the browser may still fire blur on the unmounting input.
  const cancelRef = useRef(false)
  // Enter-to-commit input: the guard owns both the composition latch and the
  // keypress, so the rename cannot fire on the Enter that commits an IME candidate.
  const ime = useImeGuard()
  return (
    <Input
      className={className}
      size={Math.min(Math.max(draft.length + 2, 6), 80)}
      autoFocus
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      {...ime.bindComposition<HTMLInputElement>({
        onBlur: () => {
          // An untouched editor writes nothing: it was seeded from the title
          // at open, and the live title may have moved on since (a generated
          // or remote rename), so committing the seed would overwrite that.
          if (!cancelRef.current && draft.trim() !== seed.trim()) onCommit(draft)
          cancelRef.current = false
          onClose()
        },
      })}
      onKeyDown={(e) => {
        if (e.key === 'Enter' && ime.claimEnter(e)) (e.target as HTMLInputElement).blur()
        if (e.key === 'Escape') { ime.reset(); cancelRef.current = true; onClose() }
      }}
    />
  )
}
