/**
 * The dashboard decides an approval only through an owner-bound target.
 *
 * An approval id is the caller's and recurs, and a chat runner's request id can
 * collide with a coordinator one, so a decide by bare id can resolve a request
 * the control never showed (types/approvalTarget.ts). `api.decideApproval` is
 * the client's only decide. This guard fails if a bare-id decide comes back:
 * a `resolveApproval` method or call anywhere in the client, or a hand-built
 * POST to `/api/approvals/<id>/...` outside the one decide that binds it.
 * Apps under `src/apps/` own their gateway calls and are not scanned.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'

const SRC = join(__dirname, '..')
const DECIDE_MODULE = 'api/client/approvals.ts'

function sources(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    const rel = relative(SRC, full).split('\\').join('/')
    if (statSync(full).isDirectory()) {
      if (rel === 'test' || rel === 'apps' || entry === '__tests__') continue
      sources(full, out)
    } else if (/\.(ts|tsx)$/.test(entry) && !/\.(test|spec)\.tsx?$/.test(entry)) {
      out.push(full)
    }
  }
  return out
}

/** Comment text is dropped so prose that names an endpoint is not a call. */
function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"`\\])\/\/.*$/gm, '$1')
}

const BARE_ID_CALL = /\bresolveApproval\b\s*[:(]/
const BARE_ID_ROUTE = /['"`]\/api\/approvals\/['"`]?\s*\+|`\/api\/approvals\/\$\{/

function offenders(pattern: RegExp, skip: (rel: string) => boolean = () => false): string[] {
  return sources(SRC)
    .map(file => [relative(SRC, file).split('\\').join('/'), code(readFileSync(file, 'utf8'))] as const)
    .filter(([rel, text]) => !skip(rel) && pattern.test(text))
    .map(([rel]) => rel)
}

describe('no dashboard path decides an approval by bare id', () => {
  it('has no resolveApproval method or call', () => {
    expect(offenders(BARE_ID_CALL)).toEqual([])
  })

  it('builds a decide URL only in the owner-bound decide', () => {
    expect(offenders(BARE_ID_ROUTE, rel => rel === DECIDE_MODULE)).toEqual([])
  })

  it('catches the shapes it is written for', () => {
    expect(BARE_ID_CALL.test(code('await api.resolveApproval(id, action)'))).toBe(true)
    expect(BARE_ID_CALL.test(code('resolveApproval: (id, action) => post(x)'))).toBe(true)
    expect(BARE_ID_CALL.test(code('dispatch(resolveApprovalRow({ target }))'))).toBe(false)
    expect(BARE_ID_CALL.test(code('// the old api.resolveApproval(id) path'))).toBe(false)
    expect(BARE_ID_ROUTE.test(code("post('/api/approvals/' + id + '/approve', {})"))).toBe(true)
    expect(BARE_ID_ROUTE.test(code('fetch(`/api/approvals/${id}/approve`)'))).toBe(true)
    expect(BARE_ID_ROUTE.test(code("fetch('/api/approvals')"))).toBe(false)
  })
})
