import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, Check, Cpu } from 'lucide-react'

import { backendRow, useBackends } from '../hooks/useBackends'
import { i18nT } from '../i18n/t'

interface BackendSelectorProps {
  /** The chat's stored backend. `''` means "inherit the configured default",
   *  and the default row is what renders as chosen for it. */
  value: string
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

  // The trigger names what the chat will actually run on. With no row resolved it
  // says "default" rather than inventing a name: an empty selection genuinely is
  // "whatever the gateway resolves", and that is also the honest label while the
  // listing is still unknown.
  const triggerLabel = current
    ? (current.label || current.id)
    : i18nT('components.backendSelector.default_backend')

  return (
    <div ref={wrapRef} className="relative flex flex-col items-center gap-1">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={i18nT('components.backendSelector.backend_name', { name: triggerLabel })}
        className="inline-flex items-center gap-1.5 h-7 px-2.5 rounded-md text-[12px] border border-border bg-transparent hover:bg-bg-hover transition-colors cursor-pointer disabled:cursor-not-allowed disabled:opacity-50 text-muted hover:text-text"
      >
        <Cpu size={13} className="shrink-0 opacity-70" />
        <span className="truncate min-w-0 max-w-[180px]">{triggerLabel}</span>
      </button>
      {open && (
        <div
          role="listbox"
          aria-label={i18nT('components.backendSelector.backend_list')}
          className="absolute top-full mt-1 z-[9999] w-[300px] max-h-[320px] overflow-y-auto bg-bg-elevated border border-border rounded-xl shadow-xl p-1 flex flex-col gap-0.5"
        >
          {state.isError && (
            <div className="px-2 py-2 text-[12px] text-warn">
              {i18nT('components.backendSelector.backend_list_unavailable')}
            </div>
          )}
          {!state.isError && state.backends.length === 0 && (
            <div className="px-2 py-2 text-[12px] text-muted">
              {i18nT('components.backendSelector.no_backends_registered')}
            </div>
          )}
          {state.backends.map(b => {
            // An empty stored value inherits the default row; match on id (not
            // truthiness) so kiro-cli's `''` id is selectable like any other.
            const selected = (value || state.defaultId) === b.id
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
              not chosen. */}
          {state.unroutable.map(b => (
            <div
              key={`unroutable-${b.id}`}
              className="w-full px-2 py-1.5 rounded-lg text-[13px] text-muted flex flex-col gap-0.5"
            >
              <span className="truncate flex items-center gap-1.5">
                <AlertTriangle size={12} className="shrink-0 text-warn" />
                {b.label || b.id}
              </span>
              <span className="text-[11px] text-warn leading-snug">
                {i18nT('components.backendSelector.unroutable_reason', { reason: b.reason })}
              </span>
            </div>
          ))}
          {/* Invalid: failed validation, not spellable at all. */}
          {state.invalid.map(b => (
            <div
              key={`invalid-${b.id}`}
              className="w-full px-2 py-1.5 rounded-lg text-[13px] text-muted flex flex-col gap-0.5"
            >
              <span className="truncate">{b.label || b.id}</span>
              <span className="text-[11px] text-danger leading-snug">
                {i18nT('components.backendSelector.invalid_reason', { reason: b.reasons.join('; ') })}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
