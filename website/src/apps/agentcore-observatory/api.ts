/**
 * AgentCore Observatory API client — a thin same-origin fetch wrapper.
 *
 * Mirrors the aws-control client: it prefers the response body's machine
 * readable `code` over the untranslated English `error` prose, so the UI has a
 * stable token to localise. Every AWS-facing endpoint is a read; the one write
 * saves a profile NAME and a region to the app's own data dir.
 *
 * `getCatalog` makes no AWS call at all — it answers from the backend's
 * in-process resource table. That is what keeps first paint instant with 27
 * resource types: a type is fetched only when its rail item is opened.
 */

const BASE = '/api/apps/agentcore-observatory'

/** Error carrying the backend's machine-readable `code` (e.g. `app_disabled`). */
export class ObservatoryError extends Error {
  readonly status: number
  constructor(code: string, status: number) {
    super(code)
    this.name = 'ObservatoryError'
    this.status = status
  }
}

/** The saved connection. `configured` is false until a region is set. */
export interface ObservatoryConfig {
  profile: string
  region: string
  configured: boolean
}

/**
 * One paginated read.
 *
 * `ok: true` with an empty `items` is an authorized account with nothing
 * deployed — a distinct state from `ok: false`, and the UI must not collapse
 * them. `truncated` means `items` is a partial page and must never be presented
 * as a total.
 */
export interface ListResult {
  ok: boolean
  items: Record<string, unknown>[]
  error: string
  denied: boolean
  truncated: boolean
}

/** One `get-*` read: a single object rather than a list. */
export interface ObjectResult {
  ok: boolean
  item: Record<string, unknown>
  error: string
  denied: boolean
}

/** A child type and the flags its query needs, as the catalog declares them. */
export interface ChildType {
  id: string
  /** CLI flags, e.g. `['--gateway-identifier']`. */
  parentParams: string[]
  /** Parent response fields supplying them, positionally paired. */
  parentFields: string[]
}

export interface RootType {
  id: string
  /** False for a get-only singleton such as `token-vault`. */
  listable: boolean
  /** Response field holding a row's own identifier; '' for a singleton. */
  idField: string
  children: ChildType[]
}

export interface CatalogGroup {
  id: string
  types: RootType[]
}

export interface Catalog {
  config: ObservatoryConfig
  groups: CatalogGroup[]
}

/** A listable type answers with `list`; a singleton answers with `singleton`. */
export interface ResourceResponse {
  type: string
  list?: ListResult
  singleton?: ObjectResult
}

export interface DetailResponse {
  type: string
  detail: ObjectResult
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!res.ok) {
    // A non-2xx body is expected to carry `code`; fall back to the status when a
    // proxy or crash produced a body this contract does not cover.
    let code = `http_${res.status}`
    try {
      const body = (await res.json()) as { code?: string }
      if (body?.code) code = body.code
    } catch {
      // Body was not JSON — the status-derived code above is the best available.
    }
    throw new ObservatoryError(code, res.status)
  }
  return (await res.json()) as T
}

/** Build `?flag=value` pairs from catalog flags (`--x` becomes `x`). */
function parentQuery(parentIds: Record<string, string>): string {
  const pairs = Object.entries(parentIds)
    .filter(([, value]) => value)
    .map(([flag, value]) => `${encodeURIComponent(flag.replace(/^--/, ''))}=${encodeURIComponent(value)}`)
  return pairs.length ? `?${pairs.join('&')}` : ''
}

export const observatoryApi = {
  getConfig: () => request<ObservatoryConfig>('/config'),

  saveConfig: (profile: string, region: string) =>
    request<ObservatoryConfig>('/config', {
      method: 'PUT',
      body: JSON.stringify({ profile, region }),
    }),

  getProfiles: () => request<{ profiles: { name: string; region: string }[] }>('/profiles'),

  getCatalog: () => request<Catalog>('/catalog'),

  getResource: (typeId: string, parentIds: Record<string, string> = {}) =>
    request<ResourceResponse>(
      `/resource/${encodeURIComponent(typeId)}${parentQuery(parentIds)}`,
    ),

  getDetail: (typeId: string, idArgs: Record<string, string>) =>
    request<DetailResponse>(
      `/resource/${encodeURIComponent(typeId)}/detail${parentQuery(idArgs)}`,
    ),
}
