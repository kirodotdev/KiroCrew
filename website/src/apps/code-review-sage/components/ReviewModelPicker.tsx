import SimpleSelect from '../../../components/SimpleSelect'
import { useAvailableModels } from '../../../hooks/useAvailableModels'
import { i18nT } from '../../../i18n/t'
import { REVIEW_MODEL_AUTO } from '../lib/types'

const SELECT_CLASS =
  'text-[12px] px-2 py-1 rounded-md bg-bg-elevated text-text border border-border '
  + 'outline-none focus:border-accent cursor-pointer'

export interface ReviewModelPickerProps {
  value: string
  onChange: (model: string) => void
  disabled?: boolean
  className?: string
}

/** A run-scoped model choice. It never reads or writes Sage's global settings. */
export default function ReviewModelPicker({
  value,
  onChange,
  disabled = false,
  className = '',
}: ReviewModelPickerProps) {
  const models = useAvailableModels()
  const advertised = models.filter(model => model.name)
  const options = advertised.map(model => model.name)
  const optionLabels = advertised.map(model => model.name === REVIEW_MODEL_AUTO
    ? i18nT('apps.codeReviewSage.views.settingsView.auto_recommended')
    : model.name)
  const selected = value || REVIEW_MODEL_AUTO
  const label = i18nT('apps.codeReviewSage.views.settingsView.review_model')

  return (
    <div className={`inline-flex min-w-0 items-center gap-1.5 ${className}`}>
      <span className="text-[11.5px] text-muted whitespace-nowrap">{label}</span>
      <SimpleSelect
        aria-label={label}
        options={options}
        optionLabels={optionLabels}
        value={selected}
        onChange={model => onChange(model || REVIEW_MODEL_AUTO)}
        disabled={disabled}
        className={SELECT_CLASS}
      />
    </div>
  )
}
