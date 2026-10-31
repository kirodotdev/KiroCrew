/**
 * In-page probe for the render benchmark (`scripts/bench-render.mjs`).
 *
 * `renderProbe` is passed to `addInitScript`, so it runs in the page before the
 * bundle and must stay self-contained: no imports, no closure over module scope.
 * It installs a stub React DevTools hook and the two store seams the profiling
 * build exposes (`window.__kcPerfMw`, `window.__kcSelWrap`, see
 * `vite.profiling.config.mjs`), and counts while `window.__perf.counting` is true:
 *
 *   commits         onCommitFiberRoot calls
 *   renders         function/class/memo/forwardRef fibers that performed work
 *   comp[name]      per-component renders, mounts, selfBaseDuration, actualDuration
 *   dispatches      store actions through the middleware, per action type
 *   selectorCalls   useAppSelector evaluations; each selector gets one cached
 *                   wrapper, so react-redux's selector memo still holds
 *   why[name]       for the components named in `why`, the cause of each render
 *   roots           per commit, the fibers the update landed on, with their cause
 *
 * The commit walk visits only fibers React worked on: a subtree whose `child`
 * equals its alternate's was bailed out wholesale and is skipped, unless the
 * fiber itself has PerformedWork (flag 1).
 *
 * Causes, per rendered fiber:
 *   mount                         no alternate
 *   useState#i / uSES#i [sel]     a hook queue whose state changed; uSES is a
 *                                 useSyncExternalStore, and for react-redux 9's
 *                                 useSelector the label is the selector source,
 *                                 read from the useCallback deps slot that sits
 *                                 up to 4 hooks earlier
 *   deferred?#i                   a queue-less hook whose raw value changed
 *                                 (useDeferredValue keeps its value that way)
 *   ctx:Name                      a context dependency whose value changed
 *   props:k1,k2                   props that are not Object.is equal
 *   parent(same props)<-Name      the nearest rendered ancestor re-rendered it
 * Every changed value carries a mark: `~` deep-equal (identity churn only), `!`
 * a real change, `ƒ` a new function with the same source, `ƒ!` different
 * source, `?` the 20k-node deep-compare budget ran out (usually a React
 * element tree).
 */
export function renderProbe({ why = [] } = {}) {
  const P = (window.__perf = {
    counting: false, commits: 0, renders: 0, comp: {}, disp: {}, dispatches: 0, sel: 0,
    why: {}, roots: {}, setState: {}, long: [], err: null,
  })
  const WHY = new Set(why)
  const NAMES = new WeakMap()
  const nameOf = (f) => {
    const t = f.type
    if (!t || typeof t === 'string') return null
    let n = NAMES.get(t)
    if (n) return n
    n = t.displayName || t.name
    if (!n && t.render) n = t.render.displayName || t.render.name || 'ForwardRef'
    if (!n && t.type) n = t.type.displayName || t.type.name || 'Memo'
    n = n || 'Anonymous'
    NAMES.set(t, n)
    return n
  }
  // FunctionComponent, ClassComponent, ForwardRef, MemoComponent, SimpleMemoComponent.
  const COMP_TAGS = new Set([0, 1, 11, 14, 15])
  const CTX_NAMES = new Map()
  const ctxName = (c) => c.displayName || CTX_NAMES.get(c) || 'ctx?'
  const isEffect = (v) => v !== null && typeof v === 'object' && 'create' in v && 'deps' in v && 'tag' in v
  const bump = (o, k, by = 1) => { o[k] = (o[k] || 0) + by }
  const fnLabel = (fn) => {
    const inner = fn.__kcSel || fn
    if (inner.__kcLabel) return inner.__kcLabel
    const src = Function.prototype.toString.call(inner).replace(/\s+/g, ' ')
    const l = inner.name || (src.length > 110 ? src.slice(0, 110) + '…' : src)
    try { inner.__kcLabel = l } catch { /* frozen */ }
    return l
  }
  // Bounded structural equality. React elements compare type, key and props.
  const deq = (a, b, d, bud) => {
    if (Object.is(a, b)) return true
    if (--bud.n < 0 || d > 8) throw new Error('budget')
    if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) return false
    if (Object.getPrototypeOf(a) !== Object.getPrototypeOf(b)) return false
    if (Array.isArray(a)) {
      if (a.length !== b.length) return false
      for (let i = 0; i < a.length; i++) if (!deq(a[i], b[i], d + 1, bud)) return false
      return true
    }
    if (a instanceof Map) {
      if (a.size !== b.size) return false
      for (const [k, v] of a) if (!b.has(k) || !deq(v, b.get(k), d + 1, bud)) return false
      return true
    }
    if (a instanceof Set) {
      if (a.size !== b.size) return false
      for (const k of a) if (!b.has(k)) return false
      return true
    }
    if (typeof Node !== 'undefined' && (a instanceof Node || b instanceof Node)) return false
    const el = a.$$typeof !== undefined
    const keys = (o) => Object.keys(o).filter((k) => !(el && (k === '_owner' || k === '_store')))
    const ka = keys(a), kb = keys(b)
    if (ka.length !== kb.length) return false
    for (const k of ka) if (!Object.prototype.hasOwnProperty.call(b, k) || !deq(a[k], b[k], d + 1, bud)) return false
    return true
  }
  const mark = (a, b) => {
    if (typeof a === 'function' && typeof b === 'function') {
      return Function.prototype.toString.call(a) === Function.prototype.toString.call(b) ? 'ƒ' : 'ƒ!'
    }
    try { return deq(a, b, 0, { n: 20000 }) ? '~' : '!' } catch { return '?' }
  }
  const stateParts = (alt, f, parts) => {
    if (f.tag === 1) { if (alt.memoizedState !== f.memoizedState) parts.push('state:class'); return }
    let a = alt.memoizedState, b = f.memoizedState
    const prev = []
    for (let i = 0; a && b && i < 6000; i++) {
      const q = b.queue
      const changed = !isEffect(b.memoizedState) && !Object.is(a.memoizedState, b.memoizedState)
      if (q != null && changed) {
        const uses = typeof q === 'object' && 'getSnapshot' in q
        let label = ''
        if (uses) {
          let cand = null
          for (let j = prev.length - 1; j >= 0 && j >= prev.length - 4; j--) {
            const ms = prev[j]
            if (Array.isArray(ms) && ms.length === 2 && Array.isArray(ms[1]) && ms[1].length === 1 && typeof ms[1][0] === 'function') {
              if (ms[1][0].__kcSel) { cand = ms[1][0]; break }
              if (!cand) cand = ms[1][0]
            }
          }
          if (cand) label = fnLabel(cand)
          else if (typeof q.getSnapshot === 'function') label = `snap:${fnLabel(q.getSnapshot)}`
        }
        parts.push(`${uses ? 'uSES' : 'useState'}#${i}${label ? ` [${label}]` : ''}${mark(a.memoizedState, b.memoizedState)}`)
      } else if (q == null && changed) {
        // useMemo/useCallback keep [value, deps] and change only because an
        // input did; a ref keeps {current}. Neither is a cause on its own.
        const ms = b.memoizedState
        const memoTuple = Array.isArray(ms) && ms.length === 2 && (Array.isArray(ms[1]) || ms[1] === null)
        const ref = ms && typeof ms === 'object' && 'current' in ms && Object.keys(ms).length === 1
        if (!memoTuple && !ref) parts.push(`deferred?#${i}${mark(a.memoizedState, ms)}`)
      }
      prev.push(b.memoizedState)
      if (prev.length > 6) prev.shift()
      a = a.next; b = b.next
    }
  }
  const ctxParts = (alt, f, parts) => {
    const fd = f.dependencies
    if (!fd || !fd.firstContext) return
    const old = new Map()
    for (let c = alt.dependencies && alt.dependencies.firstContext; c; c = c.next) old.set(c.context, c.memoizedValue)
    for (let c = fd.firstContext; c; c = c.next) {
      if (old.has(c.context) && !Object.is(old.get(c.context), c.memoizedValue)) {
        parts.push(`ctx:${ctxName(c.context)}${mark(old.get(c.context), c.memoizedValue)}`)
      }
    }
  }
  const classify = (f, parentName) => {
    const alt = f.alternate
    if (!alt) return ['mount']
    const parts = []
    stateParts(alt, f, parts)
    ctxParts(alt, f, parts)
    const A = alt.memoizedProps, B = f.memoizedProps
    if (A !== B) {
      const ch = []
      for (const k of new Set([...Object.keys(A || {}), ...Object.keys(B || {})])) {
        if (!Object.is(A && A[k], B && B[k])) ch.push(k + mark(A && A[k], B && B[k]))
      }
      if (ch.length) parts.push(`props:${ch.sort().join(',')}`)
      else if (!parts.length) parts.push(`parent(same props)<-${parentName}`)
    } else if (!parts.length) parts.push('none(force/bail-miss)')
    return parts
  }
  const walk = (root) => {
    const commitRoots = new Set()
    const stack = [[root.current, 'root', 'root']]
    while (stack.length) {
      const [f, parentName, ownerName] = stack.pop()
      let rendered = false
      let nm = null
      if (f.tag === 10 && f.type && f.type._context && !CTX_NAMES.has(f.type._context)) {
        CTX_NAMES.set(f.type._context, `${ownerName}.Provider`)
      }
      if (COMP_TAGS.has(f.tag)) {
        nm = nameOf(f)
        const mounted = f.alternate === null
        if (nm && (mounted || (f.flags & 1) === 1)) {
          rendered = true
          P.renders++
          const c = P.comp[nm] || (P.comp[nm] = { n: 0, mounts: 0, selfMs: 0, actualMs: 0 })
          c.n++
          if (mounted) c.mounts++
          c.selfMs += f.selfBaseDuration || 0
          c.actualMs += f.actualDuration || 0
          let parts = null
          if (parentName === 'root') {
            // No ancestor rendered in this commit, so the update landed here.
            parts = classify(f, parentName)
            commitRoots.add(`${nm}:${parts.map((p) => p.replace(/^props:.*/, 'props').replace(/ \[(.{0,48}).*?\]/, ' [$1]')).join('+')}`)
          }
          if (WHY.has(nm)) {
            if (!parts) parts = classify(f, parentName)
            const w = P.why[nm] || (P.why[nm] = { n: 0, causes: {}, parents: {} })
            w.n++
            bump(w.causes, parts.join(' + '))
            bump(w.parents, parentName)
          }
        }
      }
      const alt = f.alternate
      const skipped = alt !== null && f.child === alt.child && !rendered
      if (f.child && !skipped) {
        const pn = rendered ? nm : parentName
        const on = nm || ownerName
        for (let ch = f.child; ch; ch = ch.sibling) stack.push([ch, pn, on])
      }
    }
    bump(P.roots, [...commitRoots].sort().join(' ') || '(no root)')
  }
  let phase = null
  const hooks = {
    markComponentRenderStarted() { phase = 'render' },
    markComponentRenderStopped() { phase = null },
    markLayoutEffectsStarted() { phase = 'layout' },
    markLayoutEffectsStopped() { phase = null },
    markPassiveEffectsStarted() { phase = 'passive' },
    markPassiveEffectsStopped() { phase = null },
    markStateUpdateScheduled(fiber) {
      if (P.counting) bump(P.setState, `${nameOf(fiber) || `tag${fiber.tag}`} [${phase || 'task'}]`)
    },
  }
  window.__REACT_DEVTOOLS_GLOBAL_HOOK__ = {
    renderers: new Map(),
    supportsFiber: true,
    inject(r) {
      const id = this.renderers.size + 1
      this.renderers.set(id, r)
      // Only react-dom/profiling passes these; the prod build has no such hook.
      if (typeof r.injectProfilingHooks === 'function') { r.injectProfilingHooks(hooks); P.profiling = true }
      return id
    },
    onScheduleFiberRoot() {},
    onCommitFiberRoot(_id, root) {
      if (!P.counting) return
      P.commits++
      try { walk(root) } catch (e) { P.err = String(e) }
    },
    onCommitFiberUnmount() {},
    onPostCommitFiberRoot() {},
    checkDCE() {},
  }
  window.__kcPerfMw = () => (next) => (action) => {
    if (!P.counting) return next(action)
    let type = action && action.type ? action.type : 'thunk'
    if (type === 'chat/sseChatMessage' && action.payload) type += `:${action.payload.role}`
    P.dispatches++
    bump(P.disp, type)
    return next(action)
  }
  // One wrapper per selector: the profiling build calls this on every render,
  // and react-redux 9 keys its useCallback and selector memo on the selector's
  // identity, so a fresh wrapper each call would defeat that cache and inflate
  // selectorCalls.
  const SEL_WRAPS = new WeakMap()
  window.__kcSelWrap = (sel) => {
    let w = SEL_WRAPS.get(sel)
    if (w) return w
    w = function (s) { if (P.counting) P.sel++; return sel(s) }
    w.__kcSel = sel
    SEL_WRAPS.set(sel, w)
    return w
  }
  try {
    new PerformanceObserver((list) => { if (P.counting) for (const e of list.getEntries()) P.long.push(e.duration) })
      .observe({ type: 'longtask', buffered: false })
  } catch { /* longtask unsupported */ }
}
