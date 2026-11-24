import type { ComponentType } from 'react'
import { i18nT } from '../../i18n/t'
import { lazyRetry } from '../lazyRetry'
import { actionForYouLabel, type ActionForYouStatusSlot } from './actionForYouLabel'

interface PillProps { slot: ActionForYouStatusSlot | null | undefined }
interface RowLineProps { slot: ActionForYouStatusSlot; lineCls: string; iconPx: number }

const loadPill = async (): Promise<{ default: ComponentType<PillProps> }> => {
  const module = await import('./ActionForYouStatus')
  return { default: module.ActionForYouPill }
}
const loadRowLine = async (): Promise<{ default: ComponentType<RowLineProps> }> => {
  const module = await import('./ActionForYouStatus')
  return { default: module.ActionForYouRowLine }
}

// The styled status loads with the waiting-on-you chunk; its words do not. While the
// chunk loads, or after it fails, the same sentence shows as plain text, so the
// status is never blank and a failed load stays inside its own line.
const ActionForYouPillView = lazyRetry(
  loadPill,
  { layout: 'inline', title: () => i18nT('components.errorBoundary.something_went_wrong'), scope: 'waiting-on-you-status' },
)
const ActionForYouRowLineView = lazyRetry(
  loadRowLine,
  { layout: 'inline', title: () => i18nT('components.errorBoundary.something_went_wrong'), scope: 'waiting-on-you-status' },
)

export function ActionForYouPill({ slot }: PillProps) {
  const label = actionForYouLabel(slot)
  if (!label) return null
  const plain = <span className="truncate text-[10px] font-semibold text-aim" title={label}>{label}</span>
  return <ActionForYouPillView slot={slot} loading={plain} keepOnFailure={plain} />
}

export function ActionForYouRowLine({ slot, lineCls, iconPx }: RowLineProps) {
  const label = actionForYouLabel(slot)
  if (!label) return null
  const plain = <div className={lineCls} title={label}><span className="truncate font-medium" style={{ color: 'var(--aim)' }}>{label}</span></div>
  return <ActionForYouRowLineView slot={slot} lineCls={lineCls} iconPx={iconPx} loading={plain} keepOnFailure={plain} />
}
