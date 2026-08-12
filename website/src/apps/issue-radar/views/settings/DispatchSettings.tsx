/**
 * DispatchSettings — which copy of this repository the work hangs off.
 *
 * NOT the directory an agent edits. `git worktree add` needs an existing
 * repository to attach to, and each attempt gets its own worktree branched off
 * this one, so the user's own working tree is never the thing being edited. The
 * field names the base; the leaf is per issue.
 *
 * ## Why this is a setting and not something we discover
 *
 * Issue Radar reads everything through the user's own provider CLI, which is much
 * of why connecting a repo needs one dialog and no clone. Adding a worktree needs
 * a repository to add it TO, and there is no honest way to guess which one: a
 * machine can hold several copies of the same repository, and `git worktree add`
 * MUTATES the one it picks (a branch, plus an entry under .git/worktrees). Writing
 * into a repository the user never named is not a discovery problem, it is a
 * permission one. So the value is typed here, once, and dispatch is gated on it.
 *
 * ## A rejected path keeps its text, and says why
 *
 * The server validates and REFUSES rather than falling back — an unusable path
 * stored as if it were fine is worse than no path at all. So a failed write leaves
 * what the user typed on screen with the reason beside it, which is the difference
 * between fixing one character and retyping an absolute path from memory. Snapping
 * the field back to the saved value would read exactly like a successful save of a
 * value the server never took.
 *
 * ## The saved value is the SERVER's, not the draft
 *
 * The stored path is the resolved one (symlinks followed), so after a successful
 * write the field shows what the server kept rather than what was typed. A user
 * who pastes a symlink should see where it actually points, because that is the
 * directory the work will happen in.
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { Input } from '../../../../components/ui'
import { issueRadarApi, type DispatchReadiness, type RepoRef } from '../../api'
import { repoScopeKey } from '../../lib/links'

import { i18nT } from '../../../../i18n/t'

export default function DispatchSettings({ repoRef }: { repoRef: RepoRef }) {
  const queryClient = useQueryClient()
  const scopeKey = repoScopeKey(repoRef)
  // Scoped to the repo so two mounted cards cannot share one label target.
  const inputId = `ir-dispatch-checkout-${scopeKey}`
  const key = ['issue-radar', 'dispatch-readiness', scopeKey]

  const readinessQuery = useQuery({
    queryKey: key,
    queryFn: () => issueRadarApi.getDispatchReadiness(repoRef),
    staleTime: 30_000,
  })
  const readiness = readinessQuery.data ?? null
  // An OVERLAY, not a copy: with no edit in flight the field shows the server's
  // value, so a refetch that lands while the field is untouched is picked up
  // without an effect and without clobbering what is being typed.
  const [draft, setDraft] = useState<string | null>(null)
  const [refusal, setRefusal] = useState<string | null>(null)
  const value = draft ?? readiness?.local_path ?? ''

  const saveMutation = useMutation({
    mutationFn: (next: string) => issueRadarApi.setRepoLocalPath(repoRef, next),
    onSuccess: (saved: DispatchReadiness) => {
      queryClient.setQueryData<DispatchReadiness>(key, saved)
      // Released only by the server's answer, and released to the RESOLVED path.
      setDraft(null)
      setRefusal(null)
    },
    onError: (e: Error & { code?: string }) => setRefusal(
      // A known refusal is said in the catalog's words; an unknown one falls back
      // to the server's text, which is better than a generic apology that hides
      // what went wrong.
      e.code === 'invalid_local_path'
        ? i18nT('apps.issueRadar.dispatch.refusedInvalidPath')
        : e.message,
    ),
  })

  const commit = () => {
    const next = value.trim()
    if (next === (readiness?.local_path ?? '')) {
      // Nothing to write, so nothing to wait for — drop the draft at once rather
      // than leaving the field looking edited forever.
      setDraft(null)
      setRefusal(null)
      return
    }
    if (next && !next.startsWith('/') && !next.startsWith('~')) {
      // Refused locally for the one rule the client can check without a stat.
      // Everything else (exists, is a git checkout, is not protected) is the
      // server's to judge, and it answers with a reason.
      setRefusal(i18nT('apps.issueRadar.dispatch.mustBeAbsolute'))
      return
    }
    setRefusal(null)
    saveMutation.mutate(next)
  }

  const status = readiness === null
    ? i18nT('apps.issueRadar.dispatch.statusChecking')
    : readiness.reason === 'no_local_path'
      ? i18nT('apps.issueRadar.dispatch.statusNoLocalPath')
      : readiness.reason === 'checkout_unusable'
        ? i18nT('apps.issueRadar.dispatch.statusCheckoutUnusable')
        : i18nT('apps.issueRadar.dispatch.statusReady')

  return (
    <div>
      <label htmlFor={inputId} className="block text-[12.5px] font-medium mb-1">
        {i18nT('apps.issueRadar.dispatch.fieldLabel')}
      </label>
      <div className="flex items-center gap-2">
        <Input
          id={inputId}
          value={value}
          placeholder={i18nT('apps.issueRadar.dispatch.placeholder')}
          disabled={readinessQuery.isLoading || saveMutation.isPending}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => { if (e.key === 'Enter') commit() }}
          className="flex-1 text-[13px]"
        />
        <button
          onClick={commit}
          disabled={readinessQuery.isLoading || saveMutation.isPending}
          className={
            'text-[12px] px-2.5 py-1 rounded-md border-none font-medium bg-accent text-accent-fg ' +
            'hover:bg-accent-hover disabled:opacity-40 disabled:cursor-default cursor-pointer'
          }
        >
          {i18nT('apps.issueRadar.dispatch.save')}
        </button>
      </div>
      <p className="text-[11.5px] text-muted mt-1.5">
        {i18nT('apps.issueRadar.dispatch.fieldHint')}
      </p>
      <p
        className={
          'text-[11.5px] mt-1 ' +
          (readiness?.ready ? 'text-aim' : 'text-muted')
        }
      >
        {status}
      </p>
      {refusal && (
        <p className="text-[11.5px] text-danger mt-1">{refusal}</p>
      )}
    </div>
  )
}
