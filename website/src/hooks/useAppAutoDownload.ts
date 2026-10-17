/**
 * The desktop app updater's auto-download switch as Settings → About and the
 * "What's new" modal read and write it, in the same shape as
 * `useGatewayAutoUpdate`. Reads and writes go through `api/updateInfoQuery`,
 * the one source for the window's bridge.
 *
 * `held` only while the updater's `getInfo()` has not answered. After a failed
 * read the switch stays usable at the shell's own default, on, since it is
 * still the way to opt out. A write shows its value while in flight, then
 * stores the info the bridge answers with.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { storeAnsweredUpdateInfo, updateInfoQuery, writeAutoDownload } from '../api/updateInfoQuery'
import { failedWithNoData } from '../api/queryState'
import { i18nT } from '../i18n/t'
import type { SwitchState } from './useGatewayAutoUpdate'

export function useAppAutoDownload(): SwitchState {
  const qc = useQueryClient()
  const info = useQuery(updateInfoQuery)
  const save = useMutation({
    mutationFn: writeAutoDownload,
    onSuccess: answer => storeAnsweredUpdateInfo(qc, answer),
    onError: () => qc.invalidateQueries({ queryKey: updateInfoQuery.queryKey }),
  })
  const readFailed = failedWithNoData(info)
  // The bridge answers a refused write with `ok: false` rather than a throw.
  const saveFailed = save.isError || (save.data !== undefined && !save.data.ok)
  // An older shell reports no preference; auto-download is its default.
  const saved = info.data !== undefined ? info.data?.autoDownload !== false : undefined
  return {
    checked: save.isPending ? save.variables : saved ?? true,
    held: saved === undefined && !readFailed,
    readFailed,
    refreshFailed: info.isError && info.data !== undefined
      ? i18nT('pages.settings.aboutPanel.auto_download_refresh_failed')
      : null,
    set: next => save.mutate(next),
    saveError: saveFailed ? i18nT('pages.settings.aboutPanel.auto_download_save_failed') : null,
  }
}
