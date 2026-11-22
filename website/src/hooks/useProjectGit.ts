import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'

/** Branch label for a project chip, shared by the main chat and a split pane.
 *  The user can check out a different branch outside the dashboard at any
 *  time, so this refetches on a slow interval and on window focus rather than
 *  being read once. A failure (no git, path gone, not a repo) leaves the chip
 *  showing the folder name alone. */
export function useProjectGit(project: string) {
  const { data: projectGit, isError: projectGitError, error: projectGitFailure } = useQuery({
    queryKey: ['project-git', project],
    queryFn: () => api.projectGit(project),
    enabled: !!project,
    staleTime: 15_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
    retry: false,
  })
  // React Query keeps the last successful data after a failed refetch, so a
  // project that was deleted or revoked would keep showing its old branch
  // indefinitely. Treat an errored query as "no branch" and fall back to the
  // folder name, which is the same degradation as a non-repo project.
  const projectBranch = projectGitError
    ? ''
    : projectGit?.branch || (projectGit?.detached ? projectGit.head || '' : '')
  return { projectGit, projectGitError, projectGitFailure, projectBranch }
}
