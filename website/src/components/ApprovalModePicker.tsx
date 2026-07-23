import { useState } from 'react'
import { motion } from 'framer-motion'
import { ShieldCheck, BookOpen, Handshake, Rocket, Check } from 'lucide-react'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator } from './ui/dropdown-menu'
import { useAppDispatch } from '../store'
import { changeApprovalMode } from '../store/dashboardSlice'
import { safeSetItem } from '../utils/safeStorage'

/** Single source of truth for approval-mode presentation. Previously this
 *  lived three times: APPROVAL_DISPLAY in ChatInput plus duplicated
 *  APPROVAL_SEGMENTS in ChatPage and ChatPane (which had drifted apart). */
export const APPROVAL_SEGMENTS = [
  { key: 'normal' as const, label: 'Normal', icon: <ShieldCheck size={13} />, color: '', tooltip: 'KiroCrew asks you before doing anything', desc: 'KiroCrew checks with you before doing anything' },
  { key: 'trust_reads' as const, label: 'Reads', icon: <BookOpen size={13} />, color: 'text-accent', tooltip: 'KiroCrew looks things up on its own, but asks before making changes', desc: 'KiroCrew looks things up on its own, but asks before making any changes' },
  { key: 'trust' as const, label: 'Trust', icon: <Handshake size={13} />, color: 'text-ok', tooltip: 'In this chat, KiroCrew works without asking you first', desc: 'In this chat, KiroCrew works without asking you first' },
  { key: 'yolo' as const, label: 'YOLO', icon: <Rocket size={13} />, color: 'text-danger', tooltip: 'In every chat, KiroCrew works without asking you first', desc: 'In every chat, KiroCrew works without asking you first' },
]

export type ApprovalModeKey = (typeof APPROVAL_SEGMENTS)[number]['key']

/** Approval-mode picker (Normal / Reads / Trust / YOLO) for the chat footer.
 *
 *  Self-contained Radix DropdownMenu using the standard shadcn panel and item
 *  styling: renders its own trigger pill and dispatches changeApprovalMode
 *  itself. Radix supplies positioning, outside-click + Escape dismiss,
 *  arrow-key roving and focus return — replacing the hand-rolled
 *  createPortal + viewport-clamp + outside-click copies that lived in
 *  ChatPage and ChatPane.
 *
 *  The app-wide YOLO confirm gate is preserved: switching to YOLO without a
 *  stored `mc-yolo-ack` keeps the menu open (onSelect preventDefault) and
 *  reveals a confirm section below a separator in the same panel.
 *  `mc-yolo-ack` is committed ONLY when the user confirms via Enable with
 *  the checkbox ticked — never on checkbox change, so check-then-Cancel
 *  cannot silently disable the confirm. */
export default function ApprovalModePicker({ mode, slotKey, compact }: { mode: string; slotKey: string; compact?: boolean }) {
  const dispatch = useAppDispatch()
  const [open, setOpen] = useState(false)
  const [yoloConfirm, setYoloConfirm] = useState(0)
  const [yoloDontAsk, setYoloDontAsk] = useState(false)

  const display = APPROVAL_SEGMENTS.find(s => s.key === mode) || APPROVAL_SEGMENTS[0]

  const onOpenChange = (o: boolean) => {
    setOpen(o)
    if (!o) { setYoloConfirm(0); setYoloDontAsk(false) }
  }

  const pick = (m: ApprovalModeKey) => {
    dispatch(changeApprovalMode({ mode: m, slot: slotKey }))
    onOpenChange(false)
  }

  return (
    <DropdownMenu open={open} onOpenChange={onOpenChange}>
      <DropdownMenuTrigger asChild>
        <button className="h-7 px-2 rounded-lg text-[12px] font-mono text-muted hover:text-text hover:bg-bg-hover flex items-center gap-1 cursor-pointer transition-all bg-transparent border-none shrink-0 whitespace-nowrap" title="Approval mode" aria-label={`Approval mode: ${display.label}`}>
          <span className={`shrink-0 ${display.color}`}>{display.icon}</span>
          {!compact && display.label}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align="start" collisionPadding={8} className="w-[280px]">
        {APPROVAL_SEGMENTS.map(s => (
          <DropdownMenuItem
            key={s.key}
            title={s.tooltip}
            onSelect={e => {
              if (s.key === 'yolo') {
                if (mode === 'yolo') { e.preventDefault(); return }
                if (localStorage.getItem('mc-yolo-ack')) { pick('yolo'); return }
                e.preventDefault()
                setYoloConfirm(c => c + 1)
                return
              }
              pick(s.key)
            }}
            className={s.key === mode ? 'text-accent' : ''}
          >
            <span className="shrink-0">{s.icon}</span>
            <span className="flex flex-col min-w-0 flex-1">
              <span className="font-medium">{s.label}</span>
              <span className="text-[11px] font-normal text-muted leading-snug">{s.desc}</span>
            </span>
            {s.key === mode && <Check size={12} className="shrink-0 text-accent" />}
          </DropdownMenuItem>
        ))}
        {yoloConfirm > 0 && (
          <>
            <DropdownMenuSeparator />
            <motion.div
              key={yoloConfirm}
              animate={{ x: [0, -3, 3, -2, 2, 0] }}
              transition={{ duration: 0.3 }}
              className="px-3 py-2 text-[12px]"
              onClick={e => e.stopPropagation()}
              onKeyDown={e => e.stopPropagation()}
            >
              <p className="font-medium text-text">YOLO mode is an app-wide setting</p>
              <p className="text-muted mt-0.5">All tools will get auto-approved across all sessions.</p>
              <div className="flex items-center gap-2 mt-1.5">
                <button
                  autoFocus
                  className="px-2.5 py-1 rounded-md bg-card border border-border text-danger font-medium hover:bg-bg-hover cursor-pointer"
                  onClick={() => { if (yoloDontAsk) safeSetItem('mc-yolo-ack', '1'); pick('yolo') }}
                >
                  Enable
                </button>
                <button className="px-2.5 py-1 rounded-md text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-none" onClick={() => setYoloConfirm(0)}>
                  Cancel
                </button>
                <label className="flex items-center gap-1 text-[11px] text-muted cursor-pointer ml-auto">
                  <input type="checkbox" className="rounded" checked={yoloDontAsk} onChange={e => setYoloDontAsk(e.target.checked)} />
                  Don't show again
                </label>
              </div>
            </motion.div>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
