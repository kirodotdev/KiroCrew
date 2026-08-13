import { useState } from 'react'
import { motion } from 'framer-motion'
import { useNavigate } from 'react-router-dom'
import { ShieldCheck, BookOpen, Handshake, Rocket, Check, Clock } from 'lucide-react'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator } from './ui/dropdown-menu'
import { useAppDispatch, useAppSelector } from '../store'
import { changeApprovalMode } from '../store/dashboardSlice'
import { safeSetItem } from '../utils/safeStorage'

import { i18nT } from '../i18n/t'
/** Single source of truth for approval-mode presentation.
 *
 *  Only the language-INDEPENDENT metadata (key, icon, colour) lives at module
 *  scope. Human-readable strings are resolved through `i18nT` at RENDER time via
 *  `segmentText()` — a module-level constant would freeze them in whichever
 *  language was active at import and never re-translate on a language switch. */
export const APPROVAL_SEGMENTS = [
  { key: 'normal' as const, icon: <ShieldCheck size={13} />, color: '' },
  { key: 'trust_reads' as const, icon: <BookOpen size={13} />, color: 'text-accent' },
  { key: 'trust' as const, icon: <Handshake size={13} />, color: 'text-ok' },
  { key: 'yolo' as const, icon: <Rocket size={13} />, color: 'text-danger' },
]

export type ApprovalModeKey = (typeof APPROVAL_SEGMENTS)[number]['key']

/** Localized name for a configured ad-hoc duration, so no raw config token
 *  reaches user-facing copy. Static literal keys keep the dead-key and
 *  dynamic-key i18n guards happy. */
function durationLabel(token: string | undefined): string {
  switch (token) {
    case '30m': return i18nT('pages.settings.securityPanel.yolo_duration_30m')
    case '1h': return i18nT('pages.settings.securityPanel.yolo_duration_1h')
    case '12h': return i18nT('pages.settings.securityPanel.yolo_duration_12h')
    case '24h': return i18nT('pages.settings.securityPanel.yolo_duration_24h')
    default: return i18nT('pages.settings.securityPanel.yolo_duration_6h')
  }
}

/** Localized label / tooltip / description for a segment, read at call time.
 *  Static literal keys (no runtime assembly) keep the dead-key and dynamic-key
 *  i18n guards happy. */
function segmentText(key: ApprovalModeKey): { label: string; tooltip: string; desc: string } {
  switch (key) {
    case 'trust_reads':
      return {
        label: i18nT('components.approvalModePicker.reads_label'),
        tooltip: i18nT('components.approvalModePicker.reads_tooltip'),
        desc: i18nT('components.approvalModePicker.reads_desc'),
      }
    case 'trust':
      return {
        label: i18nT('components.approvalModePicker.trust_label'),
        tooltip: i18nT('components.approvalModePicker.trust_tooltip'),
        desc: i18nT('components.approvalModePicker.trust_desc'),
      }
    case 'yolo':
      return {
        label: i18nT('components.approvalModePicker.yolo_label'),
        tooltip: i18nT('components.approvalModePicker.yolo_tooltip'),
        desc: i18nT('components.approvalModePicker.yolo_desc'),
      }
    case 'normal':
    default:
      return {
        label: i18nT('components.approvalModePicker.normal_label'),
        tooltip: i18nT('components.approvalModePicker.normal_tooltip'),
        desc: i18nT('components.approvalModePicker.normal_desc'),
      }
  }
}

/** Approval-mode picker (Normal / Reads / Trust / YOLO) for the chat footer.
 *
 *  Self-contained Radix DropdownMenu using the standard shadcn panel and item
 *  styling: renders its own trigger pill and dispatches changeApprovalMode
 *  itself. Radix supplies positioning, outside-click + Escape dismiss,
 *  arrow-key roving and focus return.
 *
 *  The app-wide YOLO confirm gate is preserved: switching to YOLO without a
 *  stored `mc-yolo-ack` keeps the menu open (onSelect preventDefault) and
 *  reveals a confirm section below a separator in the same panel.
 *  `mc-yolo-ack` is committed ONLY when the user confirms via Enable with
 *  the checkbox ticked — never on checkbox change, so check-then-Cancel
 *  cannot silently disable the confirm. */
export default function ApprovalModePicker({ mode, slotKey, compact }: { mode: string; slotKey: string; compact?: boolean }) {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const yoloDuration = useAppSelector(s => s.dashboard.status?.yolo_duration)
  const [open, setOpen] = useState(false)
  const [yoloConfirm, setYoloConfirm] = useState(0)
  const [yoloDontAsk, setYoloDontAsk] = useState(false)

  const display = APPROVAL_SEGMENTS.find(s => s.key === mode) || APPROVAL_SEGMENTS[0]
  const displayText = segmentText(display.key)

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
        {/* Chrome type ("Normal" / "Reads" / "Trust" / "YOLO" are labels), so no
            `font-mono` — that pinned `var(--mono)`, which the Font Family
            setting never writes. */}
        <button className="h-7 px-2 rounded-lg text-[12px] text-muted hover:text-text hover:bg-bg-hover flex items-center gap-1 cursor-pointer transition-all bg-transparent border-none shrink-0 whitespace-nowrap outline-none focus-visible:outline-2 focus-visible:outline-accent/50 focus-visible:-outline-offset-2" title={i18nT('components.approvalModePicker.approval_mode')} aria-label={i18nT('components.approvalModePicker.approval_mode_aria', { mode: displayText.label })}>
          <span className={`shrink-0 ${display.color}`}>{display.icon}</span>
          {!compact && displayText.label}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align="start" collisionPadding={8} className="w-[280px]">
        {APPROVAL_SEGMENTS.map(s => {
          const t = segmentText(s.key)
          return (
          <DropdownMenuItem
            key={s.key}
            title={t.tooltip}
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
              <span className="font-medium">{t.label}</span>
              <span className="text-[11px] font-normal text-muted leading-snug">{t.desc}</span>
            </span>
            {s.key === mode && <Check size={12} className="shrink-0 text-accent" />}
          </DropdownMenuItem>
          )
        })}
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
              <p className="font-medium text-text">{i18nT('components.approvalModePicker.yolo_mode_is_an_app_wide_setting')}</p>
              <p className="text-muted mt-0.5">{i18nT('components.approvalModePicker.all_tools_will_get_auto_approved_across_all_sess')}</p>
              <p className="text-muted mt-1 flex items-center gap-1">
                <Clock size={11} className="shrink-0" />
                {yoloDuration === 'until_shutdown'
                  ? i18nT('components.approvalModePicker.yolo_expiration_until_shutdown')
                  : i18nT('components.approvalModePicker.yolo_expiration_timed', {
                      duration: durationLabel(yoloDuration),
                    })}
              </p>
              <div className="flex items-center gap-2 mt-1.5">
                <button
                  autoFocus
                  className="px-2.5 py-1 rounded-md bg-card border border-border text-danger font-medium hover:bg-bg-hover cursor-pointer"
                  onClick={() => { if (yoloDontAsk) safeSetItem('mc-yolo-ack', '1'); pick('yolo') }}
                >
                  {i18nT('components.approvalModePicker.enable')}
                </button>
                <button className="px-2.5 py-1 rounded-md text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-none" onClick={() => setYoloConfirm(0)}>
                  {i18nT('components.approvalModePicker.cancel')}
                </button>
                <label className="flex items-center gap-1 text-[11px] text-muted cursor-pointer ml-auto">
                  <input type="checkbox" className="rounded" checked={yoloDontAsk} onChange={e => setYoloDontAsk(e.target.checked)} />
                  {i18nT('components.approvalModePicker.don_t_show_again')}
                </label>
              </div>
              <button
                className="mt-1.5 text-[11px] text-muted hover:text-text underline bg-transparent border-none cursor-pointer p-0"
                onClick={() => { onOpenChange(false); navigate('/settings?tab=security&section=approval') }}
              >
                {i18nT('components.approvalModePicker.configure_duration')}
              </button>
            </motion.div>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
