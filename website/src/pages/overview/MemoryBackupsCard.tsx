import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Trans } from 'react-i18next'
import { HardDriveDownload, RotateCcw, Save, ShieldAlert } from 'lucide-react'
import { api } from '../../api/client'
import { Card, CardTitle, Btn, EmptyState } from '../../components/ui'
import { SettingsLink } from '../../components/SettingsLink'
import { fmtBytes, fmtNumber, fmtDateTimeNumeric } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { MEMORY_QUERY_PREFIXES, MemoryScopeNotice, memoryQueryRetry } from './MemoryStoreCard'

/**
 * A store's backup copies, with a way to take one and a way to put one back.
 *
 * Restore is confirmed before staging. Both lineages activate at the next
 * gateway startup, preserving the prior memory as a recovery copy.
 */

export default function MemoryBackupsCard({ store, privateMemory = false }: { store: string; privateMemory?: boolean }) {
  const queryClient = useQueryClient()
  /** The backup name whose Restore has been armed but not yet confirmed. One at a
   *  time: arming a second row disarms the first, so there is never more than one
   *  primed destructive control on screen. */
  const [armed, setArmed] = useState<string | null>(null)

  const backups = useQuery({
    queryKey: ['memory-backups', store],
    queryFn: () => api.memoryBackups(store || undefined),
    retry: memoryQueryRetry,
  })

  const backUpNow = useMutation({
    mutationFn: () => api.memoryBackupNow(store || undefined),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['memory-backups', store] })
      queryClient.invalidateQueries({ queryKey: ['memory-stores'] })
    },
  })

  const restore = useMutation({
    mutationFn: (name: string) => api.memoryRestoreBackup(name, store || undefined),
    onSuccess: () => {
      setArmed(null)
      // Refresh persisted staging status and the other recovery diagnostics.
      for (const prefix of MEMORY_QUERY_PREFIXES) {
        queryClient.invalidateQueries({ queryKey: prefix })
      }
    },
  })

  const cancelRestore = useMutation({
    mutationFn: () => api.cancelMemberMemoryRestore(store),
    onSuccess: result => {
      restore.reset()
      queryClient.setQueryData<Awaited<ReturnType<typeof api.memoryBackups>>>(
        ['memory-backups', store],
        current => current ? {
          ...current,
          pending: result.pending,
          pending_restore: result.pending_restore,
          restart_required: result.restart_required,
          activation_failed: result.activation_failed,
          restore_error: result.restore_error,
          recovery: undefined,
        } : current,
      )
      queryClient.invalidateQueries({ queryKey: ['memory-backups', store] })
    },
  })

  const rows = backups.data?.backups ?? []
  const taken = backUpNow.data
  const pendingRestore = backups.data?.pending ?? restore.data?.pending
  const recovery = backups.data?.recovery
  const recoveryError = backups.data?.restore_error
    ? new Error([
      backups.data.restore_error,
      recovery?.instruction,
      recovery?.journal,
      ...(recovery?.staged_copies ?? []),
      ...(recovery?.previous_copies ?? []),
    ].filter(Boolean).join('\n'))
    : null

  return (
    <Card>
      <CardTitle>{i18nT('pages.overview.memoryBackupsCard.backups')}</CardTitle>
      {privateMemory && <p className="mb-3 text-[13px] text-muted">{i18nT('memoryV2.backup_contents')}</p>}
      {rows.length > 0 && <p className="mb-3 text-[13px] text-muted">{i18nT('memoryV2.restore_preserves_current')}</p>}
      <div className="flex gap-2 items-center flex-wrap mb-2">
        <Btn onClick={() => backUpNow.mutate()} disabled={backUpNow.isPending}>
          <Save className="lucide-inline" aria-hidden="true" /> {i18nT('pages.overview.memoryBackupsCard.back_up_now')}
        </Btn>
        {taken && (taken.backed_up > 0 || taken.skipped > 0 || taken.pruned > 0) && (
          <span className="flex gap-3 flex-wrap text-[13px] text-muted" role="status">
            {/* The number is interpolated INTO the sentence, not appended after a
                label key: several languages put the count first. */}
            {taken.backed_up > 0 && <span>{i18nT('pages.overview.memoryBackupsCard.copies_taken', { value: fmtNumber(taken.backed_up) })}</span>}
            {taken.skipped > 0 && <span>{i18nT('memoryV2.backup_empty', { value: fmtNumber(taken.skipped) })}</span>}
            {taken.pruned > 0 && <span>{i18nT('memoryV2.backup_pruned', { value: fmtNumber(taken.pruned) })}</span>}
          </span>
        )}
      </div>
      <MemoryScopeNotice error={backups.error} />
      <MemoryScopeNotice error={taken && taken.failed > 0 ? new Error(i18nT('pages.overview.memoryBackupsCard.failed', { value: fmtNumber(taken.failed) })) : null} />
      <MemoryScopeNotice error={recoveryError} />
      {!!backups.error && privateMemory && <Btn className="mb-3 min-h-11" disabled={backups.isFetching} onClick={() => void backups.refetch()}>{i18nT('memoryV2.retry_read')}</Btn>}
      {backUpNow.error && <MemoryScopeNotice error={backUpNow.error} />}
      {restore.error && <MemoryScopeNotice error={restore.error} />}
      {cancelRestore.error && <MemoryScopeNotice error={cancelRestore.error} />}
      {restore.data?.superseded && !restore.data.pending && (
        <p className="text-[13px] text-ok" role="status">
          <Trans
            i18nKey="pages.overview.memoryBackupsCard.restored_the_database_it_replaced_was_kept_besid"
            components={{ name: <span className="font-mono">{restore.data.superseded}</span> }}
          />
        </p>
      )}
      {/* Persisted pending status survives a remount for either memory lineage. */}
      {!recoveryError && (pendingRestore || restore.isSuccess) && (
        <p className="text-[13px] text-warn" role="status">
          {i18nT(pendingRestore ? 'memoryV2.restore_pending' : 'pages.overview.memoryBackupsCard.a_restored_database_is_read_from_the_next_time_t')}
        </p>
      )}
      {!recoveryError && pendingRestore && <p className="text-[13px] text-muted">{i18nT('memoryV2.restart_interrupts')}</p>}
      {pendingRestore && rows.length > 0 && <p className="mt-2 text-[13px] text-muted">{i18nT('memoryV2.restore_other_backup_hint')}</p>}
      {pendingRestore && <div className="my-3 flex flex-wrap items-center gap-3">
        {!recoveryError && <SettingsLink className="inline-flex min-h-11 items-center text-[13px] text-accent underline underline-offset-2" tab="about">
          {i18nT('memoryV2.open_restart_controls')}
        </SettingsLink>}
        <Btn className="min-h-11" disabled={cancelRestore.isPending} onClick={() => cancelRestore.mutate()}>{i18nT('memoryV2.cancel_restore')}</Btn>
      </div>}
      {cancelRestore.isSuccess && !pendingRestore && <p role="status" className="my-3 text-[13px] text-muted">
        {i18nT(cancelRestore.data?.activation_failed ? 'memoryV2.restore_cancelled_after_failure' : 'memoryV2.restore_cancelled')}
      </p>}
      {backups.isPending && <p role="status" className="text-[13px] text-muted">{i18nT('pages.overview.memoryTab.loading')}</p>}
      {!backups.error && !backups.isPending && (
        <div className="overflow-x-auto"><table className="w-full border-collapse table-striped">
          <thead>
            <tr>
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.memoryBackupsCard.taken')}</th>
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.memoryBackupsCard.size')}</th>
              <th aria-label={i18nT('pages.overview.memoryBackupsCard.actions')} className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium"></th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={3}>
                  <EmptyState
                    icon={<HardDriveDownload className="lucide-inline" />}
                    title={i18nT('pages.overview.memoryBackupsCard.no_backups_yet')}
                    subtitle={i18nT('memoryV2.backup_empty_hint', { action: i18nT('pages.overview.memoryBackupsCard.back_up_now') })}
                  />
                </td>
              </tr>
            ) : rows.map(row => (
              <tr key={row.name} className="hover:bg-bg-hover transition-colors">
                <td className="px-2.5 py-2 border-b border-border text-sm">{fmtDateTimeNumeric(row.taken_at)}</td>
                <td className="px-2.5 py-2 border-b border-border text-sm">{fmtBytes(row.size_bytes)}</td>
                <td className="px-2.5 py-2 border-b border-border text-sm">
                  <div className="flex flex-col items-start gap-2">
                    <Btn
                      aria-expanded={armed === row.name}
                      disabled={restore.isPending || !!pendingRestore || armed === row.name}
                      onClick={() => { restore.reset(); setArmed(row.name) }}
                    >
                      <RotateCcw className="lucide-inline" aria-hidden="true" /> {i18nT('pages.overview.memoryBackupsCard.restore')}
                    </Btn>
                    {armed === row.name && (
                      <div className="flex max-w-xl flex-col items-start gap-2">
                        <span className="flex items-start gap-1.5 text-[13px] text-warn">
                          <ShieldAlert className="lucide-inline mt-0.5 shrink-0" aria-hidden="true" />
                          {i18nT('memoryV2.restore_confirm')}
                        </span>
                        <div className="flex flex-wrap gap-2">
                          <Btn danger disabled={restore.isPending || !!pendingRestore} onClick={() => { cancelRestore.reset(); restore.mutate(row.name) }}>
                            {i18nT('pages.overview.memoryBackupsCard.confirm_restore')}
                          </Btn>
                          <Btn onClick={() => setArmed(null)}>{i18nT('pages.overview.memoryBackupsCard.cancel')}</Btn>
                        </div>
                      </div>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}
    </Card>
  )
}
