import { useTranslation } from 'react-i18next'
import { useAppDispatch } from '../store'
import { deleteHistorySession } from '../store/chatSlice'
import { normalizeTerminalScope, usePreparedTerminalRetirements } from '../hooks/useBottomTerminal'
import { closePreparedTerminals, historyDeletionInFlight, retryHistoryTerminalCleanup, useHistoryTerminalCleanupFailures } from '../lib/historyTerminalCleanup'
import ErrorNotice from './ErrorNotice'
import { Btn } from './ui'

export default function HistoryTerminalCleanupNotice() {
  const { t } = useTranslation()
  const dispatch = useAppDispatch()
  const failures = useHistoryTerminalCleanupFailures()
  const prepared = usePreparedTerminalRetirements().filter(lease => !historyDeletionInFlight(lease.scope)
    && !failures.some(failure => failure.phase !== 'delete' && normalizeTerminalScope(failure.key) === lease.scope))
  if (!failures.length && !prepared.length) return null
  return <div className="max-h-40 overflow-y-auto shrink-0">
    {failures.map(failure => <div key={failure.key} className="mx-2 my-1 break-words" data-testid="history-terminal-cleanup-error">
      {/* No hand-off: another chat's composer may contain an unsent draft. */}
      <ErrorNotice variant="inline" message={failure.phase === 'cleanup'
        ? t('components.bottomTerminalPanel.history_cleanup_failed', { session: failure.key })
        : failure.phase === 'terminals' ? t('components.bottomTerminalPanel.terminal_cleanup_failed', { session: failure.key })
          : t('components.bottomTerminalPanel.history_delete_failed', { session: failure.key })} />
      <Btn disabled={failure.busy} onClick={() => {
        if (failure.phase !== 'delete') void retryHistoryTerminalCleanup(failure.key)
        else void dispatch(deleteHistorySession(failure.key))
      }}>{t('pages.chatSidebar.retry')}</Btn>
    </div>)}
    {prepared.map(lease => <div key={lease.scope} className="mx-2 my-1 break-words" data-testid="interrupted-terminal-retirement">
      {/* No hand-off: another chat's composer may contain an unsent draft. */}
      <ErrorNotice variant="inline" message={t('components.bottomTerminalPanel.history_deletion_unconfirmed', { session: lease.scope })} />
      <Btn onClick={() => void closePreparedTerminals(lease)}>{t('components.bottomTerminalPanel.close_terminals')}</Btn>
    </div>)}
  </div>
}
