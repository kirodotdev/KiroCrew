/**
 * After THIS machine updates, a non-modal offer to update the connected remote
 * crews that now run an older build.
 *
 * "This machine updated" is read the way the What's new dialog reads it: the
 * gateway's version differs from the one this browser last saw. The first
 * visit only records the version, so a fresh install never shows the offer.
 * The offer then reads the shared versions query, and renders nothing when no
 * connected crew is behind -- so with no remote behind, the update flow is
 * exactly what it was. Crews reconnect on their own after a restart, so it
 * re-reads for a short window until a behind crew shows up.
 *
 * Nothing here updates anything by itself: each listed crew carries its own
 * `RemoteUpdateControl`, which needs a click and its confirmation. Dismissing
 * the offer closes it for this launch; the Remote Crew rows keep the badge.
 */
import { useEffect, useState } from 'react'
import { X } from 'lucide-react'
import { useAppSelector } from '../store'
import type { InstanceVersionRow } from '../types'
import { safeSetItem } from '../utils/safeStorage'
import { useInstanceVersions } from '../hooks/useInstanceVersions'
import RemoteUpdateControl from './RemoteUpdateControl'
import ErrorNotice from './ErrorNotice'
import { errMessage } from '../utils/thunkError'
import { i18nT } from '../i18n/t'

/** Where the version this browser last offered remote updates for is kept.
 *  Separate from the What's new key, which stamps on its own schedule. */
export const REMOTE_OFFER_VERSION_KEY = 'mc-remote-offer-version'

/** Re-read cadence and window while waiting for crews to reconnect after the
 *  local restart: every 15s, for at most 8 reads (two minutes). */
const RECONNECT_POLL_MS = 15_000
const RECONNECT_MAX_READS = 8

export default function RemoteUpdateOffer() {
  const version = useAppSelector(s => s.dashboard.status?.version) || ''
  const [updatedLocally, setUpdatedLocally] = useState(false)
  const [dismissed, setDismissed] = useState(false)

  useEffect(() => {
    if (!version) return
    let seen: string | null = null
    try { seen = localStorage.getItem(REMOTE_OFFER_VERSION_KEY) } catch { seen = null }
    if (seen === version) return
    if (seen) setUpdatedLocally(true)
    safeSetItem(REMOTE_OFFER_VERSION_KEY, version)
  }, [version])

  const versionsQ = useInstanceVersions(updatedLocally && !dismissed, (data, reads) => {
    if (reads >= RECONNECT_MAX_READS) return false
    // Keep reading while a crew has not answered: it may still be reconnecting.
    const settled = data?.instances.some(r => r.behind) && !data.instances.some(r => r.error)
    return settled ? false : RECONNECT_POLL_MS
  })
  // The list is fixed the first time behind crews show up, so a crew the user
  // updates from here stays listed with its outcome after it stops being behind.
  const [listed, setListed] = useState<InstanceVersionRow[] | null>(null)
  const data = versionsQ.data
  useEffect(() => {
    if (listed !== null || !data) return
    const found = data.instances.filter(r => r.behind)
    if (found.length > 0) setListed(found)
  }, [listed, data])
  if (!updatedLocally || dismissed) return null
  // A 403 is the routine answer where remote crews are off (or the caller is
  // not the owner): there is nothing to offer, so it stays silent. Any other
  // failure is reported, or a failed read would look like "every crew is current".
  const readError = versionsQ.error && !isRoutineDenial(versionsQ.error) ? versionsQ.error : null
  const behind = listed ?? data?.instances.filter(r => r.behind) ?? []
  // Crews that did not answer: their version is unknown, so they may be behind.
  const unread = (data?.instances ?? []).filter(r => r.error).map(r => r.name)
  if (behind.length === 0 && !readError && unread.length === 0) return null
  const local = data?.local_version || version
  const title = behind.length === 0
    ? i18nT('components.remoteUpdateOffer.read_failed_title')
    : i18nT('components.remoteUpdateOffer.title')

  return (
    <div
      role="status"
      aria-label={title}
      data-testid="remote-update-offer"
      // Bounded to the viewport (less the 1rem offsets and the safe areas) as a
      // column, so on a short phone screen the header and Dismiss stay on
      // screen and only the body below them scrolls -- several failed updates
      // with their error tails would otherwise push the card off the top.
      className="fixed bottom-safe-offset-4 right-safe-offset-4 z-40 flex flex-col w-[min(420px,calc(100vw-2rem))] max-h-[calc(100dvh_-_2rem_-_var(--safe-area-top,env(safe-area-inset-top))_-_env(safe-area-inset-bottom))] bg-bg-elevated border border-border rounded-lg shadow-lg p-3 animate-rise"
    >
      <div className="flex items-start gap-2 shrink-0">
        <div className="flex-1 min-w-0">
          <div className="text-[13px] font-medium text-text">{title}</div>
          {behind.length > 0 && (
            <div className="text-[12px] text-muted mt-0.5">{i18nT('components.remoteUpdateOffer.body', { local })}</div>
          )}
        </div>
        <button
          type="button"
          onClick={() => setDismissed(true)}
          aria-label={i18nT('components.remoteUpdateOffer.dismiss')}
          className="shrink-0 text-muted hover:text-text p-1 rounded"
        >
          <X size={14} aria-hidden />
        </button>
      </div>
      <div data-testid="remote-update-offer-body" className="min-h-0 overflow-y-auto overscroll-contain">
        {readError && (
          <>
            {/* No hand-off: this card floats over whatever page is open,
                including a chat composer with an unsent draft or a half-filled
                settings form, and a hand-off navigates away from it. */}
            <ErrorNotice variant="inline" className="mt-2" message={errMessage(readError)} />
          </>
        )}
        {!readError && unread.length > 0 && (
          <>
            {/* No hand-off: same floating card as above, over unsaved drafts. */}
            <ErrorNotice
              variant="inline"
              className="mt-2"
              message={i18nT('components.remoteUpdateOffer.unread', { names: unread.join(', ') })}
            />
          </>
        )}
        {behind.length > 0 && (
          <ul className="mt-2 flex flex-col gap-2">
            {behind.map(r => (
              <li key={r.id} className="flex items-center justify-between gap-3 flex-wrap">
                <span className="text-[13px] text-text truncate min-w-0">
                  {i18nT('components.remoteUpdateOffer.row', { name: r.name, remote: r.version })}
                </span>
                {r.updatable && <RemoteUpdateControl instanceId={r.id} name={r.name} />}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

/** A 403: remote crews are off here, or the caller is not the owner. */
function isRoutineDenial(err: unknown): boolean {
  return typeof err === 'object' && err !== null && (err as { status?: unknown }).status === 403
}
