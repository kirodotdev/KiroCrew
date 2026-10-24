import { useAppSelector } from '../store'
import { selectSlotSubagentsActive } from '../store/chatSlice'

/**
 * Whether a Reload of this slot would be refused right now. The backend 409s
 * a reload while a turn runs or while sub-agent children are attached (the
 * reset would tear down their shared runtime), so every Reload control
 * disables on the same answer instead of offering a click that fails.
 */
export function useReloadBlocked(slotKey: string): { isRunning: boolean; reloadBlocked: boolean } {
  const isRunning = useAppSelector(s => !!s.dashboard.slots.find(x => x.key === slotKey)?.running)
  const hasActiveSubagents = useAppSelector(s => selectSlotSubagentsActive(s, slotKey))
  return { isRunning, reloadBlocked: isRunning || hasActiveSubagents }
}
