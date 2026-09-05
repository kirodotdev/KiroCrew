import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Bot, Check, ChevronDown } from 'lucide-react'
import { Popover, PopoverTrigger, PopoverContent } from './ui/popover'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { useDocumentImeLatch } from '../hooks/useImeGuard'
import { useProvider } from '../providers'
import { isTouchDevice } from '../utils/isTouchDevice'
import { Input, Btn } from './ui'
import { SourceBadge } from './SourceBadge'

import { i18nT } from '../i18n/t'
export interface KiroCrewAgent {
  name: string
  kiro_agent: string
  workspace: string
  memory_store: string
  /** This agent's own default model. '' means inherit (kiro template pin, then
   *  the global fallback). Optional: older payloads predate the field. */
  model?: string
  /** This agent's own default reasoning effort. '' means inherit the global
   *  default. Optional: older payloads predate the field. */
  reasoning_effort?: string
  description: string
  /** Free-text routing intent read by the orchestrator's select_crew. Optional:
   *  older payloads predate the field, and it falls back to `description`. */
  triggers?: string
  source: string
  /** Default session color (#rrggbb hex) applied to new sessions using this
   *  agent. Empty or absent means no agent color. */
  session_color?: string
  /** Per-crew avatar override, verbatim from the backend. `{}`/absent means
   *  the face is derived from the crew name; interpreted by ghostTraitsFrom. */
  avatar?: unknown
}

interface Props {
  agents: KiroCrewAgent[]
  defaultAgent: string
  value: string
  onChange: (name: string) => void
  /**
   * Pass true when this selector renders INSIDE a Radix modal dialog (the
   * Schedule job form). The popup then mounts its own react-remove-scroll
   * instance, which takes over the wheel lock from the host dialog's —
   * without it the host lock cancels wheel events over this portal (it is
   * outside the dialog's lock container and shards) and the list cannot
   * scroll. Deliberately opt-in: on an ordinary page a modal popup would lock
   * page scroll, swallow the first click on a neighbouring field, and
   * aria-hide the rest of the page — regressions, not protections, when
   * there is no host dialog to coexist with. The transfer has a transient
   * cost in modal mode: while the popup is open, the wheel lock belongs to
   * the popup, so the dialog body itself does not wheel-scroll until the
   * popup closes.
   */
  modal?: boolean
  /**
   * Present when the roster could not be LOADED, as opposed to an install that
   * genuinely has one agent. Without it those two states render identically —
   * an empty list plus a trigger showing the default — which is the whole of
   * #5990: the reporter could not tell a failed fetch from a one-agent roster,
   * and neither could six triage passes.
   *
   * One object rather than three separate flags, so an error state with no way
   * out of it is not representable: reporting the failure and being able to
   * retry it arrive together or not at all. Optional as a whole, because a
   * surface whose roster re-fetches on its own (the chat picker refetches on
   * every slot and project change) recovers without asking the user to do
   * anything.
   *
   * `reloading` drives the disabled + pending label, so an attempt that FAILS
   * AGAIN still visibly completes: the hook sets an already-true error, React
   * bails out of the re-render, and without it the button would be
   * pixel-identical after every press during an outage.
   */
  rosterFailure?: { reloading: boolean; onReload: () => void }
}

/**
 * Reusable agent selector dropdown.
 *
 * Built on Radix Popover rather than a hand-rolled `createPortal` to
 * `document.body`, because some call sites render this inside a Radix MODAL
 * dialog (Schedule's job form). A bare body portal sits OUTSIDE the dialog's
 * layer stack there: react-remove-scroll sets `pointer-events: none` on the
 * body so clicks on the options fall through, and the dialog's FocusScope
 * never lets the filter input take focus, so the keyboard is dead too. A
 * nested Radix portal joins the dialog's focus/dismiss layer stack instead —
 * the same migration `SearchableSelect` made, and the same reason
 * `SimpleSelect` (Radix Select) works in dialogs.
 *
 * Popover has no option semantics of its own, so the listbox ARIA and roving
 * focus come from `useListboxKeyboard`, unchanged.
 */
export default function AgentSelector({ agents, defaultAgent, value, onChange, modal = false, rosterFailure }: Props) {
  const provider = useProvider()
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')
  const btnRef = useRef<HTMLButtonElement>(null)
  const dropdownRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const active = value || defaultAgent || (agents[0]?.name ?? 'default')

  const filtered = useMemo(
    () => filter
      ? agents.filter(a => a.name.toLowerCase().includes(filter.toLowerCase()))
      : agents,
    [agents, filter],
  )

  // A load failure is only worth reporting while it costs the user the list.
  // Gated on the roster being EMPTY so a failed refresh over a roster we still
  // hold, and a filter that matches nothing, both keep their own honest
  // rendering rather than being overwritten by a stale error. Narrowed to the
  // failure object rather than a boolean so the render below cannot reach for a
  // retry that is not there.
  const rosterFailed = agents.length === 0 ? rosterFailure : undefined

  // Focus return: in modal mode the popover's FocusScope is still trapping
  // when this runs, so focusing eagerly would fight the scope's own teardown
  // and strand focus on the body — onCloseAutoFocus below owns it there. In
  // non-modal mode there is no trap and the eager focus keeps the selector's
  // synchronous focus contract.
  const closeToTrigger = useCallback(() => {
    setOpen(false)
    if (!modal) btnRef.current?.focus()
  }, [modal])

  const handleSelect = useCallback((a: KiroCrewAgent) => {
    onChange(a.name)
    closeToTrigger()
  }, [onChange, closeToTrigger])

  // Reset the filter on the CLOSE TRANSITION, not in onOpenChange: Radix only
  // calls onOpenChange for closes it initiates itself (outside click), while
  // select / Escape / Tab close through this component's own setOpen(false) —
  // an onOpenChange-only reset leaks the typed filter into the next open,
  // showing a narrowed (or empty "No matches") list the user did not filter.
  useEffect(() => { if (!open) setFilter('') }, [open])

  // Tracked IME latch for the two NATIVE Escape consumers below. The native
  // flags alone cannot identify a composition-cancel Escape on WebKit — the
  // keydown that cancels a candidate arrives AFTER compositionend with
  // isComposing already false — so a raw flag check would dismiss the popup
  // on a keypress that only cancelled a composition. `claimKey` consults the
  // document-tracked latch (live from compositionstart until 50ms past
  // compositionend, with stranded-latch recovery) and owns the decline:
  // stopPropagation always, preventDefault only in the post-composition
  // window where the browser would otherwise act.
  const imeLatch = useDocumentImeLatch(open)

  // Escape must dismiss ONLY this popup, never the host surface. Radix layers
  // hand the document-level Escape listener from the host dialog to this
  // popover asynchronously (an update event, a re-render, then an effect), so
  // there is a window — observed repeatedly in a real browser — where the
  // DIALOG still holds the listener and a single Escape closes both. Claiming
  // the key at window capture, which runs before every document-level
  // listener, and marking it defaultPrevented is the one deterministic order:
  // every DismissableLayer skips its own onDismiss on a defaulted event, and
  // the popup closes through closeToTrigger alone.
  //
  // Scoped, because Escape is a shared key: only an Escape originating inside
  // this popup or on its trigger is claimed — one landing anywhere else (an
  // overlay stacked above that did not steal focus, the host form on touch
  // where the popup never took focus) is left for its own surface, since
  // consumers like Modal gate their dismissal on !defaultPrevented. A
  // composition-cancel Escape is declined through the latch — the IME keeps
  // its native action and no dismissal layer sees the key, so the popup
  // stays open across the whole composition window, including WebKit's
  // post-compositionend keydown that the raw flags cannot identify.
  // Non-composing claimed keys are preventDefault'ed WITHOUT stopPropagation:
  // handlers that must see every Escape regardless of who consumed it (voice
  // read-back stop) still do.
  useEffect(() => {
    if (!open) return
    const onEsc = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      const t = e.target
      const inPopup = t instanceof Node
        && (dropdownRef.current?.contains(t) || btnRef.current?.contains(t))
      if (!inPopup) return
      if (!imeLatch.claimKey(e)) return
      e.preventDefault()
      closeToTrigger()
    }
    window.addEventListener('keydown', onEsc, { capture: true })
    return () => window.removeEventListener('keydown', onEsc, { capture: true })
  }, [open, closeToTrigger, imeLatch])

  const { onListKeyDown } = useListboxKeyboard({
    open,
    dropdownRef,
    inputRef,
    // Radix autofocuses the first focusable node in the content. That is the
    // filter box normally, and the Retry button when the roster failed to load
    // and the filter is withheld — either way something else owns focus, so the
    // hook must not also grab it for the list.
    hasFilterInput: true,
    filteredCount: filtered.length,
    onEnterSingleMatch: () => {
      const a = filtered[0]
      if (a) handleSelect(a)
    },
    closeToTrigger,
  })

  return (
    <Popover open={open} onOpenChange={setOpen} modal={modal}>
      <PopoverTrigger
        ref={btnRef}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-mono font-medium border border-border bg-bg-elevated text-text hover:border-border-strong transition-all cursor-pointer"
        aria-label={i18nT('components.agentSelector.switch_agent')}
      >
        <span className="text-accent"><Bot size={14} /></span> {active}
        <ChevronDown size={12} className="text-muted ml-1 shrink-0" aria-hidden />
      </PopoverTrigger>
      <PopoverContent
        ref={dropdownRef}
        align="start"
        // Focusable programmatically: the touch open path below parks focus on
        // this container, and without an explicit tabIndex the focus() call is
        // a silent no-op that strands focus on the (aria-hidden, in modal
        // mode) trigger.
        tabIndex={-1}
        // Radix gives this surface role="dialog" (the trigger's
        // aria-haspopup matches); it needs a name or AT announces an
        // unnamed dialog. Named after the list — NOT the trigger's label,
        // which must stay unique to the trigger.
        aria-label={i18nT('components.agentSelector.agent_list')}
        onKeyDown={onListKeyDown}
        // On touch, keep the on-screen keyboard down (it costs half the
        // viewport before the user has asked to filter) but move focus to
        // the content container itself: leaving it on the trigger strands a
        // screen-reader user outside the popup — and, in modal mode, on an
        // element hideOthers() has just aria-hidden.
        onOpenAutoFocus={e => {
          if (!isTouchDevice()) return
          e.preventDefault()
          dropdownRef.current?.focus()
        }}
        // Modal only: the focus trap is still live when closeToTrigger runs,
        // so the deterministic focus return must happen at FocusScope
        // teardown. Non-modal keeps Radix's own semantics — its
        // hasInteractedOutside guard deliberately does NOT reclaim focus
        // after an outside click, so the field the user clicked keeps the
        // caret (pre-migration behaviour); overriding it in non-modal mode
        // would yank focus back one macrotask after the click landed.
        onCloseAutoFocus={modal ? e => {
          e.preventDefault()
          btnRef.current?.focus()
        } : undefined}
        // Backstop for the window-capture Escape handler above: if a Radix
        // layer still delivers Escape here (e.g. the key originated outside
        // the popup, where the window handler deliberately does not claim
        // it), keep the dismissal ours alone. Routing through the same
        // tracked latch is defensive parity with the window handler — for a
        // popup-origin key that handler already consumed the event before
        // any layer saw it.
        onEscapeKeyDown={e => {
          if (!imeLatch.claimKey(e)) return
          e.preventDefault()
          closeToTrigger()
        }}
        // An 8px viewport gutter (the hand-rolled positioner guaranteed the
        // left one) and a width cap so the popup never overhangs at 320px.
        collisionPadding={8}
        className="w-auto min-w-[240px] max-w-[min(340px,calc(100vw-16px))] max-h-[min(280px,var(--radix-popover-content-available-height))] p-0 flex flex-col overflow-hidden bg-card"
      >
        {/* Withheld when the roster failed: a filter box directly above
            "couldn't load the agent list" invites narrowing a list that does
            not exist. There is nothing to filter, so the control is absent
            rather than merely inert. */}
        {!rosterFailed && (
          <div className="p-2 border-b border-border">
            <Input
              ref={inputRef}
              type="text"
              aria-label={i18nT('components.agentSelector.filter_agents')}
              placeholder={i18nT('components.agentSelector.type_to_filter')}
              value={filter}
              onChange={e => setFilter(e.target.value)}
              className="w-full px-2 py-1 text-[13px]"
            />
          </div>
        )}
        <div role="listbox" aria-label={i18nT('components.agentSelector.agent_list')} className="flex-1 min-h-0 overflow-y-auto divide-y divide-border">
          {filtered.map(a => {
            const isCurrent = active === a.name
            const isDefault = a.name === defaultAgent
            return (
              <Btn
                key={a.name}
                role="option"
                aria-selected={isCurrent}
                tabIndex={-1}
                className={`w-full text-left px-3 py-2 flex items-center gap-2 min-w-0 border-0 rounded-none cursor-pointer
                  ${isCurrent ? 'bg-accent-subtle hover:bg-accent-subtle' : 'hover:bg-bg-hover'}
                `}
                onClick={() => handleSelect(a)}
              >
                <div className="flex flex-col min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className={`text-[13px] font-mono font-semibold truncate ${isCurrent ? 'text-accent' : 'text-text'}`}>{a.name}</span>
                    {isDefault && <span className="px-1.5 py-[1px] rounded-full text-[10px] font-bold bg-accent-subtle text-accent border border-accent/30 shrink-0">{i18nT('components.agentSelector.default')}</span>}
                    {a.source && (
                      <SourceBadge source={a.source} className="shrink-0">
                        {a.source}
                      </SourceBadge>
                    )}
                  </div>
                  <span className="text-[11px] text-muted truncate">{a.description || provider.resolveAgentTemplate(a)}</span>
                </div>
                {isCurrent && <span className="text-accent text-[11px] ml-auto shrink-0"><Check className="lucide-inline" /></span>}
              </Btn>
            )
          })}
          {filtered.length === 0 && !rosterFailed && <div className="px-3 py-2 text-[13px] text-muted italic">{i18nT('components.agentSelector.no_matches')}</div>}
        </div>
        {/* Outside the listbox, not another childless row inside it: the retry is
            a button, and a button is not an `option` — putting it among them
            would make the list announce a control it cannot select. */}
        {rosterFailed && (
          <div className="px-3 py-2 border-t border-border flex items-center justify-between gap-2">
            <span className="text-[12px] text-danger">{i18nT('components.agentSelector.roster_load_failed')}</span>
            <Btn
              onClick={rosterFailed.onReload}
              disabled={rosterFailed.reloading}
              aria-busy={rosterFailed.reloading}
              className="text-[12px] px-2 py-1 shrink-0"
            >
              {rosterFailed.reloading
                ? i18nT('components.agentSelector.retrying')
                : i18nT('components.agentSelector.retry')}
            </Btn>
          </div>
        )}
      </PopoverContent>
    </Popover>
  )
}
