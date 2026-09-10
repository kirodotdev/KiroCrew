import { useState, useRef, useLayoutEffect } from 'react'
import { motion } from 'framer-motion'
import { Trans } from 'react-i18next'
import { ChevronRight, ChevronLeft, Settings2, Pin, Check, Ban, GripVertical } from 'lucide-react'
import { Input } from './ui'
import ModelDropdownList, { type ModelItem } from './ModelDropdownList'
import { ModelOrderEditor } from './ModelOrderEditor'
import ReasoningEffortDropdown from './ReasoningEffortDropdown'
import { effortLabel } from './ChatInput'

import { i18nT } from '../i18n/t'

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
  /** Configured default effort for new sessions. Shown in the footer when the
   *  slot carries no override, so the row reflects what a turn would run at. */
  defaultEffort?: string
  /** Effort levels to offer instead of this machine's — set for a session whose
   *  turns run on a peer crew. Forwarded verbatim to the slider; see
   *  `ReasoningEffortDropdown`'s `levelsOverride`. */
  effortLevelsOverride?: string[]
  onListKeyDown: (e: React.KeyboardEvent) => void
  /** Deep-link to the Settings row that sets the GLOBAL fallback model — the
   *  tier that applies to agents pinning no model of their own. Optional so
   *  call sites that have no router (or don't want the link) are unaffected —
   *  the row is simply not rendered. */
  onSetDefault?: () => void
  /** Pin the currently-active model as this agent's own default, in place. Omit
   *  to hide the row (e.g. surfaces with no agent in scope). */
  onPinToAgent?: () => void
  /** KiroCrew agent the pin row acts on; shown in its label. */
  agentName?: string
  /** Model the pin row would WRITE, named in its label. This is the slot's real
   *  model, which is not always the one the composer displays: when a pin is
   *  withheld (the account cannot run it) every display surface reads `auto`
   *  while the write still carries the pin, deliberately, so a degraded model
   *  list cannot clobber a valid pin. Naming it here is what keeps that split
   *  honest — the row states what it persists instead of letting the user assume
   *  it matches the chip. */
  pinModelName?: string
  /** True when the pinned model is withheld, so the row explains that instead of
   *  offering a write. Setting an agent default to a model the account cannot run
   *  has no upside and surfaces later as an unexplained switch, so the action is
   *  withdrawn rather than merely warned about. */
  pinModelUnavailable?: boolean
  /** True when that agent already pins the active model, so the row reports the
   *  state instead of offering a no-op write. */
  pinnedToAgent?: boolean
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
  defaultEffort = '', effortLevelsOverride, onPinToAgent, agentName = '', pinModelName = '',
  pinModelUnavailable = false, pinnedToAgent = false,
}: Props) {
  /** Which drill-in page is showing. 'order' is the model-reorder editor —
   *  maintainer review on PR #9969 moved it here from a Settings card, riding
   *  the same footer-row → spring-page pattern the effort slider established. */
  const [page, setPage] = useState<'list' | 'effort' | 'order'>('list')
  const modelPage = useRef<HTMLDivElement>(null)
  const effortPage = useRef<HTMLDivElement>(null)
  const orderPage = useRef<HTMLDivElement>(null)
  const [height, setHeight] = useState<number | undefined>(undefined)

  // Size the popover to the active page (springs on toggle / list changes).
  useLayoutEffect(() => {
    const el = page === 'effort' ? effortPage.current : page === 'order' ? orderPage.current : modelPage.current
    if (el) setHeight(el.offsetHeight)
  }, [page, models.length, filter, currentEffort, hasEffort, onSetDefault, onPinToAgent, agentName, pinModelName, pinModelUnavailable, pinnedToAgent])

  // Viewport-aware width: the design width, shrunk to fit narrow screens with
  // an 8px gutter each side (AUTOSDE narrow-viewport-required — at 320px the
  // pinned 340 overflowed by 28px). The drill-in track is percentage-based
  // (w-[300%] / w-1/3 / % translates), so every page reflows to this width.
  const width = Math.min(WIDTH, window.innerWidth - 16)
  // Right-align the dropdown to the button's right edge (clamped to viewport).
  const left = Math.max(8, Math.min(anchorRect.right - width, window.innerWidth - width - 8))

  return (
    <div
      ref={dropdownRef}
      tabIndex={-1}
      onKeyDown={page === 'list' ? onListKeyDown : undefined}
      className="fixed z-[9999] bg-bg-elevated border border-border rounded-xl shadow-xl overflow-hidden animate-slide-up"
      style={{ width, bottom: window.innerHeight - anchorRect.top + 4, left }}
    >
      <motion.div animate={{ height }} transition={SPRING} style={{ height }} className="overflow-hidden">
        <motion.div className="flex w-[300%] items-start" animate={{ x: page === 'list' ? '0%' : page === 'effort' ? '-33.3333%' : '-66.6667%' }} transition={SPRING}>
          {/* Page 1 — model list + non-scrolling effort footer */}
          <div ref={modelPage} className="w-1/3 flex flex-col p-1">
            <div className="px-1.5 pt-1.5 pb-1">
              <Input
                ref={inputRef}
                type="text"
                aria-label={i18nT('components.modelEffortDropdown.filter_models')}
                placeholder={i18nT('components.modelEffortDropdown.type_to_filter')}
                value={filter}
                onChange={e => setFilter(e.target.value)}
                className="w-full px-2 py-1 text-[13px]"
              />
            </div>
            <div role="listbox" aria-label={i18nT('components.modelEffortDropdown.model_list')} className="overflow-y-auto max-h-[280px]">
              <ModelDropdownList models={models} activeModel={activeModel} onSelect={onSelectModel} />
            </div>
            {hasEffort && slot && (
              <button
                type="button"
                onClick={() => setPage('effort')}
                className="shrink-0 mt-0.5 border-t border-border rounded-b-lg flex items-center justify-between gap-2 px-3 py-2.5 text-[13px] cursor-pointer bg-transparent border-x-0 border-b-0 hover:bg-bg-hover transition-colors"
              >
                <span className="text-muted">{i18nT('components.modelEffortDropdown.reasoning')}</span>
                <span className="flex items-center gap-1 text-text font-medium">
                  {effortLabel(currentEffort || defaultEffort)}
                  <ChevronRight size={14} className="text-muted" />
                </span>
              </button>
            )}
            <button
              type="button"
              onClick={() => setPage('order')}
              className="shrink-0 border-t border-border flex items-center justify-between gap-2 px-3 py-2 text-[12px] cursor-pointer bg-transparent border-x-0 border-b-0 text-muted hover:text-text hover:bg-bg-hover transition-colors"
            >
              <span>{i18nT('settings.chat.modelOrder.label')}</span>
              <span className="flex items-center gap-1">
                <GripVertical size={13} />
                <ChevronRight size={14} />
              </span>
            </button>
            {onPinToAgent && agentName && (
              <button
                type="button"
                onClick={pinnedToAgent || pinModelUnavailable ? undefined : onPinToAgent}
                disabled={pinnedToAgent || pinModelUnavailable}
                aria-pressed={pinnedToAgent}
                className="shrink-0 border-t border-border flex items-center justify-between gap-2 px-3 py-2 text-[12px] cursor-pointer bg-transparent border-x-0 border-b-0 text-muted hover:text-text hover:bg-bg-hover transition-colors disabled:cursor-default disabled:hover:bg-transparent"
              >
                {/* Wraps rather than truncates. The label's whole job is to name
                    WHICH agent and WHICH model the write targets, and both
                    identifiers sit at the ends — an ellipsis eats exactly the
                    part that carries the meaning. English fits on one line, but
                    the disambiguating word costs 8-14 characters in the Romance
                    locales ("modelo predeterminado", "modèle par défaut"), so
                    those overflow 340px. The popover already springs its height
                    to the measured page, so a second line is free. min-w-0 lets
                    the flex item shrink below its content; break-words is the
                    backstop for a model id longer than one line. */}
                <span className="min-w-0 text-left break-words">
                  {pinModelUnavailable
                    ? <Trans
                        i18nKey="components.modelEffortDropdown.pin_model_unavailable"
                        components={{ model: <span className="font-mono">{pinModelName}</span> }}
                      />
                    : pinnedToAgent
                    ? <Trans
                        i18nKey="components.modelEffortDropdown.default_for_agent"
                        components={{ agent: <span className="font-mono">{agentName}</span> }}
                      />
                    : <Trans
                        i18nKey="components.modelEffortDropdown.set_default_for_agent"
                        components={{
                          model: <span className="font-mono">{pinModelName}</span>,
                          agent: <span className="font-mono">{agentName}</span>,
                        }}
                      />}
                </span>
                {pinnedToAgent ? <Check size={13} className="text-accent" /> : pinModelUnavailable ? <Ban size={13} /> : <Pin size={13} />}
              </button>
            )}
            {onSetDefault && (
              <button
                type="button"
                onClick={onSetDefault}
                className="shrink-0 border-t border-border rounded-b-lg flex items-center justify-between gap-2 px-3 py-2 text-[12px] cursor-pointer bg-transparent border-x-0 border-b-0 text-muted hover:text-text hover:bg-bg-hover transition-colors"
              >
                <span>{i18nT('components.modelEffortDropdown.set_default_for_new_sessions')}</span>
                <Settings2 size={13} />
              </button>
            )}
          </div>

          {/* Page 2 — reasoning effort slider, reached via the footer */}
          <div ref={effortPage} className="w-1/3 flex flex-col p-1">
            <button
              type="button"
              onClick={() => setPage('list')}
              className="shrink-0 flex items-center gap-1 px-2 py-1.5 text-[12px] text-muted hover:text-text border-b border-border bg-transparent border-x-0 border-t-0 cursor-pointer self-stretch"
            >
              <ChevronLeft size={14} />
              {i18nT('components.modelEffortDropdown.models')}
            </button>
            {slot && (
              <ReasoningEffortDropdown slot={slot} currentEffort={currentEffort} defaultEffort={defaultEffort} onClose={onClose} embedded levelsOverride={effortLevelsOverride} />
            )}
          </div>
          {/* Page 3 — model-order editor, reached via the footer. Hosts the
              drag list; the host's list-nav keyboard handler is off on this
              page (see onKeyDown above), so dnd-kit's keyboard sensor owns
              the arrows. */}
          <div ref={orderPage} className="w-1/3 flex flex-col p-1">
            <button
              type="button"
              onClick={() => setPage('list')}
              className="shrink-0 flex items-center gap-1 px-2 py-1.5 text-[12px] text-muted hover:text-text border-b border-border bg-transparent border-x-0 border-t-0 cursor-pointer self-stretch"
            >
              <ChevronLeft size={14} />
              {i18nT('components.modelEffortDropdown.models')}
            </button>
            {/* Mounted only while showing: the editor runs its own config query
                and DndContext, neither of which needs to exist behind page 1. */}
            {page === 'order' && <ModelOrderEditor />}
          </div>
        </motion.div>
      </motion.div>
    </div>
  )
}
