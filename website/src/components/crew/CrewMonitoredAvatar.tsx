import CrewStateAvatar, { type CrewStateAvatarProps } from '../CrewStateAvatar'
import { isAutomationActive, type AutomationRecord } from '../../monitoring/automation'
import CrewLoopIndicator from './CrewLoopIndicator'

export interface CrewMonitoredAvatarProps extends CrewStateAvatarProps {
  /** The normalized record for this avatar's slot. Null means no proven monitor. */
  automation?: AutomationRecord | null
  indicatorTestId?: string
}

/**
 * One avatar surface for crewmates that may be on watch.
 *
 * The reaction face and the on-watch ring stay separate visual states, but every
 * roster, header, Profile, and face-flight surface gets both from this component.
 * `isAutomationActive` is the one fail-closed state predicate.
 */
export default function CrewMonitoredAvatar({
  automation,
  indicatorTestId,
  ...avatarProps
}: CrewMonitoredAvatarProps) {
  const monitorActive = isAutomationActive(automation)
  return (
    <span
      className="relative inline-flex shrink-0 rounded-full"
      data-monitor-active={monitorActive ? 'true' : 'false'}
    >
      <CrewStateAvatar {...avatarProps} />
      <CrewLoopIndicator on={monitorActive} testId={indicatorTestId} />
    </span>
  )
}
