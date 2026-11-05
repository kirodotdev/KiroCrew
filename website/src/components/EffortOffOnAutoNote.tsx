import { i18nT } from '../i18n/t'

/**
 * Stands where the effort slider would be when the session is on `auto` (#11546).
 *
 * On `auto` the backend picks the model and takes no effort level until a
 * specific model is selected, so the picker offers no slider. Without this line
 * the configured Default Reasoning Effort reads as if it were in force.
 */
export default function EffortOffOnAutoNote() {
  return (
    <div
      className="mt-2 shrink-0 border-t border-border px-3 py-2 text-[12px] leading-relaxed text-muted"
      role="note"
      data-testid="effort-off-on-auto"
    >
      {i18nT('components.reasoningEffortDropdown.effort_off_on_auto')}
    </div>
  )
}
