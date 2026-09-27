import { memo } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import { ChevronDown, ChevronUp, LayoutDashboard } from 'lucide-react'
import { Btn } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import { usePersistedBool } from '../../../hooks/usePersistedBool'
import { fmtNumber } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import { useLanguageGeneration } from '../../../i18n/useLanguageGeneration'
import { useCommandCenter } from './useCommandCenter'

/** A persistent entrance, not a prescribed dashboard layout. The authored page
 * lives in the existing panel, which already owns dock/expand/mobile behaviour. */
function CommandCenterDock({ slot, onOpen }: { slot: string | null; onOpen: () => void }) {
  useLanguageGeneration()
  const data = useCommandCenter(slot)
  const [collapsed, setCollapsed] = usePersistedBool('mc-task-dashboard-collapsed', false)
  const reducedMotion = useReducedMotion()
  if (!data.relevant) return null
  return <motion.div layout transition={{ duration: reducedMotion ? 0 : 0.2 }} className="mx-4 mb-2 rounded-lg border border-border bg-card overflow-hidden relative z-[2]" data-testid="command-center-dock">
    <div className="flex items-center gap-2 p-2">
      <Btn className="flex-1 justify-start border-0 min-w-0" onClick={onOpen}>
        <LayoutDashboard size={15} className="text-accent shrink-0" /><span className="truncate">{i18nT('commandCenter.title')}</span>
        {data.attention.length > 0 && <span className="ml-auto text-warn font-mono">{i18nT('commandCenter.input_count', { countText: fmtNumber(data.attention.length) })}</span>}
      </Btn>
      <Btn aria-label={collapsed ? i18nT('commandCenter.expand') : i18nT('commandCenter.collapse')} aria-expanded={!collapsed} onClick={() => setCollapsed(v => !v)}>
        {collapsed ? <ChevronDown size={14} /> : <ChevronUp size={14} />}
      </Btn>
    </div>
    <motion.div initial={false} animate={{ height: collapsed ? 0 : 'auto', opacity: collapsed ? 0 : 1 }} transition={{ duration: reducedMotion ? 0 : 0.2 }} aria-hidden={collapsed} className="overflow-hidden">
      {data.stale ? <div className="px-3 pb-2">
        {/* No hand-off: the adjacent chat composer and panel can hold unsent answer drafts. */}
        <ErrorNotice message={i18nT('commandCenter.stale')} />
      </div> : <p className="px-3 pb-2 text-[12px] text-muted" aria-live="polite">
        {i18nT('commandCenter.summary', { running: fmtNumber(data.running), blocked: fmtNumber(data.blocked), approvals: fmtNumber(data.approvalCount) })}
      </p>}
    </motion.div>
  </motion.div>
}

export default memo(CommandCenterDock)
