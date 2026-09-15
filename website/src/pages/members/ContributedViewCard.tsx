/**
 * Generic rendering for a contributed `<app>/<key>` projection (contribution
 * protocol §7).
 *
 * The host renders; the contributor does not. No app JavaScript reaches this
 * component -- it receives a JSON value and, optionally, a small declarative
 * schema naming one of five body shapes plus dotted `path` selectors. Anything
 * the schema cannot describe falls back to a compact key-value dump, so a view
 * published with no schema at all still renders something a person can read
 * rather than nothing.
 *
 * Every string that reaches the DOM here goes through `scalarText`, which
 * stringifies and TRUNCATES. The value is third-party data: a 200 KB string in
 * one cell would make the drawer unusable, and React's own escaping handles the
 * injection half.
 */
import type { ContributedView, ProjectionSchema } from '../../state/memberProjectionTypes'

/** Longest scalar rendered in a card. Past this the text is elided. */
const MAX_SCALAR_CHARS = 240
/** Most rows a list or table body renders before it stops. */
const MAX_ROWS = 20
/** Most columns a table renders. */
const MAX_COLUMNS = 6

/** A readable, bounded string for any JSON scalar or small container. */
export function scalarText(value: unknown): string {
  if (value === null || value === undefined) return ''
  let text: string
  if (typeof value === 'string') text = value
  else if (typeof value === 'number' || typeof value === 'boolean') text = String(value)
  else {
    try {
      text = JSON.stringify(value) ?? ''
    } catch {
      text = ''
    }
  }
  return text.length > MAX_SCALAR_CHARS ? `${text.slice(0, MAX_SCALAR_CHARS)}…` : text
}

/**
 * Read a dotted path out of a value. `''` and a path that misses return
 * undefined; an array index is a numeric segment (`items.0.name`).
 */
export function resolvePath(value: unknown, path: string): unknown {
  if (!path) return value
  let cursor: unknown = value
  for (const segment of path.split('.')) {
    if (cursor === null || cursor === undefined) return undefined
    if (Array.isArray(cursor)) {
      const index = Number(segment)
      if (!Number.isInteger(index) || index < 0 || index >= cursor.length) return undefined
      cursor = cursor[index]
      continue
    }
    if (typeof cursor !== 'object') return undefined
    cursor = (cursor as Record<string, unknown>)[segment]
  }
  return cursor
}

/** The array a `list` / `table` body renders: the first selector, or the value. */
function rowsOf(value: unknown, schema: ProjectionSchema | undefined): unknown[] {
  const picked = schema?.path?.length ? resolvePath(value, schema.path[0]) : value
  if (Array.isArray(picked)) return picked.slice(0, MAX_ROWS)
  if (Array.isArray(value)) return value.slice(0, MAX_ROWS)
  return []
}

/** The `[label, value]` pairs a keyvalue body renders. */
function pairsOf(value: unknown, schema: ProjectionSchema | undefined): Array<[string, unknown]> {
  if (schema?.path?.length) {
    return schema.path.map((p) => [p, resolvePath(value, p)] as [string, unknown])
  }
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return Object.entries(value as Record<string, unknown>).slice(0, MAX_ROWS)
  }
  return [['value', value]]
}

/** The scalar a badge / text body renders: the first selector, or the value. */
function scalarOf(value: unknown, schema: ProjectionSchema | undefined): unknown {
  if (schema?.path?.length) {
    const picked = resolvePath(value, schema.path[0])
    if (picked !== undefined) return picked
  }
  return value
}

function ContributedBody({ view }: { view: ContributedView }): JSX.Element {
  const { value, schema } = view
  const kind = schema?.kind ?? 'keyvalue'

  if (kind === 'badge') {
    const text = scalarText(scalarOf(value, schema))
    return (
      <span
        className="inline-flex items-center rounded-full bg-bg-hover px-2 py-0.5 text-[11px] text-text"
        data-testid="contributed-badge"
      >
        {text || '—'}
      </span>
    )
  }

  if (kind === 'text') {
    return (
      <p className="text-[12px] leading-relaxed text-text break-words" data-testid="contributed-text">
        {scalarText(scalarOf(value, schema)) || '—'}
      </p>
    )
  }

  if (kind === 'list') {
    const rows = rowsOf(value, schema)
    if (rows.length === 0) return <EmptyBody />
    return (
      <ul className="space-y-0.5" data-testid="contributed-list">
        {rows.map((row, i) => (
          <li key={i} className="text-[12px] text-text break-words">
            {scalarText(row) || '—'}
          </li>
        ))}
      </ul>
    )
  }

  if (kind === 'table') {
    const rows = rowsOf(value, schema)
    if (rows.length === 0) return <EmptyBody />
    // Columns come from the schema's selectors after the first (which picked the
    // array), else from the union of the rows' own keys. Bounded either way: a
    // contributor whose rows carry forty fields must not widen the drawer.
    const declared = (schema?.path ?? []).slice(1)
    const columns = declared.length
      ? declared.slice(0, MAX_COLUMNS)
      : Array.from(
          rows.reduce<Set<string>>((acc, row) => {
            if (row && typeof row === 'object' && !Array.isArray(row)) {
              for (const k of Object.keys(row as Record<string, unknown>)) acc.add(k)
            }
            return acc
          }, new Set<string>()),
        ).slice(0, MAX_COLUMNS)
    if (columns.length === 0) {
      // Rows of scalars: a table with no columns is a list.
      return (
        <ul className="space-y-0.5" data-testid="contributed-table">
          {rows.map((row, i) => (
            <li key={i} className="text-[12px] text-text break-words">
              {scalarText(row) || '—'}
            </li>
          ))}
        </ul>
      )
    }
    return (
      <div className="overflow-x-auto">
        <table className="w-full text-[11px]" data-testid="contributed-table">
          <thead>
            <tr>
              {columns.map((c) => (
                <th key={c} className="pr-3 pb-1 text-left font-semibold text-muted">
                  {scalarText(c)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i}>
                {columns.map((c) => (
                  <td key={c} className="pr-3 py-0.5 align-top text-text break-words">
                    {scalarText(resolvePath(row, c)) || '—'}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  const pairs = pairsOf(value, schema)
  if (pairs.length === 0) return <EmptyBody />
  return (
    <dl className="space-y-0.5" data-testid="contributed-keyvalue">
      {pairs.map(([label, cell]) => (
        <div key={label} className="flex gap-2 text-[11px]">
          <dt className="shrink-0 text-muted">{scalarText(label)}</dt>
          <dd className="min-w-0 flex-1 text-text break-words">{scalarText(cell) || '—'}</dd>
        </div>
      ))}
    </dl>
  )
}

function EmptyBody(): JSX.Element {
  return (
    <p className="text-[11px] text-muted" data-testid="contributed-empty">
      Nothing to show yet
    </p>
  )
}

/**
 * One contributed view as a drawer card. The title is the schema's `title` when
 * it published one, else the key itself -- which is `<app>/<name>`, so an
 * untitled card still says which app it came from.
 */
export function ContributedViewCard({ view }: { view: ContributedView }): JSX.Element {
  const app = view.key.split('/')[0]
  const title = view.schema?.title || view.key
  return (
    <div className="mb-4" data-testid={`contributed-card-${view.key}`}>
      <div className="text-[11px] font-semibold tracking-wide text-muted mb-1.5 flex items-center gap-1.5">
        <span className="flex-1 truncate" title={view.key}>
          {scalarText(title)}
        </span>
        <span className="shrink-0 rounded bg-bg-hover px-1.5 py-0.5 text-[10px] font-normal text-muted">
          {scalarText(app)}
        </span>
      </div>
      <ContributedBody view={view} />
    </div>
  )
}

/** Every contributed view for the open member, or nothing when there are none. */
export function ContributedViews({
  views,
}: {
  views: readonly ContributedView[]
}): JSX.Element | null {
  if (views.length === 0) return null
  return (
    <div data-testid="member-contributed-views">
      {views.map((view) => (
        <ContributedViewCard key={view.key} view={view} />
      ))}
    </div>
  )
}
