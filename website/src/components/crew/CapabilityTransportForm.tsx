import { useTranslation } from 'react-i18next'
import { fmtNumber } from '../../i18n/format'
import type { CapabilityOperation } from '../../api/crewCapabilities'
import { Btn, Input } from '../ui'
import SimpleSelect from '../SimpleSelect'
import { atOrBelow, pointerPart, replaceTransportFields, secretMask, transportValue } from './capabilityTransport'

export default function CapabilityTransportForm({ id, transport, retain, disabled, onChange }: {
  id: string
  transport: Record<string, unknown>
  retain: string[]
  disabled: boolean
  onChange: (operation: CapabilityOperation) => void
}) {
  const { t } = useTranslation()
  const patch = (value: Record<string, unknown>, paths: string[]) => onChange(replaceTransportFields(id, transport, retain, value, paths))
  const replace = (value: Record<string, unknown>) => onChange({ section: 'mcpServers', id, action: 'set', value })
  const isHttp = typeof transport.url === 'string'
  const args = Array.isArray(transport.args) ? transport.args : []
  const field = (key: 'url' | 'command') => {
    const value = typeof transport[key] === 'string' ? transport[key] as string : ''
    const kept = value === secretMask && retain.includes(`/${key}`)
    return <label>{t(key === 'url' ? 'crewCapabilities.url' : 'crewCapabilities.command')}
      <Input aria-label={t(key === 'url' ? 'crewCapabilities.url' : 'crewCapabilities.command')} value={value === secretMask ? '' : value} placeholder={kept ? t('crewCapabilityEditing.keep') : undefined} onChange={e => patch({ [key]: e.target.value }, [`/${key}`])} />
      {kept && <span className="block text-[12px] text-muted">{t('crewCapabilityEditing.keep')}</span>}
    </label>
  }
  return <fieldset disabled={disabled} className="mt-2 flex min-w-0 flex-col gap-2">
    <SimpleSelect aria-label={t('crewCapabilities.transport')} options={['command', 'url']} optionLabels={[t('crewCapabilities.transportCommand'), t('crewCapabilities.transportUrl')]} value={isHttp ? 'url' : 'command'} disabled={disabled} onChange={kind => replace(kind === 'url' ? { url: '', headers: {} } : { command: '', args: [], env: {} })} />
    {field(isHttp ? 'url' : 'command')}
    {!isHttp && <div className="space-y-2">
      <p className="text-[13px]">{t('crewCapabilities.arguments')}</p>
      {args.map((value, index) => {
        const path = `/args/${index}`
        const kept = value === secretMask && retain.includes(path)
        // Removing an earlier slot changes every later JSON pointer. Do not
        // silently bind a moved mask to the secret formerly at its new index.
        const movesSecret = retain.some(p => p.startsWith('/args/') && Number(p.slice(6)) > index)
        return <div key={index} className="space-y-1">
          <Input aria-label={t('crewCapabilityEditing.argument', { index: fmtNumber(index + 1) })} value={value === secretMask ? '' : String(value)} placeholder={kept ? t('crewCapabilityEditing.keep') : undefined} onChange={e => patch({ args: args.map((arg, i) => i === index ? e.target.value : arg) }, [path])} />
          {kept && <p className="text-[12px] text-muted">{t('crewCapabilityEditing.keep')}</p>}
          <Btn disabled={disabled || movesSecret} onClick={() => patch({ args: args.filter((_, i) => i !== index) }, [path])}>{t('crewCapabilityEditing.removeArgument')}</Btn>
          {movesSecret && <p className="text-[12px] text-muted">{t('crewCapabilityEditing.position')}</p>}
        </div>
      })}
      <Btn onClick={() => patch({ args: [...args, ''] }, [])}>{t('crewCapabilityEditing.addArgument')}</Btn>
    </div>}
    {(isHttp ? ['headers'] as const : ['env'] as const).map(kind => {
      const values = transportValue(transport[kind]) ?? {}
      const label = kind === 'env' ? 'crewCapabilities.environment' : 'crewCapabilityEditing.headers'
      const nameLabel = kind === 'env' ? 'crewCapabilities.environmentName' : 'crewCapabilityEditing.headerName'
      const valueLabel = kind === 'env' ? 'crewCapabilities.environmentValue' : 'crewCapabilityEditing.headerValue'
      return <div key={kind} className="space-y-2">
        <p className="text-[13px]">{t(label)}</p>
        {Object.entries(values).map(([key, value], index) => {
          const path = `/${kind}/${pointerPart(key)}`
          const kept = value === secretMask && retain.includes(path)
          return <div key={index} className="space-y-1">
            <Input aria-label={t(nameLabel)} disabled={disabled || kept} value={key} onChange={e => {
              const nextKey = e.target.value
              if (nextKey !== key && Object.hasOwn(values, nextKey)) return
              const next = { ...values }; delete next[key]; next[nextKey] = value
              patch({ [kind]: next }, [path, `/${kind}/${pointerPart(nextKey)}`])
            }} />
            <Input aria-label={t(valueLabel)} type="password" autoComplete="off" value={value === secretMask ? '' : String(value)} placeholder={kept ? t('crewCapabilityEditing.keep') : undefined} onChange={e => patch({ [kind]: { ...values, [key]: e.target.value } }, [path])} />
            {kept && <><p className="text-[12px] text-muted">{t('crewCapabilityEditing.keep')}</p><p className="text-[12px] text-muted">{t('crewCapabilityEditing.position')}</p></>}
            <Btn onClick={() => { const next = { ...values }; delete next[key]; patch({ [kind]: next }, [path]) }}>{t(kind === 'env' ? 'crewCapabilities.removeVariable' : 'crewCapabilityEditing.removeHeader')}</Btn>
          </div>
        })}
        <Btn disabled={disabled || Object.hasOwn(values, '')} onClick={() => patch({ [kind]: { ...values, '': '' } }, [])}>{t(kind === 'env' ? 'crewCapabilities.addVariable' : 'crewCapabilityEditing.addHeader')}</Btn>
      </div>
    })}
    {/* Old servers may mask a whole container. It remains in the complete
        replacement and keeps its pointer; it cannot masquerade as an empty map. */}
    {(['args', 'env', 'headers'] as const).filter(key => transport[key] === secretMask && retain.some(path => atOrBelow(path, `/${key}`))).map(key => <p key={key} className="text-[12px] text-muted"><code>{key}</code>: {t('crewCapabilityEditing.keep')}</p>)}
  </fieldset>
}
