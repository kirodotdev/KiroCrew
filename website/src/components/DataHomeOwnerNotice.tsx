import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { canFixDataHome, fixDataHome } from '../lib/electron'
import type { StatusData } from '../types'
import ErrorNotice from './ErrorNotice'
import { CopyCommand } from './agentHarness/CopyCommand'
import { Btn } from './ui'

/** Re-read the data home's owner at most once a minute while a reader is mounted. */
const DATA_HOME_ACCESS_STALE_MS = 60_000
export const DATA_HOME_ACCESS_QUERY_KEY = ['data-home-access'] as const

function readDataHomeAccess() {
  return api.status().then(status => (status as StatusData).data_home_access ?? null)
}

/**
 * The way out: on the desktop app a one-click repair (the OS asks for the admin
 * password and the data home is given back to this account), then the command
 * to run by hand. The button shows only where the shell can do it AND the
 * connected gateway runs on the data home the shell would repair; elsewhere (a
 * plain browser, the PWA, a remote gateway) the command alone is the way.
 */
export function DataHomeRepair({ fixCommand }: { fixCommand: string }) {
  // The gateway's own data home, from the status read Settings shares.
  const { data: access, isError: statusFailed } = useQuery({
    queryKey: DATA_HOME_ACCESS_QUERY_KEY,
    queryFn: readDataHomeAccess,
    staleTime: DATA_HOME_ACCESS_STALE_MS,
  })
  const gatewayHome = access?.home ?? ''
  const [available, setAvailable] = useState(false)
  const [state, setState] = useState<'idle' | 'fixing' | 'fixed' | 'failed'>('idle')
  useEffect(() => {
    let live = true
    void canFixDataHome(gatewayHome).then(ok => { if (live) setAvailable(ok) })
    return () => { live = false }
  }, [gatewayHome])
  // A fixed home still needs a restart: the running gateway keeps treating the
  // config it could not read as unreadable, so the notice stays and says so.
  const run = async () => {
    setState('fixing')
    const outcome = await fixDataHome(gatewayHome)
    setState(outcome === 'fixed' ? 'fixed' : outcome === 'cancelled' ? 'idle' : 'failed')
  }
  return (
    <div data-testid="data-home-repair">
      {/* No hand-off: the New Bot dialog's form is unsaved, and the hand-off
          would navigate away and discard it; the command below still works. */}
      <ErrorNotice
        message={statusFailed && !available ? i18nT('components.dataHomeOwnerNotice.check_failed') : ''}
        variant="inline"
        testId="data-home-repair-check-failed"
      />
      {available && (
        <div className="mt-2 flex flex-col gap-1" data-testid="data-home-fix">
          <div>
            {/* type="button": inside the New Bot form a bare button would also submit the create. */}
            <Btn type="button" onClick={() => { void run() }} disabled={state === 'fixing'} data-testid="data-home-fix-button">
              {state === 'fixing' ? i18nT('components.dataHomeOwnerNotice.fixing') : i18nT('components.dataHomeOwnerNotice.fix_button')}
            </Btn>
          </div>
          {state === 'fixed' && <p className="text-[13px] text-ok" role="status">{i18nT('components.dataHomeOwnerNotice.fixed')}</p>}
          {/* No hand-off: the New Bot dialog's form and any Settings draft are
              unsaved, and the hand-off would navigate away and discard them;
              the command below is the way on from here. */}
          <ErrorNotice
            message={state === 'failed' ? i18nT('components.dataHomeOwnerNotice.fix_failed') : ''}
            variant="inline"
            testId="data-home-fix-failed"
          />
        </div>
      )}
      <p className="mt-2 text-[13px] text-muted">
        {/* After a failed fix the command is the one way left, so no "Or". */}
        {i18nT(available && state !== 'failed' ? 'components.dataHomeOwnerNotice.or_run_this' : 'components.dataHomeOwnerNotice.run_this')}
      </p>
      <CopyCommand><code>{fixCommand}</code></CopyCommand>
    </div>
  )
}

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
      <p className="mt-1 text-[13px] text-muted">{i18nT('components.dataHomeOwnerNotice.blocks')}</p>
      <DataHomeRepair fixCommand={fixCommand} />
    </div>
  )
}

/** The same notice, shown only while `/api/status` reports the problem. */
export function DataHomeOwnerStatusNotice() {
  const { data, isError } = useQuery({
    queryKey: DATA_HOME_ACCESS_QUERY_KEY,
    queryFn: readDataHomeAccess,
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
