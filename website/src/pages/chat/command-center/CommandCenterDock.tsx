import { memo, useId, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { LayoutDashboard } from 'lucide-react'
import { Btn } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import { fmtNumber } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import { useLanguageGeneration } from '../../../i18n/useLanguageGeneration'
import { missingSourcesNotice, useCommandCenter } from './useCommandCenter'
import { PANEL_HEADING_ATTR } from './CommandCenterPanel'
import { safeSetItem } from '../../../utils/safeStorage'

/** Once a session's card has been clicked it opens the side panel and stays
 * gone for that session: the panel tab is the way back in. Keyed per slot so
 * another session's first dashboard still gets its one-time entrance.
 * Registered byte-identically in `utils/storageGc.ts` `SESSION_PREFIXES` so a
 * dead session's flag is collected; keep the two in step. */
const DISMISS_PREFIX = 'mc-task-dashboard-dismissed:'
function isDismissed(slot: string | null): boolean {
  if (!slot) return false
  try { return localStorage.getItem(DISMISS_PREFIX + slot) === '1' } catch { return false }
}

/** The click unmounts the card, and the focused button with it; neither
 * caller's `onOpen` moves focus, so it would fall to `<body>` and a keyboard or
 * screen-reader user would lose their place. The panel mounts in the commit the
 * caller's updates schedule (or is already mounted, hidden, and merely shown),
 * so look for its shown heading over a few frames and land there. */
function focusOpenedPanel(attempt = 0) {
  const heading = Array.from(document.querySelectorAll<HTMLElement>(`[${PANEL_HEADING_ATTR}]`)).find(el => !el.closest('[hidden]') && el.getClientRects().length > 0)
  if (heading) { heading.focus({ preventScroll: true }); return }
  if (attempt < 5) requestAnimationFrame(() => focusOpenedPanel(attempt + 1))
}

/** A one-time entrance, not a prescribed dashboard layout. The authored page
 * lives in the existing panel, which already owns dock/expand/mobile behaviour. */
function CommandCenterDock({ slot, onOpen }: { slot: string | null; onOpen: () => void }) {
  useLanguageGeneration()
  // The slot dismissed during this mount. Kept in state as well as storage so a
  // click hides the card even when the flag could not be persisted (quota full,
  // storage blocked); keyed by slot so a switch still reads that slot's own flag.
  const [dismissedHere, setDismissedHere] = useState<string | null>(null)
  const dismissed = (slot !== null && slot === dismissedHere) || isDismissed(slot)
  // A dismissed session's card renders nothing, so it must not keep reading the
  // command-center sources either: disabled, the hook issues no requests.
  const data = useCommandCenter(slot, !dismissed, 'task', { dock: true })
  const reducedMotion = useReducedMotion()
  const hintId = useId()
  const open = () => {
    if (slot) {
      safeSetItem(DISMISS_PREFIX + slot, '1')
      setDismissedHere(slot)
    }
    onOpen()
    focusOpenedPanel()
  }
  // Same content column as the sibling status bars (TaskProgressBar & co), so
  // the card lines up with the transcript and composer instead of the pane edge.
  // The column stays mounted so AnimatePresence can play the clicked card out
  // instead of cutting it; the card itself is the only thing that comes and goes.
  return <div className="px-4 mx-auto w-full relative z-[2]" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
  <AnimatePresence initial={false}>
    {data.relevant && !dismissed && <motion.div key="card" animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0, scale: 0.96 }} transition={{ duration: reducedMotion ? 0 : 0.2 }} className="mb-2 rounded-lg border border-border bg-card overflow-hidden" data-testid="command-center-dock">
      <div className="p-2">
        <Btn className="w-full justify-start border-0 min-w-0" onClick={open} aria-describedby={hintId}>
          <LayoutDashboard size={15} className="text-accent shrink-0" /><span className="truncate">{i18nT('commandCenter.title')}</span>
          {data.attention.length > 0 && <span className="ml-auto text-warn font-mono">{i18nT('commandCenter.input_count', { countText: fmtNumber(data.attention.length) })}</span>}
        </Btn>
      </div>
      {data.stale ? <div className="px-3 pb-2">
        {/* No hand-off: the adjacent chat composer and panel can hold unsent answer drafts. */}
        <ErrorNotice message={i18nT('commandCenter.stale')} />
      </div> : <>
        <p className="px-3 pb-2 text-[12px] text-muted" aria-live="polite">
          {i18nT('commandCenter.summary', { running: fmtNumber(data.running), blocked: fmtNumber(data.blocked), approvals: fmtNumber(data.approvalCount) })}
        </p>
        {/* No hand-off: the adjacent chat composer and panel can hold unsent answer drafts. */}
        {data.missing.length > 0 && <div className="px-3 pb-2"><ErrorNotice message={missingSourcesNotice(data.missing)} /></div>}
      </>}
      {/* Names the click's outcome: the card is a one-time hint, not a persistent control. */}
      <p id={hintId} className="px-3 pb-2 text-[12px] text-muted">{i18nT('commandCenter.hint_once')}</p>
    </motion.div>}
  </AnimatePresence>
  </div>
}

export default memo(CommandCenterDock)
