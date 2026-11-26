/**
 * The phone's ⋯ menu on the memory page: a bottom sheet of actions, under a
 * title naming the store they act on.
 */
import { useRef } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import { X, type LucideIcon } from 'lucide-react'
import Clickable from '../../../components/Clickable'
import { IconButton } from '../../../components/ui'
import { useDialogFocusTrap } from '../../../hooks/useDialogFocusTrap'
import { i18nT } from '../../../i18n/t'

export type SheetAction = { key: string; icon: LucideIcon; label: string; run: () => void; disabled?: boolean }

export function MemoryActionSheet({ title, groups, onClose }: { title: string; groups: SheetAction[][]; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null)
  const reduceMotion = useReducedMotion()
  useDialogFocusTrap(ref, onClose)
  return <div className="fixed inset-0 z-[90] flex items-end">
    <Clickable className="absolute inset-0 bg-black/50" onClick={onClose} aria-label={i18nT('components.modal.close')} />
    <motion.div ref={ref} role="dialog" aria-modal="true" aria-label={title} tabIndex={-1}
      initial={reduceMotion ? { opacity: 0 } : { y: '100%' }} animate={reduceMotion ? { opacity: 1 } : { y: 0 }} transition={{ duration: 0.22, ease: 'easeOut' }}
      className="relative w-full rounded-t-2xl border-t border-border bg-bg-elevated pb-6 outline-hidden p-safe" onKeyDown={event => event.stopPropagation()}>
      <div className="mx-auto mt-2 h-1 w-10 rounded-full bg-border-strong" aria-hidden="true" />
      <div className="flex h-[52px] items-center gap-1 pl-5 pr-2">
        <h3 className="m-0 min-w-0 flex-1 truncate text-[16px] font-semibold text-text">{title}</h3>
        <IconButton aria-label={i18nT('components.modal.close')} className="flex h-11 w-11 items-center justify-center" onClick={onClose}><X className="lucide-inline" aria-hidden="true" /></IconButton>
      </div>
      <div className="px-5">{groups.filter(group => group.length).map((group, index) => <div key={index} className={index ? 'mt-1 border-t border-border pt-1' : ''}>
        {group.map(item => <button key={item.key} type="button" disabled={item.disabled} onClick={() => { onClose(); item.run() }} className="flex min-h-12 w-full items-center gap-3 text-left text-[15px] text-text focus-ring disabled:opacity-40">
          <item.icon className="lucide-inline text-muted" aria-hidden="true" />{item.label}
        </button>)}
      </div>)}</div>
    </motion.div>
  </div>
}
