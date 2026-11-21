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
    const js = await fetchRuntimeText(window.location.origin + withBase(TAILWIND_RUNTIME_PATH))
    if (!js) throw new Error('tailwind runtime: read failed')
    return js
  },
  staleTime: Infinity,
  retry: false,
}

/** The runtime text at *url*, read with the page's cookie; '' when refused.
 * The one reader of the runtime: the saved dashboard file uses it too. */
export async function fetchRuntimeText(url: string): Promise<string> {
  const res = await fetch(url, { credentials: 'same-origin' })
  if (!res.ok) return ''
  return res.text()
}
