import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import type { StatusData } from '../types'
import ErrorNotice from './ErrorNotice'
import { CopyCommand } from './agentHarness/CopyCommand'

/** Re-read the data home's owner at most once a minute while a reader is mounted. */
const DATA_HOME_ACCESS_STALE_MS = 60_000

/**
 * Some files in the data home belong to another account (an earlier
 * `sudo kirocrew ...`), so this account cannot read them and a new Bot cannot be
 * created. Says so in one sentence and gives the gateway's own repair line to copy.
 */
export function DataHomeOwnerNotice({ fixCommand, testId = 'data-home-owner-notice' }: {
  fixCommand: string
  testId?: string
}) {
  return (
    <div data-testid={testId}>
      {/* No hand-off: it renders inside the New Bot dialog, whose form is unsaved,
          and above a Settings tab that may hold an unsaved draft; the hand-off
          would navigate away and discard either. The repair is the command below. */}
      <ErrorNotice message={i18nT('components.dataHomeOwnerNotice.message')} variant="inline" />
      <p className="mt-2 text-[13px] text-muted">{i18nT('components.dataHomeOwnerNotice.run_this')}</p>
      <CopyCommand><code>{fixCommand}</code></CopyCommand>
    </div>
  )
}

/** The same notice, shown only while `/api/status` reports the problem. */
export function DataHomeOwnerStatusNotice() {
  const { data, isError } = useQuery({
    queryKey: ['data-home-access'],
    queryFn: async () => ((await api.status()) as StatusData).data_home_access ?? null,
    staleTime: DATA_HOME_ACCESS_STALE_MS,
  })
  if (isError) {
    return (
      <div className="mb-4">
        {/* No hand-off: the Settings tab below may hold an unsaved draft, and the
            hand-off would navigate away and discard it; the read retries in place. */}
        <ErrorNotice
          message={i18nT('components.dataHomeOwnerNotice.check_failed')}
          variant="inline"
          testId="settings-data-home-owner-check-failed"
        />
      </div>
    )
  }
  if (!data || data.ok || !data.fix_command) return null
  return (
    <div className="mb-4">
      <DataHomeOwnerNotice fixCommand={data.fix_command} testId="settings-data-home-owner" />
    </div>
  )
}
