import CrewStateAvatar, { type CrewStateAvatarProps } from '../CrewStateAvatar'
import { isAutomationActive, type AutomationRecord } from '../../monitoring/automation'
import CrewLoopIndicator from './CrewLoopIndicator'

export interface CrewMonitoredAvatarProps extends CrewStateAvatarProps {
  /** The normalized record for this avatar's slot. Null means no proven monitor. */
  automation?: AutomationRecord | null
  /** On watch by another route than the slot's monitor: the crewmate's
   *  heartbeat is on, so it will act again without being asked. ORed with the
   *  record, so a host that already resolved the one reading its filters
   *  count (`MemberSignals.patrolling`) can hand it in whole. */
  watch?: boolean
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
  watch = false,
  indicatorTestId,
  ...avatarProps
}: CrewMonitoredAvatarProps) {
  const monitorActive = isAutomationActive(automation) || watch
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
