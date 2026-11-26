/**
 * The date range button on the memory page's toolbars: Today, Last 7 days, Last
 * 30 days, or a custom from–to pair picked with the platform's own date fields.
 * Any time clears it. The picked range shows as a removable chip beside the filters.
 */
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { CalendarDays, Check, ChevronDown } from 'lucide-react'
import { Btn, Input } from '../../../components/ui'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '../../../components/ui/dropdown-menu'
import Modal from '../../../components/Modal'
import { fmtDate } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import { RANGE_PRESETS, dayDate, dayKey, rangeBounds, type DateRange } from './memoryDateRange'

const PRESET_KEYS = { today: 'memoryPage.range_today', '7d': 'memoryPage.range_7d', '30d': 'memoryPage.range_30d' } as const

/** How a range reads on its chip and its button. */
export function rangeLabel(range: DateRange): string {
  if (range.preset !== 'custom') return i18nT(PRESET_KEYS[range.preset])
  const from = range.from ? dayDate(range.from) : null
  const to = range.to ? dayDate(range.to) : null
  if (from && to) return i18nT('memoryPage.range_between', { from: fmtDate(from), to: fmtDate(to) })
  if (from) return i18nT('memoryPage.range_since', { date: fmtDate(from) })
  return i18nT('memoryPage.range_until', { date: fmtDate(to!) })
}

export function DateRangeButton({ range, onChange }: { range?: DateRange; onChange: (range?: DateRange) => void }) {
  const { t } = useTranslation()
  const [custom, setCustom] = useState(false)
  return <>
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button type="button" aria-label={t('memoryPage.date_range')} className={`flex min-h-11 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border px-3 text-[13px] focus-ring sm:min-h-10 ${range ? 'border-accent text-text' : 'border-border text-text'}`}>
          <CalendarDays className="lucide-inline text-muted" aria-hidden="true" />
          <span>{range ? rangeLabel(range) : t('memoryPage.range_any')}</span>
          <ChevronDown className="lucide-inline text-muted" aria-hidden="true" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-52">
        <DropdownMenuItem onSelect={() => onChange(undefined)}><Mark on={!range} />{t('memoryPage.range_any')}</DropdownMenuItem>
        {RANGE_PRESETS.map(preset => <DropdownMenuItem key={preset} onSelect={() => onChange({ preset })}><Mark on={range?.preset === preset} />{t(PRESET_KEYS[preset])}</DropdownMenuItem>)}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => setCustom(true)}><Mark on={range?.preset === 'custom'} />{t('memoryPage.range_custom')}</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
    {custom && <CustomRangeDialog range={range} onClose={() => setCustom(false)} onApply={next => { setCustom(false); onChange(next) }} />}
  </>
}

function Mark({ on }: { on: boolean }) {
  return <Check className={`lucide-inline ${on ? '' : 'invisible'}`} aria-hidden="true" />
}

function CustomRangeDialog({ range, onClose, onApply }: { range?: DateRange; onClose: () => void; onApply: (range: DateRange) => void }) {
  const { t } = useTranslation()
  const today = dayKey(new Date())
  const [from, setFrom] = useState(range?.preset === 'custom' ? range.from || '' : '')
  const [to, setTo] = useState(range?.preset === 'custom' ? range.to || '' : '')
  const next: DateRange = { preset: 'custom', ...(from ? { from } : {}), ...(to ? { to } : {}) }
  const valid = !!rangeBounds(next)
  return <Modal open title={t('memoryPage.range_custom_title')} onClose={onClose} footer={<>
    <Btn className="min-h-11" onClick={onClose}>{t('pages.overview.memoryTab.cancel')}</Btn>
    <Btn primary className="min-h-11" disabled={!valid} onClick={() => onApply(next)}>{t('memoryPage.range_apply')}</Btn>
  </>}>
    <form className="grid gap-3 sm:grid-cols-2" onSubmit={event => { event.preventDefault(); if (valid) onApply(next) }}>
      <label className="block space-y-1.5 text-[13px]"><span className="block text-[12px] font-medium text-muted">{t('memoryPage.range_from')}</span>
        <Input type="date" className="min-h-11 w-full" value={from} max={to || today} onChange={event => setFrom(event.target.value)} /></label>
      <label className="block space-y-1.5 text-[13px]"><span className="block text-[12px] font-medium text-muted">{t('memoryPage.range_to')}</span>
        <Input type="date" className="min-h-11 w-full" value={to} min={from || undefined} max={today} onChange={event => setTo(event.target.value)} /></label>
      <button type="submit" hidden />
    </form>
  </Modal>
}
