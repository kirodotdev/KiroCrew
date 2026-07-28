import { useState, useRef, useLayoutEffect } from 'react'
import { motion } from 'framer-motion'
import { ChevronRight, ChevronLeft, Settings2 } from 'lucide-react'
import { Input } from './ui'
import ModelDropdownList from './ModelDropdownList'
import ReasoningEffortDropdown from './ReasoningEffortDropdown'
import { effortLabel } from './ChatInput'

interface ModelItem { name: string; description?: string }

interface Props {
  anchorRect: DOMRect
  dropdownRef: React.Ref<HTMLDivElement>
  inputRef: React.Ref<HTMLInputElement>
  models: ModelItem[]
  activeModel: string
  onSelectModel: (name: string) => void
  filter: string
  setFilter: (v: string) => void
  onClose: () => void
  hasEffort: boolean
  slot: string | null
  currentEffort: string
  onListKeyDown: (e: React.KeyboardEvent) => void
  /** Deep-link to the Settings row that sets the default model for NEW sessions.
   *  Optional so call sites that have no router (or don't want the link) are
   *  unaffected — the row is simply not rendered. */
  onSetDefault?: () => void
}

const WIDTH = 340
const SPRING = { type: 'spring' as const, stiffness: 420, damping: 38 }

/** Model picker with a drill-in reasoning-effort panel. The model list scrolls;
 *  a fixed (non-scrolling) footer shows the current effort + a chevron. Clicking
 *  the footer springs the effort slider in from the right (the list pushes left);
 *  a back chevron returns. The popover height springs to the active page. */
export default function ModelEffortDropdown({
  anchorRect, dropdownRef, inputRef, models, activeModel, onSelectModel,
  filter, setFilter, onClose, hasEffort, slot, currentEffort, onListKeyDown, onSetDefault,
}: Props) {
  const [showEffort, setShowEffort] = useState(false)
  const modelPage = useRef<HTMLDivElement>(null)
  const effortPage = useRef<HTMLDivElement>(null)
  const [height, setHeight] = useState<number | undefined>(undefined)

  // Size the popover to the active page (springs on toggle / list changes).
  useLayoutEffect(() => {
    const el = showEffort ? effortPage.current : modelPage.current
    if (el) setHeight(el.offsetHeight)
  }, [showEffort, models.length, filter, currentEffort, hasEffort, onSetDefault])

  // Right-align the dropdown to the button's right edge (clamped to viewport).
  const left = Math.max(8, Math.min(anchorRect.right - WIDTH, window.innerWidth - WIDTH - 8))

  return (
    <div
      ref={dropdownRef}
      tabIndex={-1}
      onKeyDown={showEffort ? undefined : onListKeyDown}
      className="fixed z-[9999] bg-bg-elevated border border-border rounded-xl shadow-xl overflow-hidden animate-slide-up"
      style={{ width: WIDTH, bottom: window.innerHeight - anchorRect.top + 4, left }}
    >
      <motion.div animate={{ height }} transition={SPRING} style={{ height }} className="overflow-hidden">
        <motion.div className="flex w-[200%] items-start" animate={{ x: showEffort ? '-50%' : '0%' }} transition={SPRING}>
          {/* Page 1 — model list + non-scrolling effort footer */}
          <div ref={modelPage} className="w-1/2 flex flex-col p-1">
            <div className="px-1.5 pt-1.5 pb-1">
              <Input
                ref={inputRef}
                type="text"
                aria-label="Filter models"
                placeholder="Type to filter…"
                value={filter}
                onChange={e => setFilter(e.target.value)}
                className="w-full px-2 py-1 text-[13px] font-mono"
              />
            </div>
            <div role="listbox" aria-label="Model list" className="overflow-y-auto max-h-[280px]">
              <ModelDropdownList models={models} activeModel={activeModel} onSelect={onSelectModel} />
            </div>
            {hasEffort && slot && (
              <button
                type="button"
                onClick={() => setShowEffort(true)}
                className="shrink-0 mt-0.5 border-t border-border rounded-b-lg flex items-center justify-between gap-2 px-3 py-2.5 text-[13px] cursor-pointer bg-transparent border-x-0 border-b-0 hover:bg-bg-hover transition-colors"
              >
                <span className="text-muted">Reasoning</span>
                <span className="flex items-center gap-1 text-text font-medium">
                  {effortLabel(currentEffort)}
                  <ChevronRight size={14} className="text-muted" />
                </span>
              </button>
            )}
            {onSetDefault && (
              <button
                type="button"
                onClick={onSetDefault}
                className="shrink-0 border-t border-border rounded-b-lg flex items-center justify-between gap-2 px-3 py-2 text-[12px] cursor-pointer bg-transparent border-x-0 border-b-0 text-muted hover:text-text hover:bg-bg-hover transition-colors"
              >
                <span>Set default for new sessions…</span>
                <Settings2 size={13} />
              </button>
            )}
          </div>

          {/* Page 2 — reasoning effort slider, reached via the footer */}
          <div ref={effortPage} className="w-1/2 flex flex-col p-1">
            <button
              type="button"
              onClick={() => setShowEffort(false)}
              className="shrink-0 flex items-center gap-1 px-2 py-1.5 text-[12px] text-muted hover:text-text border-b border-border bg-transparent border-x-0 border-t-0 cursor-pointer self-stretch"
            >
              <ChevronLeft size={14} />
              Models
            </button>
            {slot && (
              <ReasoningEffortDropdown slot={slot} currentEffort={currentEffort} onClose={onClose} embedded />
            )}
          </div>
        </motion.div>
      </motion.div>
    </div>
  )
}
