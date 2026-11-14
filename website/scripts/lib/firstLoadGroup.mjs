// The codeSplitting groups that bundle the dashboard's first load into a few
// chunks instead of several hundred. See the `firstLoadGroups(...)` call in
// website/vite.config.ts for why they exist.
//
// Which modules: those the SPA entry reaches through static imports alone. Anything
// behind `import()` stays out, so a lazy page stays its own chunk. Those modules
// split into three kinds:
//   - shared: an app window (any other build entry) also reaches it, by any import.
//     All of these go into one `first-load-shared` chunk, so an app window loads
//     that chunk and never the dashboard's own code.
//   - vendor: the rest of node_modules, into `first-load-vendor`. Library code
//     changes less often than ours, so it keeps its cache across most releases.
//   - own: the dashboard's own code, cut into `layers` chunks by depth (below).
//
// Why no chunk can import back into one that imports it (a cycle, which blanks the
// page; scripts/check-chunk-cycles.mjs is the gate):
//   - A static dependency of a first-load module is itself a first-load module.
//   - A dependency of a shared module is reached by the same app window, so it is
//     shared too. The shared chunk imports only itself and the vendor-* groups.
//   - A dependency of a vendor module is in node_modules, so it is vendor or shared.
//   - Own modules are cut by height: a module's height is one more than the
//     tallest own module it imports (a group of modules that import each other in
//     a loop counts as one and stays in one chunk). An import always points to a
//     lower height, so a layer only imports lower layers.
// Higher priority goes to the lower layer, so when rolldown pulls a module's
// dependencies into its group (`includeDependenciesRecursively`, left on), they
// are already taken by the lower layer and stay there.

/**
 * @param {object} opts
 * @param {string} opts.mainEntryId  absolute id of the SPA entry (index.html)
 * @param {string[]} opts.otherEntryIds  absolute ids of every other build entry
 * @param {number} opts.layers  how many chunks the dashboard's own code is cut into
 * @param {number} opts.priority  priority of the top layer; every other group sits above it
 */
export function firstLoadGroups({ mainEntryId, otherEntryIds, layers, priority }) {
  /** One plan per chunking pass; a pass hands every call the same ctx. */
  const plans = new WeakMap()
  const plan = (ctx) => {
    let p = plans.get(ctx)
    if (!p) {
      p = buildPlan(ctx, mainEntryId, otherEntryIds, layers)
      plans.set(ctx, p)
    }
    return p
  }
  const groups = [
    { name: (id, ctx) => (plan(ctx).get(id) === 'shared' ? 'first-load-shared' : null), priority: priority + layers + 1 },
    { name: (id, ctx) => (plan(ctx).get(id) === 'vendor' ? 'first-load-vendor' : null), priority: priority + layers },
  ]
  for (let k = 0; k < layers; k++) {
    groups.push({ name: (id, ctx) => (plan(ctx).get(id) === k ? `first-load-${k}` : null), priority: priority + layers - 1 - k })
  }
  return groups
}

const NODE_MODULES = /[\\/]node_modules[\\/]/

/** @returns {Map<string, 'shared' | 'vendor' | number>} */
function buildPlan(ctx, mainEntryId, otherEntryIds, layers) {
  const info = (id) => ctx.getModuleInfo(id)
  const walk = (roots, withDynamic) => {
    const seen = new Set()
    const stack = [...roots]
    while (stack.length > 0) {
      const id = stack.pop()
      if (seen.has(id)) continue
      const m = info(id)
      if (!m) continue
      seen.add(id)
      for (const dep of m.importedIds) stack.push(dep)
      if (withDynamic) for (const dep of m.dynamicallyImportedIds) stack.push(dep)
    }
    return seen
  }
  const fromMain = walk([mainEntryId], false)
  const fromOthers = walk(otherEntryIds, true)
  const out = new Map()
  const own = []
  for (const id of fromMain) {
    if (fromOthers.has(id)) out.set(id, 'shared')
    else if (NODE_MODULES.test(id)) out.set(id, 'vendor')
    else own.push(id)
  }
  const ownSet = new Set(own)
  const deps = (id) => info(id).importedIds.filter((d) => ownSet.has(d))

  // Tarjan's strongly connected components, iterative so a deep import chain
  // cannot overflow the stack. Components come out dependencies-first.
  const index = new Map()
  const low = new Map()
  const onStack = new Set()
  const sccStack = []
  const comps = []
  let next = 0
  for (const root of own) {
    if (index.has(root)) continue
    const work = [[root, 0]]
    index.set(root, next)
    low.set(root, next++)
    sccStack.push(root)
    onStack.add(root)
    while (work.length > 0) {
      const frame = work[work.length - 1]
      const [v, i] = frame
      const ds = deps(v)
      if (i < ds.length) {
        frame[1]++
        const w = ds[i]
        if (!index.has(w)) {
          index.set(w, next)
          low.set(w, next++)
          sccStack.push(w)
          onStack.add(w)
          work.push([w, 0])
        } else if (onStack.has(w)) {
          low.set(v, Math.min(low.get(v), index.get(w)))
        }
        continue
      }
      work.pop()
      if (work.length > 0) {
        const parent = work[work.length - 1][0]
        low.set(parent, Math.min(low.get(parent), low.get(v)))
      }
      if (low.get(v) === index.get(v)) {
        const comp = []
        let w
        do {
          w = sccStack.pop()
          onStack.delete(w)
          comp.push(w)
        } while (w !== v)
        comps.push(comp)
      }
    }
  }
  const compOf = new Map()
  comps.forEach((c, i) => c.forEach((id) => compOf.set(id, i)))
  const height = new Array(comps.length).fill(0)
  comps.forEach((c, i) => {
    for (const id of c) {
      for (const d of deps(id)) {
        const j = compOf.get(d)
        if (j !== i) height[i] = Math.max(height[i], height[j] + 1)
      }
    }
  })
  const size = (c) => c.reduce((n, id) => n + ((info(id).code || '').length || 1), 0)
  const order = comps.map((c, i) => i).sort((a, b) => height[a] - height[b] || a - b)
  const total = order.reduce((n, i) => n + size(comps[i]), 0)
  let filled = 0
  for (const i of order) {
    const layer = Math.min(layers - 1, Math.floor((filled * layers) / total))
    for (const id of comps[i]) out.set(id, layer)
    filled += size(comps[i])
  }
  return out
}
