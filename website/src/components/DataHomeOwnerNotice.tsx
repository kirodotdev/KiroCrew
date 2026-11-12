import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import type { StatusData } from '../types'
import ErrorNotice from './ErrorNotice'

/** Re-read the data home's owner at most once a minute while a reader is mounted. */
const DATA_HOME_ACCESS_STALE_MS = 60_000

/** What is wrong, in one sentence: which folder, and which account owns its files. */
export function dataHomeOwnerMessage(home: string, owner: string): string {
  return owner
    ? i18nT('components.dataHomeOwnerNotice.owned_by', { home, owner })
    : i18nT('components.dataHomeOwnerNotice.unusable', { home })
}

/**
 * The likely cause and the way out: stop running as root, and the docs section
 * that explains giving the files back by hand. Kiro Crew gives no command to
 * copy: changing owners as root is the person's own step, taken with nothing
 * else running.
 */
export function DataHomeOwnerHelp({ helpUrl }: { helpUrl: string }) {
  return (
    <p className="mt-2 text-[13px] text-muted">
      {i18nT('components.dataHomeOwnerNotice.cause')}{' '}
      <a
        href={helpUrl}
        target="_blank"
        rel="noopener noreferrer"
        className="underline"
        data-testid="data-home-owner-help-link"
      >
        {i18nT('components.dataHomeOwnerNotice.help_link')}
      </a>
    </p>
  )
}

/**
 * Some files in the data home belong to another account (an earlier
 * `sudo kirocrew ...`), so this account cannot read them and a new Bot cannot be
 * created. Says which folder and owner, the likely cause, and where the fix is.
 */
export function DataHomeOwnerNotice({ home, owner, helpUrl, testId = 'data-home-owner-notice' }: {
  home: string
  owner: string
  helpUrl: string
  testId?: string
}) {
  return (
    <div data-testid={testId}>
      {/* No hand-off: it renders above a Settings tab that may hold an unsaved
          draft, and the hand-off would navigate away and discard it. The way out
          is the docs link below. */}
      <ErrorNotice message={dataHomeOwnerMessage(home, owner)} variant="inline" />
      <DataHomeOwnerHelp helpUrl={helpUrl} />
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
  if (!data || data.ok) return null
  return (
    <div className="mb-4">
      <DataHomeOwnerNotice
        home={data.home}
        owner={data.owner}
        helpUrl={data.help_url}
        testId="settings-data-home-owner"
      />
    </div>
  )
}
