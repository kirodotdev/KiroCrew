// Followed GitLab groups, shared by the Settings section that follows and
// unfollows them, the "Review requests" dashboard that lists their merge
// requests, and the rail entry that only appears once a group is followed.
// One react-query cache entry backs all three, so a follow or unfollow in
// Settings shows up in the rail and the dashboard without a reload.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback } from 'react'

import { GitLabGroupError, issueRadarApi, type GitLabGroup } from '../api'
import { i18nT } from '../../../i18n/t'

export const GITLAB_GROUPS_QUERY_KEY = ['issue-radar', 'gitlab-groups'] as const

export const groupReviewsQueryKey = (group: GitLabGroup) =>
  ['issue-radar', 'gitlab-group-reviews', group.host, group.path] as const

export const sameGroup = (a: GitLabGroup, b: GitLabGroup) => a.host === b.host && a.path === b.path

/** The followed groups. A local read on the server: it never calls GitLab. */
export function useGitlabGroups() {
  return useQuery({
    queryKey: GITLAB_GROUPS_QUERY_KEY,
    queryFn: async () => (await issueRadarApi.gitlabGroups()).groups,
    staleTime: Infinity,
  })
}

/** Follow / unfollow, keeping the shared group list in step with the server. */
export function useGitlabGroupActions() {
  const queryClient = useQueryClient()
  const follow = useCallback(async (value: string) => {
    const group = await issueRadarApi.addGitlabGroup(value)
    queryClient.setQueryData<GitLabGroup[]>(GITLAB_GROUPS_QUERY_KEY, (prev = []) => (
      prev.some((g) => sameGroup(g, group)) ? prev : [...prev, group]
    ))
    return group
  }, [queryClient])
  const unfollow = useCallback(async (group: GitLabGroup) => {
    await issueRadarApi.removeGitlabGroup(group)
    queryClient.setQueryData<GitLabGroup[]>(GITLAB_GROUPS_QUERY_KEY, (prev = []) => (
      prev.filter((g) => !sameGroup(g, group))
    ))
    queryClient.removeQueries({ queryKey: groupReviewsQueryKey(group) })
  }, [queryClient])
  return { follow, unfollow }
}

/** A failed group request as a sentence that says what to do. A failure the
 * server did not classify keeps its own message, since no better advice exists. */
export function gitlabGroupErrorText(err: unknown): string {
  const code = err instanceof GitLabGroupError ? err.code : ''
  switch (code) {
    case 'invalid_group':
    case 'missing_group':
      return i18nT('apps.issueRadar.lib.gitlabGroups.invalid_group')
    case 'group_not_found':
      return i18nT('apps.issueRadar.lib.gitlabGroups.group_not_found')
    case 'group_forbidden':
      return i18nT('apps.issueRadar.lib.gitlabGroups.group_forbidden')
    case 'setup_required':
      return i18nT('apps.issueRadar.lib.gitlabGroups.setup_required')
    case 'no_login':
      return i18nT('apps.issueRadar.lib.gitlabGroups.no_login')
    default:
      return i18nT('apps.issueRadar.lib.gitlabGroups.unreachable', {
        error: err instanceof Error ? err.message : String(err),
      })
  }
}
