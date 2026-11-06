// The Tailwind v4 browser runtime carried inside a document built in the
// browser, instead of linked from it.
//
// A widget's "Open in new tab" and "Download", and an html artifact's export,
// build their document here and open it as a blob or save it as a file. That
// document's own request for the linked runtime carries no session cookie, so a
// dashboard reached through a proxy that authenticates every request by cookie
// (a single-sign-on tunnel) has it refused and the page renders unstyled; a
// saved file opened later cannot reach the dashboard at all. The dashboard's own
// request for the same file does carry the cookie, so it reads the runtime once
// and buildSrcdoc puts it inside those documents.

import { withBase } from './basePath'
import { TAILWIND_RUNTIME_PATH } from './vendorPaths'

/** The runtime source, read once per page and shared by every surface that
 * builds a document to open or save. A static same-origin file outside the API,
 * so it is read with a plain same-origin fetch, which sends the cookie a proxy
 * in front of the dashboard checks. A failed read leaves the link in place. */
export const tailwindRuntimeQuery = {
  queryKey: ['tailwind-runtime'] as const,
  queryFn: async (): Promise<string> => {
    const r = await fetch(withBase(TAILWIND_RUNTIME_PATH), { credentials: 'same-origin' })
    if (!r.ok) throw new Error(`tailwind runtime: HTTP ${r.status}`)
    return r.text()
  },
  staleTime: Infinity,
  retry: false,
}

/** `js` made safe to put between `<script>` and `</script>`, or null.
 *
 * Every casing of `</script` is neutralised as `<\/script`, which the script
 * reads back unchanged. `<!--` is refused rather than escaped: after it, a later
 * `<script` puts the HTML tokenizer in its double-escaped state, where the
 * closing `</script>` does not end the element. The bundle contains neither
 * today; refusing keeps a dependency bump from breaking the document instead of
 * falling back to the link. */
export function runtimeScriptText(js: string): string | null {
  if (!js || js.includes('<!--')) return null
  return js.replace(/<\/(script)/gi, '<\\/$1')
}
