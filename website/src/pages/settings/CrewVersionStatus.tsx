/**
 * The version line on a CONNECTED Remote Crew row: the "Behind" badge when the
 * crew runs an older version than this machine, and beside it the explicit
 * Update action (see `RemoteUpdateControl`).
 *
 * Every row reads the SAME versions query (`useInstanceVersions`), so a list of
 * N crews costs one `GET /api/instances/versions` rather than N capability
 * reads. The row's `updatable` comes from that read too: a chained or fargate
 * crew still gets the badge but no Update, and the gateway refuses the update
 * for those regardless of what a row offers.
 *
 * Once an update has finished, the control stays to show its outcome after the
 * badge goes (the crew is no longer behind); it reads the job only from the
 * cache for that, so a current crew costs no job read.
 */
import ErrorNotice from '../../components/ErrorNotice'
import RemoteBehindBadge from '../../components/RemoteBehindBadge'
import RemoteUpdateControl, { isSettledOutcome, useRemoteUpdateJob } from '../../components/RemoteUpdateControl'
import { useInstanceVersions } from '../../hooks/useInstanceVersions'
import { errMessage } from '../../utils/thunkError'
import { i18nT } from '../../i18n/t'

export default function CrewVersionStatus({
  instanceId,
  name,
  connected,
}: {
  instanceId: string
  name: string
  connected: boolean
}) {
  const versionsQ = useInstanceVersions(connected)
  const cachedJob = useRemoteUpdateJob(instanceId, false).data?.job
  // A failed read is reported once for the whole list (CrewVersionsReadError),
  // not repeated on every row.
  if (!connected || versionsQ.error) return null
  const row = versionsQ.data?.instances.find(r => r.id === instanceId)
  if (!row) return null
  if (row.error) {
    return (
      <>
        {/* No hand-off: the Remote Crew panel this row sits on holds the add
            and edit crew forms, whose unsaved values a hand-off would discard. */}
        <ErrorNotice
          variant="inline"
          className="max-w-[420px]"
          message={i18nT('components.remoteBehindBadge.row_read_failed', { name, code: row.error })}
        />
      </>
    )
  }
  const showOutcome = row.updatable && isSettledOutcome(cachedJob)
  if (!row.behind && !showOutcome) return null
  // While an update waits for the crew to reconnect, the version read is the
  // pre-update one; the control says so, and the stale badge would contradict it.
  const awaitingVersion = cachedJob?.state === 'unverified' && row.version === cachedJob.version_before
  return (
    <>
      {!awaitingVersion && (
        <RemoteBehindBadge
          className="whitespace-normal max-w-full break-words"
          behind={row.behind}
          remote={row.version}
          local={versionsQ.data?.local_version || ''}
        />
      )}
      {row.updatable && <RemoteUpdateControl instanceId={instanceId} name={name} />}
    </>
  )
}

/**
 * The one report of a failed versions read, above the crew list, so a failure
 * never reads as "every crew is current". Reads the same shared query the rows
 * do, so it costs no request of its own.
 */
export function CrewVersionsReadError({ anyConnected }: { anyConnected: boolean }) {
  const versionsQ = useInstanceVersions(anyConnected)
  if (!anyConnected || !versionsQ.error) return null
  return (
    <>
      {/* No hand-off: the Remote Crew panel this sits on holds the add and
          edit crew forms, whose unsaved values a hand-off would discard. */}
      <ErrorNotice
        variant="inline"
        className="mb-2"
        title={i18nT('components.remoteBehindBadge.read_failed')}
        message={errMessage(versionsQ.error)}
      />
    </>
  )
}
