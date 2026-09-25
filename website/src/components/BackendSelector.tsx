import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, Check, ChevronDown, Cpu } from 'lucide-react'

import { backendRow, useBackends } from '../hooks/useBackends'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import ErrorNotice from './ErrorNotice'
import { SettingsLink } from './SettingsLink'
import { i18nT } from '../i18n/t'

interface BackendSelectorProps {
  /** The chat's stored backend. `null` means "inherit the configured default",
   *  and the default row is what renders as chosen for it; `''` is kiro-cli's
   *  own id, a real pin. */
  value: string | null
  /** Called with the picked backend id. Never called for an invalid or
   *  unroutable row: those rows are rendered to be READ, not chosen. */
  onSelect: (id: string) => void
  /** Blocks picking while a previous pick is still being applied. The trigger
   *  stays focusable and keeps its label so the current choice is still
   *  readable. */
  disabled?: boolean
}

/**
 * Backend picker for the new-chat surface (D8 vocabulary: "backend").
 *
 * Unlike the salvaged harness picker, the descriptor-harness `/api/backends`
 * gates selectability SERVER-SIDE: every row in `backends` is one the gateway
 * will start, so this component does not re-derive an `available`/`serviceable`
 * verdict per row. The two diagnostic lists it still renders — never selectable —
 * are the operator's own entries that could not become a backend:
 *
 * **Invalid descriptors** failed validation and are not spellable at all; a
 * session could never run on one, so they are listed apart from the pickable
 * rows and carry their reasons.
 *
 * **Unroutable descriptors** parsed cleanly but declared no verified routing, so
 * the gateway refused them selectability. They are shown so an operator sees WHY
 * an entry they wrote is not offered, rather than watching it silently vanish.
 *
 * **A failed listing offers nothing.** `useBackends` empties the rows on
 * `isError` rather than retaining the last answer: selectability is a statement
 * about the build/governance ceiling right now, and a retained list could offer a
 * pick the gateway now refuses. The picker says it does not know instead of
 * guessing.
 */
export default function BackendSelector({ value, onSelect, disabled }: BackendSelectorProps) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  const btnRef = useRef<HTMLButtonElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  // No filter input in this picker; the hook still needs the ref slot.
  const inputRef = useRef<HTMLElement | null>(null)
  const state = useBackends()
  const current = backendRow(state, value)

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (wrapRef.current?.contains(e.target as Node)) return
      setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  // Keyboard: the same WAI-ARIA listbox pattern AgentSelector and
  // SearchableSelect use. With no filter input the hook moves focus into the
  // list on open (to the selected row), Arrow/Home/End rove across the
  // `role="option"` buttons, Enter activates the focused row natively, and
  // Escape/Tab close and hand focus back to the trigger.
  const closeToTrigger = useCallback(() => {
    setOpen(false)
    btnRef.current?.focus()
  }, [])
  const { onListKeyDown } = useListboxKeyboard({
    open,
    dropdownRef: listRef,
    inputRef,
    hasFilterInput: false,
    filteredCount: state.backends.length,
    onEnterSingleMatch: () => {},
    closeToTrigger,
  })

  // The trigger names what the chat will actually run on. With no row resolved it
  // says "default" rather than inventing a name: an empty selection genuinely is
  // "whatever the gateway resolves", and that is also the honest label while the
  // listing is still unknown.
  const triggerLabel = current
    ? (current.label || current.id)
    : i18nT('components.backendSelector.default_backend')
  const triggerText = i18nT('components.backendSelector.backend_name', { name: triggerLabel })

  return (
    // The wrapper spans its pane (`w-full`) so the dropdown below has the pane's
    // width to position in: an `absolute` list anchored to a centered,
    // content-width trigger extends from the trigger's LEFT edge, and at 320px
    // or in a narrow split pane a fixed 300px list ran off-screen. The trigger
    // stays content-width and centered; the list is `w-full max-w-[300px]`
    // centered under it (`left-1/2 -translate-x-1/2`), so it is never wider than
    // the pane and never wider than 300px.
    <div ref={wrapRef} className="relative flex flex-col items-center gap-1 w-full min-w-0">
      <button
        ref={btnRef}
        type="button"
        onClick={() => setOpen(o => !o)}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={triggerText}
        className="inline-flex items-center gap-1.5 h-7 px-2.5 rounded-md text-[12px] border border-border bg-transparent hover:bg-bg-hover transition-colors cursor-pointer disabled:cursor-not-allowed disabled:opacity-50 text-muted hover:text-text max-w-full"
      >
        <Cpu size={13} className="shrink-0 opacity-70" />
        {/* Visible "Backend: <name>" so a first-time user can tell what the chip
            selects without opening it — the sibling memory-mode trigger names its
            purpose in its text too. One interpolated string, not a prefix plus a
            name, so a translator can reorder it. `title` carries the full string
            when a long label or a wide locale truncates it. */}
        <span className="truncate min-w-0 max-w-[220px]" title={triggerText}>
          {triggerText}
        </span>
        <ChevronDown size={12} className="shrink-0 opacity-70" aria-hidden />
      </button>
      {open && (
        <div
          ref={listRef}
          role="listbox"
          // Focusable programmatically (jsx-a11y: a listbox with a key handler
          // must be able to hold focus); the hook moves focus to an option on
          // open, so this never becomes a tab stop.
          tabIndex={-1}
          aria-label={i18nT('components.backendSelector.backend_list')}
          onKeyDown={onListKeyDown}
          className="absolute top-full left-1/2 -translate-x-1/2 mt-1 z-[9999] w-full max-w-[300px] max-h-[320px] overflow-y-auto bg-bg-elevated border border-border rounded-xl shadow-xl p-1 flex flex-col gap-0.5"
        >
          {state.isError && (
            <div className="px-2 py-2">
              <ErrorNotice
                variant="inline"
                askAgent
                testId="backend-selector-list-error"
                message={i18nT('components.backendSelector.backend_list_unavailable')}
              />
            </div>
          )}
          {/* Only after the listing answered: "none registered" is a claim, and
              the hook returns empty arrays while the first fetch is in flight,
              so stating it then would assert it of every install for a frame. */}
          {!state.isError && !state.isLoading && state.backends.length === 0 && (
            <div className="px-2 py-2 text-[12px] text-muted">
              {i18nT('components.backendSelector.no_backends_registered')}
            </div>
          )}
          {state.backends.map(b => {
            // A null stored value inherits the default row; match on id (not
            // truthiness) so kiro-cli's `''` id is selectable like any other.
            const selected = (value ?? state.defaultId) === b.id
            return (
              <button
                key={b.id || '__kiro__'}
                type="button"
                role="option"
                aria-selected={selected}
                tabIndex={-1}
                onClick={() => { onSelect(b.id); setOpen(false) }}
                className="w-full text-left px-2 py-1.5 rounded-lg text-[13px] flex items-start gap-1.5 bg-transparent border-none transition-colors text-text hover:bg-bg-hover cursor-pointer"
              >
                <span className="w-3.5 shrink-0 pt-0.5">{selected && <Check size={13} />}</span>
                <span className="flex flex-col min-w-0">
                  <span className="truncate">{b.label || b.id}</span>
                  {b.is_global_default && (
                    <span className="text-[11px] text-muted/70 leading-snug">
                      {i18nT('components.backendSelector.default')}
                    </span>
                  )}
                </span>
              </button>
            )
          })}
          {/* Unroutable: spellable, but the gateway refused selectability. Read,
              not chosen. The reason is VISIBLE (a hover title is unreachable by
              keyboard or touch) and "see Settings → AI Backends" is a real link
              to the panel that carries the Verify action. */}
          {state.unroutable.map(b => (
            <div
              key={`unroutable-${b.id}`}
              className="w-full px-2 py-1.5 rounded-lg text-[13px] text-muted flex flex-col gap-0.5"
              data-testid={`backend-selector-unroutable-${b.id}`}
            >
              <span className="truncate flex items-center gap-1.5">
                <AlertTriangle size={12} className="shrink-0 text-warn" />
                {b.label || b.id}
              </span>
              <span className="text-[11px] text-warn leading-snug">
                <SettingsLink tab="backends" className="text-warn underline underline-offset-2 hover:text-text">
                  {i18nT('components.backendSelector.unroutable_see_settings')}
                </SettingsLink>
              </span>
              <span className="text-[11px] text-muted leading-snug whitespace-normal">{b.reason}</span>
            </div>
          ))}
          {/* Invalid: failed validation, not spellable at all. Same split; every
              reason is shown (they are field/position statements, never
              operator text) through the standard error surface with the agent
              hand-off, as the Settings panel shows them. */}
          {state.invalid.map(b => (
            <div
              key={`invalid-${b.id}`}
              className="w-full px-2 py-1.5 rounded-lg text-[13px] text-muted flex flex-col gap-0.5"
              data-testid={`backend-selector-invalid-${b.id}`}
            >
              <span className="truncate">{b.label || b.id}</span>
              <span className="text-[11px] text-danger leading-snug">
                <SettingsLink tab="backends" className="text-danger underline underline-offset-2 hover:text-text">
                  {i18nT('components.backendSelector.invalid_see_settings')}
                </SettingsLink>
              </span>
              {b.reasons.map((reason, i) => (
                <ErrorNotice
                  key={i}
                  variant="inline"
                  message={reason}
                  messageClassName="font-mono"
                  askAgent
                  testId={`backend-selector-invalid-${b.id}-reason-${i}`}
                />
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
