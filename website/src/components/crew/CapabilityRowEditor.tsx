import { useTranslation } from 'react-i18next'
import type { CapabilityOperation, CapabilityRow } from '../../api/crewCapabilities'
import { Btn, Checkbox } from '../ui'
import SimpleSelect from '../SimpleSelect'
import CapabilityTransportForm from './CapabilityTransportForm'
import { capabilityLabels } from './capabilityLabels'
import { redactedPaths, retainedPaths, transportValue } from './capabilityTransport'

export default function CapabilityRowEditor({ row, operation, disabled, models, onChange }: {
  row: CapabilityRow
  operation?: CapabilityOperation
  disabled: boolean
  models: string[]
  onChange: (operation: CapabilityOperation) => void
}) {
  const { t } = useTranslation()
  const managed = row.managed === true
  const value = operation?.action === 'set' && 'value' in operation ? operation.value : row.value
  const transport = row.section === 'mcpServers' ? managed ? { ...transportValue(row.value), ...transportValue(value) } : transportValue(value) : null
  const retain = retainedPaths(row.value, operation)
  const state = operation ? operation.action === 'inherit' ? 'inherited' : operation.action === 'remove' ? 'removed' : 'local' : row.state
  const ref = { section: row.section, id: row.id }
  const set = (value: unknown) => onChange({ ...ref, action: 'set', value })
  const canPin = (row.section !== 'mcpServers' || !!transport) && (row.section !== 'model' || (typeof value === 'string' && !!value.trim()))
  const text = typeof value === 'string' ? value : ''
  const modelOptions = text && !models.includes(text) ? [text, ...models] : models
  const label = row.section === 'model' ? t('pages.kiroCrewAgentsPage.model') : row.section === 'prompt' ? t('pages.agentsPage.system_prompt') : row.label
  return (
    <div className="min-w-0 border-b border-border py-2.5" data-testid={`capability-${row.section}-${row.id}`}>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        {/* The source select beside the label is the one place the row's
            state is shown; a second badge read as a separate setting. */}
        <div className="min-w-0 flex-1 break-words text-[13px]"><span>{label}</span></div>
        <SimpleSelect aria-label={t('crewCapabilities.stateFor', { name: label })} className="w-full sm:w-[140px]"
          options={canPin ? ['inherited', 'local', 'removed'] : ['inherited', 'removed']}
          optionLabels={canPin ? [t('crewCapabilities.inherited'), t('crewCapabilities.local'), t('crewCapabilities.removed')] : [t('crewCapabilities.inherited'), t('crewCapabilities.removed')]}
          triggerFallback={t(capabilityLabels.state[state])} value={state} disabled={disabled}
          onChange={next => {
            if (next === 'inherited') onChange({ ...ref, action: 'inherit' })
            else if (next === 'removed') onChange({ ...ref, action: 'remove' })
            else if (managed) set({ disabled: transport?.disabled === true })
            else if (row.section === 'mcpServers') onChange({ ...ref, action: 'set', value, ...(retain.length ? { retain_paths: retain } : {}) })
            else set(['tools', 'allowedTools', 'autoApprove', 'skills', 'resources'].includes(row.section) ? true : value ?? '')
          }} />
      </div>
      {managed && <label className="mt-2 flex items-center gap-2 text-[13px]"><Checkbox disabled={disabled} checked={state !== 'removed' && transport?.disabled !== true} onChange={e => set({ disabled: !e.target.checked })} /><span>{t('pages.hooksPage.enabled')}</span></label>}
      {managed && <p className="mt-1 text-[12px] text-muted">{t('pages.agentsPage.comes_with_the_template_read_only_here')}</p>}
      {row.shared_reference && <p className="mt-1 text-[12px] text-muted">{t('crewCapabilities.sharedReference')}</p>}
      {operation?.action === 'set' && 'connection_id' in operation && <p className="mt-1 text-[12px] text-muted">{t('crewCapabilities.connectionSelected')}</p>}
      {transport && state !== 'removed' && !(operation?.action === 'set' && 'connection_id' in operation) && (
        <details className="mt-2 text-[13px]">
          <summary className="cursor-pointer text-accent">{t('crewCapabilities.transport')}</summary>
          {redactedPaths(transport).length > 0 && !managed && <p className="mt-2 text-muted">{t('crewCapabilityEditing.keepHint')}</p>}
          {!managed && <details className="mt-2">
            <summary>{t('crewCapabilities.replaceTransport')}</summary>
            <p className="my-2 text-[12px] text-muted">{t('crewCapabilities.masked')}</p>
            <Btn disabled={disabled} onClick={() => set({ command: '', args: [], env: {} })}>{t('crewCapabilities.replaceTransport')}</Btn>
          </details>}
          <CapabilityTransportForm id={row.id} transport={transport} retain={retain} disabled={disabled || managed} onChange={onChange} />
        </details>
      )}
      {/* The row label above already names the setting; the textarea keeps
          only its accessible name so the reader sees one control, not two. */}
      {row.section === 'prompt' && <textarea aria-label={label} className="mt-2 block w-full rounded border border-border bg-bg p-2 text-[13px]" rows={4} disabled={disabled} value={state === 'removed' ? '' : text} onChange={e => set(e.target.value)} />}
      {row.section === 'model' && <div className="mt-2">
        <SimpleSelect aria-label={label} options={modelOptions} value={state === 'removed' ? '' : text} triggerFallback={t('components.agentTemplateDetail.no_model_pinned')} disabled={disabled} onChange={set} />
      </div>}
      {row.section === 'resources' && <code className="mt-1 block break-all text-[12px]" translate="no">{row.id}</code>}
      {state === 'removed' && <Btn className="mt-2" disabled={disabled} onClick={() => onChange({ ...ref, action: 'inherit' })}>{t('crewCapabilities.restore')}</Btn>}
    </div>
  )
}
