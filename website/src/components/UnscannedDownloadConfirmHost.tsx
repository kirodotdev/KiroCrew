import { useEffect } from 'react'

import { useConfirm, type ConfirmOptions } from './ConfirmDialog'
import { i18nT } from '../i18n/t'
import { fmtBytes } from '../i18n/format'
import { setUnscannedDownloadAsker, type UnscannedDownloadInfo } from '../utils/unscannedDownload'

/** The dialog for a download over the server's scan limit. */
export function unscannedDownloadOptions(info: UnscannedDownloadInfo): ConfirmOptions {
  return {
    title: i18nT('components.unscannedDownload.title'),
    body: i18nT('components.unscannedDownload.body', {
      name: info.name,
      size: fmtBytes(info.size),
      limit: fmtBytes(info.limit),
    }),
    confirmLabel: i18nT('components.unscannedDownload.download_anyway'),
    // A download can start from the full-screen file preview (z-[9999]); the
    // question must paint above it, not under it.
    layer: 'top',
  }
}

/** Mount once: answers every `confirmUnscannedDownload` with the shared confirm
 *  dialog (focus trap, Escape and the backdrop cancel). It lives at the app root
 *  because the menus that start a download close before the answer comes. */
export default function UnscannedDownloadConfirmHost() {
  const { confirm, confirmDialog } = useConfirm()
  useEffect(
    () => setUnscannedDownloadAsker(info => confirm(unscannedDownloadOptions(info))),
    [confirm],
  )
  return <>{confirmDialog}</>
}
