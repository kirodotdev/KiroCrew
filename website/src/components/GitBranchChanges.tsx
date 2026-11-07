import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronRight } from 'lucide-react'
import { api } from '../api/client'
import DiffBlock from './DiffBlock'
import ErrorNotice from './ErrorNotice'
import { errMessage } from '../utils/thunkError'
import { findReport } from '../utils/errorReport'
import { gitFilterRefusalCause, gitFilterRefusalCopyKey, isGitFilterRefusal } from '../utils/gitStatusError'
import { i18nT } from '../i18n/t'

/** A Git panel section: every change the branch committed since it left its
 *  base branch, across all of its commits, as one patch. Collapsed until the
 *  user opens it, and fetched only then: the server reads every shown file on
 *  both sides, so it is never on the status poll. `head` is the HEAD commit the
 *  panel's log shows, so the diff is read again whenever the log reads a new one. */
export default function GitBranchChanges({ projectDir, head }: { projectDir: string; head?: string }) {
  const [open, setOpen] = useState(false)
  const { data, error, isLoading } = useQuery({
    queryKey: ['git-branch-diff', projectDir, head],
    queryFn: () => api.projectGitBranchDiff(projectDir),
    enabled: open,
    staleTime: 30_000,
    retry: 1,
  })
  // The repo-config refusal keeps the panel's own wording for that cause.
  const refused = isGitFilterRefusal(error)

  return (
    <section className="py-2 border-t border-border" data-testid="git-branch-changes">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-1.5 px-3 pb-1.5 bg-transparent border-none cursor-pointer text-left"
        data-testid="git-branch-changes-toggle"
      >
        <ChevronRight
          size={12}
          aria-hidden="true"
          className={`text-muted shrink-0 transition-transform ${open ? 'rotate-90' : ''}`}
        />
        <span className="text-[10px] font-semibold uppercase tracking-wider text-muted">
          {i18nT('components.gitPanel.branch_changes')}
        </span>
        {open && data?.base && (
          <span className="text-[10px] text-muted font-mono truncate">
            {i18nT('components.gitPanel.branch_changes_since', { base: data.base })}
          </span>
        )}
      </button>
      {open && (
        <div className="px-3 text-[12px]">
          {isLoading ? (
            <div className="text-muted">{i18nT('components.gitPanel.loading')}</div>
          ) : error ? (
            <ErrorNotice
              message={refused
                ? i18nT(gitFilterRefusalCopyKey(gitFilterRefusalCause(error)))
                : i18nT('components.gitPanel.branch_changes_failed')}
              report={findReport(errMessage(error))}
              askAgent
              actionPlacement="below"
              testId="git-branch-changes-error"
            />
          ) : !data?.base ? (
            <div className="text-muted">{i18nT('components.gitPanel.branch_changes_no_base')}</div>
          ) : (
            <>
              {(data.omitted ?? 0) > 0 && (
                <div role="status" className="pb-1 text-[11px] text-warn" data-testid="git-branch-changes-partial">
                  {i18nT('components.gitPanel.branch_changes_partial')}
                </div>
              )}
              {data.diff ? (
                <DiffBlock code={data.diff} complete />
              ) : !data.omitted && (
                <div className="text-muted">
                  {i18nT('components.gitPanel.branch_changes_none', { base: data.base })}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </section>
  )
}
