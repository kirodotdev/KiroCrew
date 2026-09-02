// GitHub URL helpers for this app's own repository.
// Branch/commit come from git (via the dashboard status payload). They are
// wrapped in encodeURI(), which escapes spaces / '%' / other unsafe chars while
// preserving '/' — GitHub wants '/' literal in branch refs (e.g. "feat/foo"),
// and hex commit SHAs pass through unchanged.

const CODE_BROWSER_PACKAGE_BASE = 'https://github.com/kirodotdev/KiroCrew'

/** Browse a branch's source tree at its HEAD. */
export const codeBrowserBranchUrl = (branch: string): string =>
  `${CODE_BROWSER_PACKAGE_BASE}/tree/${encodeURI(branch)}`

/** View a single commit (diff). */
export const codeBrowserCommitUrl = (commit: string): string =>
  `${CODE_BROWSER_PACKAGE_BASE}/commit/${encodeURI(commit)}`
