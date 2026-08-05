/**
 * Blessed API transport — the shared, session-key-authenticated request helpers.
 *
 * `api/client.ts` defines the low-level helpers (`get`/`post`/`put`/`del`/
 * `patch` + the `j`/`jNullable` response parsers) and installs them here via
 * `installApiTransport`. A downstream edition that ships its own backend routes
 * imports `apiTransport` and builds its OWN fully-typed API module on top of it,
 * instead of forking `api/client.ts` (the whole-file-shadow erosion the seams
 * exist to eliminate) or writing methods on raw `fetch`.
 *
 * Why an exported transport and NOT a `registerApiExtensions()` registry: unlike
 * the other seams (builtin routes/icons/theme branding/top-bar widgets/panel
 * shortcuts/theme options), the core never CONSUMES edition API methods — they
 * are written and read only by the edition. A registry the core never reads adds
 * public, stringly-typed (`unknown`-cast) seam surface for zero composition
 * benefit. Exporting
 * the blessed transport gives an edition the one thing it actually needs —
 * `X-Session-Key` + the auth-recovery/`ApiError` pipeline by construction — while
 * letting it keep full static types in its own module and adding no new
 * *registry* contract.
 *
 * ## `ApiTransport` IS a small, intentionally-FROZEN downstream contract
 *
 * A separately-built edition compiles against the seven signatures below and the
 * `j`/`jNullable` semantics (the session-expiry / auth-banner / `ApiError`
 * recovery pipeline). Nothing analogous to the backend's `CONTRACT_VERSION`
 * guards this frontend seam, so **treat these signatures as frozen** — like the
 * backend's "CONTRACT_VERSION pinned at 1 pre-launch": a change to a request
 * helper's shape or to `j`'s error behavior is **edition-breaking**, not a free
 * internal refactor (the stock build stays green — the seam is inert — and the
 * breakage surfaces only at runtime in the out-of-repo edition, on auth-adjacent
 * surface since the transport carries `X-Session-Key`). Evolve additively.
 *
 * Trust boundary: this transport carries the session-key header; it is for the
 * edition composition root, NOT for app/plugin-contributed frontend code.
 */

/**
 * The session-key-authenticated request helpers, identical to those the core
 * `api` methods use. `get`/`post`/`put`/`del`/`patch` issue the request with the
 * `X-Session-Key` header; `j`/`jNullable` parse the `Response` and run the
 * session-expiry / auth-banner / `ApiError` recovery pipeline.
 *
 * Intentionally frozen — see the module header. Add methods, never change these.
 */
export interface ApiTransport {
  get: (url: string) => Promise<Response>
  post: (url: string, body?: object) => Promise<Response>
  put: (url: string, body: object) => Promise<Response>
  del: (url: string, body?: object) => Promise<Response>
  patch: (url: string, body: object) => Promise<Response>
  j: (r: Response) => Promise<unknown>
  jNullable: (r: Response) => Promise<unknown>
}

// Installed once by client.ts (which owns the helpers) at its module load.
let _installed: ApiTransport | null = null

/**
 * Install the blessed transport. Called once by `api/client.ts`. Not part of the
 * downstream-facing contract — editions read `apiTransport`, they never install.
 */
export function installApiTransport(transport: ApiTransport): void {
  _installed = transport
}

function _resolve(): ApiTransport {
  if (!_installed) {
    // client.ts installs at its own module load. Any real request happens well
    // after app startup, by which point client.ts has been imported (it is
    // pulled in transitively by the store/providers/App). This guards only the
    // impossible-in-practice case of a call before that.
    throw new Error('apiTransport used before api/client installed it')
  }
  return _installed
}

/**
 * The blessed transport. Each method is a STABLE wrapper that resolves the
 * installed helper at CALL time — so an edition may `import { apiTransport }`
 * and even destructure it at module-init (before `client.ts` installs the real
 * helpers) without throwing; the wrappers just forward once actually invoked,
 * which is always after startup. This deliberately avoids any import-ordering
 * coupling with `extensions.ts` (imported first in `main.tsx`).
 */
export const apiTransport: ApiTransport = {
  get: (url) => _resolve().get(url),
  post: (url, body) => _resolve().post(url, body),
  put: (url, body) => _resolve().put(url, body),
  del: (url, body) => _resolve().del(url, body),
  patch: (url, body) => _resolve().patch(url, body),
  j: (r) => _resolve().j(r),
  jNullable: (r) => _resolve().jNullable(r),
}
