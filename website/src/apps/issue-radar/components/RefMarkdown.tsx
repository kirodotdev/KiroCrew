// Markdown body (issue/PR description or comment) whose SAME-REPO issue and PR
// references open inside the app instead of in the browser.
//
// Drop-in replacement for <MarkdownRenderer content={…} /> at the body call
// sites of the two detail panes. Two things happen here:
//
//   1. Bare `#123` shorthand is rewritten into a real markdown link, because the
//      raw markdown GitHub's API returns carries it as plain text (GitHub's own
//      web UI linkifies it at render time).
//   2. Every rendered anchor that resolves to a reference into the ACTIVE repo is
//      rendered as <RefLink> instead of a plain anchor, via MarkdownRenderer's
//      `LinkOverrideCtx` seam.
//
// The markdown pipeline itself is untouched, and no rendered DOM is mutated after
// the fact: every link the app does NOT claim (other repos, other hosts, docs,
// commits) renders exactly as before.
import { useCallback, useMemo } from 'react'
import MarkdownRenderer, { LinkOverrideCtx, type LinkOverride } from '../../../components/MarkdownRenderer'
import { useIssueRadar } from '../context'
import { linkifyIssueRefs, parseRepoRef } from '../lib/refLinks'
import RefLink from './RefLink'

export default function RefMarkdown({ content }: { content: string }) {
  const { active } = useIssueRadar()
  const { owner, repo } = active

  const linkified = useMemo(() => linkifyIssueRefs(content, owner, repo), [content, owner, repo])

  const override = useCallback<LinkOverride>(({ href, children }) => {
    const target = parseRepoRef(href, owner, repo)
    if (!target) return null
    return <RefLink target={target} href={href}>{children}</RefLink>
  }, [owner, repo])

  return (
    <LinkOverrideCtx.Provider value={override}>
      <MarkdownRenderer content={linkified} />
    </LinkOverrideCtx.Provider>
  )
}
