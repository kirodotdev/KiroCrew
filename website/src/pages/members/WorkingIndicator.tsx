/**
 * WorkingIndicator — one muted line saying what the member is doing right now.
 *
 * The chat profile hides tool rows, so between a question and its reply the
 * thread would otherwise look idle. This reads the slot's live status detail
 * (the tool purpose the runner stamps while a tool runs) and the slot's
 * running flag, and draws "Working: <purpose>" with a soft pulse dot. Nothing
 * while the slot is idle.
 */
import { useTranslation } from 'react-i18next'
import { useAppSelector } from '../../store'
import { selectSlotStreamState } from '../../store/chatSlice'

export default function WorkingIndicator({ slotKey }: { slotKey: string }) {
  const { t } = useTranslation()
  const detail = useAppSelector((s) => s.chat.slotStatusDetail[slotKey])
  const slotRunning = useAppSelector((s) => !!s.dashboard.slots.find((x) => x.key === slotKey)?.running)
  const streaming = useAppSelector((s) => selectSlotStreamState(s, slotKey) !== 'idle')
  const running = slotRunning || streaming
  if (!running) return null
  // Purpose when the backend sent one; else the bare tool name (KAS/kiro emit no
  // purpose today), stripped of its `@server/` prefix; else the generic line.
  const toolName = (detail?.toolName ?? '').replace(/^@[^/]+\//, '')
  const purpose = detail?.kind === 'tool' ? (detail.text || toolName) : ''
  return (
    <div
      className="flex items-center gap-2 px-4 py-1 mx-auto w-full text-[12px] leading-5 text-muted min-w-0"
      style={{ maxWidth: 'var(--mc-content-width, 900px)' }}
      role="status"
      aria-live="polite"
      data-testid="member-working-indicator"
    >
      <span className="w-1.5 h-1.5 rounded-full shrink-0 animate-pulse" style={{ background: 'var(--accent)' }} aria-hidden="true" />
      <span className="truncate min-w-0">
        {purpose ? t('pages.members.chat.working', { purpose }) : t('pages.members.chat.working_generic')}
      </span>
    </div>
  )
}
