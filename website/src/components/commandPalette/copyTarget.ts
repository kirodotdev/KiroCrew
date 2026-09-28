import { safeHttpUrl } from '../../lib/safeUrl'
import type { EnterAction } from './types'

/**
 * What a launcher row's thing is addressed BY, so the launcher can hand that
 * address to the clipboard.
 *
 * This exists because the capability already existed three times and the
 * launcher could reach none of it: `utils/shareUrl` copies a session link,
 * `WebAppArtifactCard` copies a deployed artifact's URL, `CopyBranchButton`
 * copies a branch name -- three buttons on three surfaces, each knowing how to
 * address one kind of thing. A launcher that lists all of those things could
 * not copy any of them, so the reader had to navigate to the thing first and
 * find its button.
 *
 * The resolution is DERIVED, not declared per row. {@link EnterAction} already
 * says what each row points at -- that is its whole purpose -- so a row that
 * can be opened can be addressed, and every provider that populated `enter`
 * gets copy without changing a line. A row only carries {@link CopyableRow.copyUrl}
 * when its address is something the action kind genuinely cannot express.
 *
 * Kept pure and DOM-free (the origin and the session-link builder are injected)
 * so the rules are pinned by unit test rather than by driving a browser.
 *
 * The row fields it reads are structural rather than the palette's full result
 * type, so the root launcher's own row model can satisfy them without either
 * model importing the other -- the two lists are separate surfaces and this
 * layer is meant to serve both.
 */
export interface CopyableRow {
  enter?: EnterAction
  /**
   * An explicit URL to copy, for a row whose address its action kind cannot
   * express.
   *
   * An artifact row NAVIGATES to a dashboard route, but the address worth
   * handing to someone else is the deployed public URL, and only the provider
   * holding that record knows it. Always validated through `safeHttpUrl`, so a
   * `javascript:` value yields no target rather than reaching the clipboard --
   * the same guard `WebAppArtifactCard` already applies to the same field.
   */
  copyUrl?: string
}

export interface CopyDeps {
  /** `window.location.origin`. Injected to keep this module testable without a DOM. */
  origin: string
  /**
   * The session deep-link builder (`utils/shareUrl.buildShareableUrl`).
   *
   * Injected for the same reason, and so a session link has ONE definition in
   * the product rather than a second one here that drifts from it.
   */
  sessionLink: (sessionKey: string, title?: string) => string
}

/**
 * A dashboard route turned into an absolute link, or null when the route cannot
 * be trusted to stay on this origin.
 *
 * A route is concatenated onto the origin, so one beginning `//` would produce a
 * protocol-relative URL pointing at someone else's host -- a copied link that
 * silently leaves the product. Requiring a single leading slash is what keeps
 * the output an address of THIS dashboard.
 */
function routeLink(route: string, origin: string): string | null {
  if (!route.startsWith('/') || route.startsWith('//')) return null
  return origin + route
}

/**
 * The string this row would put on the clipboard, or null when the row points at
 * something with no address.
 *
 * Null is a normal answer, not a failure: an `invoke` row is a callback, and a
 * callback has no address to hand anyone. The caller reports "nothing to copy"
 * rather than copying something approximate, because a launcher that copies the
 * wrong string is worse than one that copies nothing -- the reader finds out
 * only after pasting it somewhere.
 */
export function resolveCopyTarget(row: CopyableRow, deps: CopyDeps): string | null {
  // The explicit override wins: a provider that set it knows an address this
  // resolver cannot derive.
  if (row.copyUrl) {
    return safeHttpUrl(row.copyUrl) || null
  }

  const action = row.enter
  if (!action) return null

  switch (action.kind) {
    case 'navigate':
      return routeLink(action.route, deps.origin)
    case 'open-session':
      // The session's shareable deep link, exactly as the chat surface's own
      // copy button produces it -- the action payload already carries the two
      // fields that builder needs.
      return deps.sessionLink(action.sessionKey, action.title) || null
    case 'insert-token':
      // Nothing, DELIBERATELY, though a token would be a perfectly good thing to
      // copy: the only producer of this kind feeds the palette's Skills and Prompts
      // tabs, and the palette does not claim the copy chord yet. Returning the token
      // here would be a branch no shipped surface can reach, and copying a token also
      // wants its own confirmation wording -- both belong to the change that wires
      // that surface, not to this one.
      return null
    case 'open-knowledge':
      // No address to derive: the payload carries an entry id and a title, and
      // no route to an entry exists to build one from. Inventing one here would
      // produce a link that 404s. A knowledge row that wants to be copyable
      // sets `copyUrl`.
      return null
    case 'invoke':
      // A callback has no address.
      return null
  }
}
