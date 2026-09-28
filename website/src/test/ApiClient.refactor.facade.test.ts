/**
 * The `api` singleton's shape, which ~700 test files and every page rely on.
 *
 * Test suites replace members with `vi.spyOn(api, name)` and rebuild the object
 * with `{ ...mod.api, name: vi.fn() }` inside `vi.mock` factories. Both only
 * work while every member is an own, enumerable, writable DATA property of a
 * plain object: a getter, a frozen segment, or a class instance would break
 * them silently. `teams` and `appearances` are the two nested namespaces.
 */
import { describe, it, expect, vi } from 'vitest'
import * as client from '../api/client'
import { api } from '../api/client'
import { installApiTransport } from '../api/apiTransport'

// Observe the install without replacing it: the real transport stays installed.
vi.mock('../api/apiTransport', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../api/apiTransport')>()
  return { ...mod, installApiTransport: vi.fn(mod.installApiTransport) }
})

const members = Object.keys(api) as (keyof typeof api)[]
const NESTED: Record<string, string[]> = {
  teams: ['list', 'create', 'update', 'remove'],
  appearances: ['list', 'detail', 'importBundle', 'remove'],
}

describe('api singleton shape', () => {
  it('is one plain object, the same one on every import', async () => {
    expect(Object.getPrototypeOf(api)).toBe(Object.prototype)
    const again = await import('../api/client')
    expect(again.api).toBe(api)
    expect(client.api).toBe(api)
  })

  it('exposes every member as an own, enumerable, writable data property', () => {
    expect(members.length).toBeGreaterThan(600)
    for (const name of members) {
      const d = Object.getOwnPropertyDescriptor(api, name)
      expect(d, name).toBeDefined()
      expect('value' in d!, `${name} is an accessor`).toBe(true)
      expect(d!.enumerable && d!.writable && d!.configurable, name).toBe(true)
    }
  })

  it('holds functions, except the two nested namespaces', () => {
    const nonFunctions = members.filter((name) => typeof api[name] !== 'function')
    expect(nonFunctions.sort()).toEqual(Object.keys(NESTED).sort())
    for (const [ns, keys] of Object.entries(NESTED)) {
      const obj = api[ns as keyof typeof api] as unknown as Record<string, unknown>
      expect(Object.getPrototypeOf(obj)).toBe(Object.prototype)
      expect(Object.keys(obj)).toEqual(keys)
      for (const k of keys) expect(typeof obj[k], `${ns}.${k}`).toBe('function')
    }
  })

  it('lets every member be spied on and restored', () => {
    for (const name of members) {
      if (typeof api[name] !== 'function') continue
      const original = api[name]
      const spy = vi.spyOn(api, name as never).mockImplementation((() => 'spied') as never)
      expect((api[name] as unknown as () => unknown)()).toBe('spied')
      spy.mockRestore()
      expect(api[name]).toBe(original)
    }
  })

  it('survives the spread a partial vi.mock factory performs', () => {
    const copy = { ...api, status: vi.fn() }
    expect(Object.keys(copy)).toEqual(members)
    for (const name of members) if (name !== 'status') expect(copy[name]).toBe(api[name])
  })
})

describe('the blessed transport', () => {
  it('is installed once, at load, with exactly the seven helpers', () => {
    const install = vi.mocked(installApiTransport)
    expect(install).toHaveBeenCalledTimes(1)
    const [installed] = install.mock.calls[0]
    expect(Object.keys(installed)).toEqual(['get', 'post', 'put', 'del', 'patch', 'j', 'jNullable'])
    for (const helper of Object.values(installed)) expect(typeof helper).toBe('function')
  })
})

describe('api assembly from the domain endpoint modules', () => {
  // `api` is built by spreading every domain module's segments in order. A key
  // two segments both define would silently resolve to the LATER spread -- the
  // one failure a single object literal could not have (TypeScript rejects a
  // duplicate key there). So each module is instantiated here on a stub
  // transport and its segments are checked against the assembled object.
  type Factory = (t: unknown) => Record<string, Record<string, unknown>>
  const modules = import.meta.glob<Record<string, unknown>>('../api/client/*.ts', { eager: true })
  const factories = Object.entries(modules).flatMap(([file, mod]) =>
    Object.entries(mod)
      .filter(([name, value]) => /^create[A-Z]\w*Endpoints$/.test(name) && typeof value === 'function')
      .map(([name, value]) => ({ file, name, create: value as Factory })))
  // Defined in the facade itself; each has a pin comment saying why.
  const INLINE = ['wakatimeExportDownload', 'deleteLesson', 'browseRemoteArtifacts', 'saveDecisionsConsent']

  it('finds one endpoint factory per domain module', () => {
    const withFactory = new Set(factories.map((f) => f.file))
    const domainFiles = Object.keys(modules).filter((f) => !f.endsWith('/transport.ts'))
    expect([...withFactory].sort()).toEqual(domainFiles.sort())
    expect(factories.length).toBe(domainFiles.length)
  })

  it('gives every api member exactly one owner', () => {
    const owner = new Map<string, string>()
    for (const { file, create } of factories) {
      for (const [label, segment] of Object.entries(create({}))) {
        for (const key of Object.keys(segment)) {
          expect(owner.get(key), `${key} defined in ${owner.get(key)} and ${file}#${label}`).toBeUndefined()
          owner.set(key, `${file}#${label}`)
          expect(typeof api[key as keyof typeof api], key).toBe(typeof segment[key])
        }
      }
    }
    const unowned = members.filter((name) => !owner.has(name))
    expect(unowned.sort()).toEqual([...INLINE].sort())
    expect(owner.size + INLINE.length).toBe(members.length)
  })
})
