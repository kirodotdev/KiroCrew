import type { QueryClient } from '@tanstack/react-query'

/**
 * The desktop updater's own state, read and written through one source.
 *
 * `updateInfoQuery` is the ONE definition of the shared ['update-info'] query:
 * the updater's `getInfo()` (version, channel, auto-download preference). Every
 * consumer spreads this object rather than restating the key, so two spellings
 * cannot diverge on what it holds; a consumer may add `enabled`.
 *
 * Fresh for 30 seconds, then re-read when a reader mounts or the window
 * regains focus: another window of the app, or a hand edit of the shell's
 * config file, can change the preference, and no push reports it. Never
 * collected, so a cache-only reader (the settings searches) still sees the
 * entry `useUpdateSubscription` seeds at boot. A write through the bridge
 * stores the info it answers with. `null` is a bridge with no `getInfo` at
 * all: the update popup reads that as "no preference", and About as a failed
 * read.
 */
export const updateInfoQuery = {
  queryKey: ['update-info'] as const,
  queryFn: async () => (await window.updateAPI?.getInfo?.()) ?? null,
  staleTime: 30_000,
  gcTime: Infinity,
}

/**
 * Store the info a bridge write answers with, keeping the fields only
 * `getInfo()` reports (`lastState`). An answer without info (an older shell,
 * or a refusal) leaves a re-read to fetch it.
 */
export function storeAnsweredUpdateInfo(qc: QueryClient, answer: UpdateResult | undefined): void {
  if (answer?.ok && answer.info) {
    qc.setQueryData(updateInfoQuery.queryKey, (old: Awaited<ReturnType<typeof updateInfoQuery.queryFn>> | undefined) =>
      ({ ...(old ?? {}), ...answer.info }))
  } else {
    void qc.invalidateQueries({ queryKey: updateInfoQuery.queryKey })
  }
}

/** Write the auto-download preference through the same bridge the query reads. */
export function writeAutoDownload(next: boolean) {
  const set = window.updateAPI?.setAutoDownload
  if (!set) return Promise.reject(new Error('the desktop bridge has no setAutoDownload'))
  return set(next)
}

/** The window's bridge can both read and write the auto-download preference. */
export function hasAutoDownload(api: UpdateAPI | undefined): boolean {
  return typeof api?.getInfo === 'function' && typeof api.setAutoDownload === 'function'
}
