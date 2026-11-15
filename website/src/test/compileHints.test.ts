import { COMPILE_HINT, compileHintsPlugin, firstLoadChunkFiles } from '../../scripts/compile-hints.mjs'

// The plugin puts V8's eager-compile hint on the chunks the dashboard loads
// before its first paint, and on nothing else: a lazy chunk that got it would be
// compiled in full on every load that reaches it, for code that may never run.

type Chunk = { type: 'chunk'; fileName: string; name: string; isEntry: boolean; imports: string[]; code: string }

function chunk(fileName: string, imports: string[] = [], extra: Partial<Chunk> = {}): Chunk {
  return { type: 'chunk', fileName, name: fileName.replace(/-.*$/, ''), isEntry: false, imports, code: 'export{};', ...extra }
}

function bundleOf(...chunks: Chunk[]) {
  return Object.fromEntries(chunks.map((c) => [c.fileName, c]))
}

function run(bundle: Record<string, Chunk>) {
  const plugin = compileHintsPlugin()
  plugin.generateBundle.handler({}, bundle)
}

describe('compileHintsPlugin', () => {
  it('hints the main entry and its static imports, and leaves lazy and other-entry chunks alone', () => {
    const bundle = bundleOf(
      chunk('main-A.js', ['shared-B.js'], { isEntry: true, name: 'main' }),
      chunk('shared-B.js', ['vendor-C.js']),
      chunk('vendor-C.js'),
      chunk('LazyPage-D.js', ['vendor-C.js']),
      chunk('panel-E.js', ['shared-B.js'], { isEntry: true, name: 'app-windows/x/panel' })
    )
    expect([...firstLoadChunkFiles(bundle)].sort()).toEqual(['main-A.js', 'shared-B.js', 'vendor-C.js'])
    run(bundle)
    for (const f of ['main-A.js', 'shared-B.js', 'vendor-C.js']) expect(bundle[f].code.startsWith(COMPILE_HINT)).toBe(true)
    for (const f of ['LazyPage-D.js', 'panel-E.js']) expect(bundle[f].code).toBe('export{};')
  })

  it('adds the hint once, as the first line', () => {
    const bundle = bundleOf(chunk('main-A.js', [], { isEntry: true, name: 'main', code: 'const __vite__mapDeps=1;' }))
    run(bundle)
    run(bundle)
    expect(bundle['main-A.js'].code).toBe(COMPILE_HINT + 'const __vite__mapDeps=1;')
  })
})
