/**
 * The merge-blocker → agent handoff prompts. Model-facing text only, per the
 * `*.prompt.ts` convention in `eslint.i18n.config.js`: everything here is the
 * body of a message the Changes panel pre-fills for the agent when the user
 * clicks "Add to chat" on a banner, so it is English by design and carries no
 * UI copy. The banner the user reads (title and detail) stays in
 * `PullRequestPanel.tsx`, translated through the catalog.
 */

export interface MergeBlockerHandoffSource {
  provider: string
  title: string
  headBranch?: string
  /** The provider's own label for the change (`PR #12`, `MR !7`, `Change 466698`). */
  label: string
  /** The base branch name, already defaulted by the caller. */
  base: string
  /** The change's url once it passed the external-url check, else `''`. */
  sourceUrl: string
}

/**
 * The lines every handoff opens with: what is wrong, on which change, between
 * which branches, and where to find it. A Gerrit change is a change, not a
 * pull request, so its link line says so.
 */
function handoffHeader(problem: string, source: MergeBlockerHandoffSource): string[] {
  const linkWord = source.provider === 'gerrit' ? 'Change' : 'Pull request'
  return [
    `${problem} on ${source.label} (${source.title}):`,
    '',
    `- Branch: ${source.headBranch || 'the feature branch'} -> ${source.base}`,
    ...(source.sourceUrl ? [`- ${linkWord}: ${source.sourceUrl}`] : []),
    '',
  ]
}

/**
 * Conflicts with the base. A Gerrit change is amended, never force-pushed: the
 * fix is a new patchset of the same commit (same Change-Id) pushed to
 * `refs/for/<base>`. Everywhere else the branch is rewritten, with the lease
 * guarding a shared branch.
 */
export function mergeConflictHandoff(source: MergeBlockerHandoffSource): string {
  const { base } = source
  const body = source.provider === 'gerrit'
    ? `Rebase the change onto ${base}, resolve the conflicts, keep the Change-Id, and push the result as a new patchset (\`git review\` or \`git push origin HEAD:refs/for/${base}\`). Never force-push a Gerrit change.`
    : `Resolve the conflicts with ${base}. If the branch is shared with other contributors, prefer merging ${base} into the branch; otherwise rebase onto ${base} and push with \`git push --force-with-lease\` (abort if the lease fails).`
  return [...handoffHeader('Merge conflict', source), body].join('\n')
}

/**
 * The project requires a rebase (a fast-forward-only merge method), so a merge
 * commit cannot unblock it. The deliberately rebase-aware wording: a merge of
 * the base into the branch would not help here.
 */
export function rebaseRequiredHandoff(source: MergeBlockerHandoffSource): string {
  const { base } = source
  const body = source.provider === 'gerrit'
    ? `This project is fast-forward-only, so the change must be rebased onto ${base} (the Gerrit "Rebase" button does this when there are no conflicts). Keep the Change-Id and push the result as a new patchset (\`git review\` or \`git push origin HEAD:refs/for/${base}\`). Never force-push a Gerrit change.`
    : `This project requires a rebase (merge commits will not unblock the MR). If the branch is shared with other contributors, coordinate with them before rewriting history; otherwise rebase onto ${base} and push with \`git push --force-with-lease\` (abort if the lease fails).`
  return [...handoffHeader('Rebase required', source), body].join('\n')
}

/**
 * Behind the base with no conflict. The handoff deliberately avoids history
 * rewriting (merge the base into the branch); rebase plus force-push is
 * reserved for genuine conflicts.
 */
export function branchBehindHandoff(source: MergeBlockerHandoffSource): string {
  const { base } = source
  return [
    ...handoffHeader('Out-of-date branch', source),
    `Update the branch without rewriting history: merge ${base} into the branch (or use the provider's update-branch option) and push normally. Only rebase if the repository requires a linear history, and never force-push a shared branch without checking with its other contributors.`,
  ].join('\n')
}
