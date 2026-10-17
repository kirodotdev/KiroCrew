// Resolving a backend-supplied docs FILENAME to a public URL.
//
// Several surfaces carry a bare `*.md` filename from a backend catalog -- a
// feature tip's `doc`, a feature video's `doc` -- and none of them ship a resolved
// link beside it. This is the one place that turns such a filename into a URL, so
// the base and the validation live together instead of being re-derived per caller.
//
// It lives in `utils/` rather than beside its first caller on purpose: a pure
// resolver imported from a component module drags that component's whole graph
// (router, markdown renderer) into anything that wants the function, which both
// bloats a lazy chunk and breaks a harness that mounts without those providers.

/** Where the shipped docs are published. Same base the Security and Discord
 *  settings panels link to. */
const DOCS_BASE = 'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs'

/** A plain docs filename and nothing else. */
const DOC_FILENAME_RE = /^[a-z0-9][a-z0-9._-]*\.md$/i

/** The guide to passing secrets to MCP servers, including `secret://` values
 *  and routing a server through the MCP gateway. It lives in the repository's
 *  guides tree, not the shipped docs above, so it carries its own full URL. */
export const SECRETS_GUIDE_URL = 'https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/secrets-env.md'

/** The section of that guide on routing a server through the MCP gateway, the
 *  one place a `secret://` value resolves. */
export const SECRETS_GUIDE_ROUTING_URL = `${SECRETS_GUIDE_URL}#route-the-server-through-the-gateway`

/**
 * The public URL for a docs filename, or `null` when the value is not one.
 *
 * The catalog entries these come from can be authored loosely, so anything
 * carrying a scheme, a path separator, or any other shape is refused rather than
 * rendered: a caller that gets `null` drops its link, which is always better than
 * emitting something a reader cannot open or that navigates off-origin.
 */
export function tipDocHref(doc: string | undefined): string | null {
  if (!doc || !DOC_FILENAME_RE.test(doc)) return null
  return `${DOCS_BASE}/${doc}`
}
