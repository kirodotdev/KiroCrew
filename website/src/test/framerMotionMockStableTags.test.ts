/**
 * A framer-motion mock built as `new Proxy({}, { get: (_t, tag) => make(tag) })`
 * hands React a NEW component type on every `motion.<tag>` read. Each re-render
 * then unmounts and remounts that subtree and replaces its DOM nodes, so a test
 * holding a node from before the re-render clicks or reads a detached element
 * and fails only when a re-render happens to land in between. This scan fails,
 * naming the file, when a mock's `motion` Proxy getter does not return one
 * cached component per tag.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

const SRC = join(__dirname, '..')

/**
 * Files a separate in-flight change already fixes. Tolerated in either state, so
 * whichever change merges first leaves the other green; drop an entry once its
 * file caches on main.
 */
const OWNED_ELSEWHERE = new Set(['test/ChatSidebar.resumeAll.test.tsx'])

function* walk(dir: string): Generator<string> {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) { yield* walk(p); continue }
    if (/\.tsx?$/.test(name)) yield p
  }
}

/** The text from `start` (an opening bracket) through its matching close. */
function balanced(src: string, start: number): string {
  let depth = 0
  for (let i = start; i < src.length; i++) {
    const c = src[i]
    if (c === '(' || c === '{' || c === '[') depth++
    else if (c === ')' || c === '}' || c === ']') {
      depth--
      if (depth === 0) return src.slice(start, i + 1)
    }
  }
  return src.slice(start)
}

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/**
 * Line numbers of every `motion` Proxy whose getter does not cache per tag. A
 * getter caches when it checks `.has(<its key>)` itself, or when it delegates to
 * a function defined earlier in the file that checks `.has(<its own key>)`.
 */
function uncachedMotionProxies(src: string): number[] {
  const out: number[] = []
  const re = /\bmotion\s*[:=]\s*new Proxy\(/g
  for (let m = re.exec(src); m; m = re.exec(src)) {
    const open = m.index + m[0].length - 1
    const args = balanced(src, open)
    const getter = /\bget\s*:\s*\(\s*\w+\s*,\s*(\w+)/.exec(args)
    if (!getter) continue
    const key = getter[1]
    if (new RegExp(`\\.has\\(${escape(key)}\\)`).test(args)) continue
    const delegate = new RegExp(`=>\\s*(\\w+)\\(${escape(key)}\\)`).exec(args)
    if (delegate) {
      const def = new RegExp(`\\b(?:const|let|function)\\s+${escape(delegate[1])}\\b\\s*=?\\s*\\(\\s*(\\w+)`).exec(src.slice(0, m.index))
      if (def && new RegExp(`\\.has\\(${escape(def[1])}\\)`).test(src.slice(def.index, m.index))) continue
    }
    out.push(src.slice(0, m.index).split('\n').length)
  }
  return out
}

describe('framer-motion mocks keep one component per tag', () => {
  it('flags a getter that builds a component per read, and passes the cached shapes', () => {
    const make = 'const make = (tag: string) => React.forwardRef((p, ref) => React.createElement(tag, { ...p, ref }))\n'
    expect(uncachedMotionProxies(`${make}const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })`)).toEqual([2])
    expect(uncachedMotionProxies(`${make}return { motion: new Proxy({}, { get: (_t, tag: string) => make(tag) }) }`)).toEqual([2])
    expect(uncachedMotionProxies('const motion = new Proxy({}, { get: (_t, tag: string) => React.forwardRef(() => null) })')).toEqual([1])
    expect(uncachedMotionProxies(`${make}const c = new Map()\nconst motion = new Proxy({}, { get: (_t, tag: string) => { if (!c.has(tag)) c.set(tag, make(tag)); return c.get(tag) } })`)).toEqual([])
    const cachingMake = 'const cache = new Map()\nconst make = (tag: string) => { if (!cache.has(tag)) cache.set(tag, 1); return cache.get(tag) }\n'
    expect(uncachedMotionProxies(`${cachingMake}const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })`)).toEqual([])
  })

  it('every framer-motion mock under src caches its motion components', () => {
    const offenders: string[] = []
    for (const file of walk(SRC)) {
      if (file === __filename) continue // its fixtures are uncached on purpose
      const src = readFileSync(file, 'utf8')
      if (!src.includes('framer-motion')) continue
      const rel = relative(SRC, file).split('\\').join('/')
      if (OWNED_ELSEWHERE.has(rel)) continue
      for (const line of uncachedMotionProxies(src)) offenders.push(`src/${rel}:${line}`)
    }
    expect(offenders, 'cache one component per tag in these motion Proxy getters').toEqual([])
  })
})
