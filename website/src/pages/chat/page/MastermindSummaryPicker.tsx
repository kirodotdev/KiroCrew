/**
 * The Mastermind chat's second model picker, beside the composer's own model chip:
 * "Summaries: <model>". The chip next to it still picks the model RESPONSES run
 * on; this one picks the model the memory tree's SUMMARIES run on in the
 * background (`agent.role_models.mastermind`), the same setting Settings >
 * Mastermind edits, through the same hook and the same `['kirocrewConfig']`
 * cache, so the two always agree.
 *
 * Drawn only on the `mastermind` slot while Mastermind is enabled; on every
 * other slot it renders nothing and reads nothing.
 */
import { Brain } from 'lucide-react'
import ErrorNotice from '../../../components/ErrorNotice'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuLabel, DropdownMenuRadioGroup, DropdownMenuRadioItem,
  DropdownMenuSeparator, DropdownMenuTrigger,
} from '../../../components/ui/dropdown-menu'
import { MASTERMIND_SLOT_KEY, useMastermindInfo, useMastermindLocked } from '../../../hooks/useMastermind'
import { i18nT } from '../../../i18n/t'
import { useMastermindSummaryModel } from '../../settings/useMastermindSummaryModel'

export function MastermindSummaryPicker({ slotKey, compact }: { slotKey: string; compact?: boolean }) {
  return slotKey === MASTERMIND_SLOT_KEY ? <MastermindGate slotKey={slotKey} compact={compact} /> : null
}

/** Reads the Mastermind status itself (the sidebar may not be mounted, e.g. a
 *  closed mobile drawer), then draws the picker only while it is enabled. */
function MastermindGate({ slotKey, compact }: { slotKey: string; compact?: boolean }) {
  useMastermindInfo({ refreshMs: 60_000 })
  const on = useMastermindLocked(slotKey)
  return on ? <SummaryPicker compact={compact} /> : null
}

function SummaryPicker({ compact }: { compact?: boolean }) {
  const model = useMastermindSummaryModel()
  const shownLabel = model.labelOf(model.shown)
  const title = i18nT('components.chatInput.mastermind_summaries_title', { model: shownLabel })
  return (
    <span className="inline-flex items-center min-w-0">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button type="button"
            className="inline-flex items-center gap-1 h-7 min-w-0 text-[12px] text-muted hover:text-text px-2 rounded-md bg-transparent hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))] transition-colors border-none cursor-pointer disabled:cursor-not-allowed disabled:hover:bg-transparent"
            disabled={!model.ready || model.pending}
            title={title} aria-label={title}
            data-testid="composer-mastermind-summary-chip">
            <Brain size={12} className="shrink-0 text-accent" aria-hidden="true" />
            <span className="truncate max-w-[160px]">
              {compact ? shownLabel : i18nT('components.chatInput.mastermind_summaries', { model: shownLabel })}
            </span>
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="min-w-[220px] max-h-[360px] overflow-y-auto"
          data-testid="composer-mastermind-summary-menu">
          <DropdownMenuLabel className="text-[11px] text-muted font-normal whitespace-normal max-w-[260px]">
            {i18nT('pages.settings.mastermindPanel.summary_model_desc')}
          </DropdownMenuLabel>
          <DropdownMenuSeparator />
          <DropdownMenuRadioGroup value={model.shown}>
            {model.options.map((m, i) => (
              <DropdownMenuRadioItem key={m} value={m} className="text-[13px]"
                onSelect={() => { if (m !== model.shown) model.set(m) }}>
                {model.labels[i]}
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </DropdownMenuContent>
      </DropdownMenu>
      {model.error && (
        <ErrorNotice variant="inline" title={i18nT('pages.settings.mastermindPanel.summary_model_failed')}
          message={model.error} askAgent onDismiss={model.dismissError}
          testId="composer-mastermind-summary-error" className="ml-1 min-w-0" />
      )}
    </span>
  )
}
