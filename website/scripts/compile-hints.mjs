// Vite plugin: mark every first-load chunk with V8's explicit compile hint.
//
// Without a hint, V8 compiles a function lazily the first time it is called, on
// the main thread. The dashboard's first React render calls thousands of them,
// so about half of that render's task is parsing and compiling (V8.CompileCode
// ~90 ms of a ~175 ms task, measured in a startup trace). The magic comment
// `//# allFunctionsCalledOnLoad` on a script's first line tells V8 to compile
// every function in it eagerly, on the background thread that already streams
// and compiles the script while it downloads, so that work leaves the main thread.
// Browsers without the feature read it as an ordinary comment.
//
// Only the chunks `index.html` loads before the first paint get it: the `main`
// entry chunk and every chunk it reaches through static imports. Lazy chunks are
// left alone, since eagerly compiling code that may never run costs memory for
// nothing. The cost here is memory too: the renderer holds bytecode for functions
// that would otherwise never compile (measured renderer PSS: +17 to +24 MB on the
// boot page, about +30 MB on the chat page with a 100-row session).
//
// Runs in `generateBundle`, after minification, which would otherwise drop the
// comment; it must be the first line, so it is prepended to the final code.

export const COMPILE_HINT = '//# allFunctionsCalledOnLoad\n'

/** File names of the `main` entry chunk and every chunk it statically imports. */
export function firstLoadChunkFiles(bundle, entryName = 'main') {
  const chunks = Object.values(bundle).filter((c) => c.type === 'chunk')
  const byFile = new Map(chunks.map((c) => [c.fileName, c]))
  const entry = chunks.find((c) => c.isEntry && c.name === entryName)
  const seen = new Set()
  const stack = entry ? [entry.fileName] : []
  while (stack.length > 0) {
    const file = stack.pop()
    if (seen.has(file) || !byFile.has(file)) continue
    seen.add(file)
    for (const dep of byFile.get(file).imports) stack.push(dep)
  }
  return seen
}

export function compileHintsPlugin() {
  return {
    name: 'kirocrew-compile-hints',
    apply: 'build',
    generateBundle: {
      // After Vite's own post hooks, which prepend `__vite__mapDeps` to chunks
      // with dynamic imports; the hint has to stay the first line.
      order: 'post',
      handler(_options, bundle) {
        for (const file of firstLoadChunkFiles(bundle)) {
          const chunk = bundle[file]
          if (!chunk.code.startsWith(COMPILE_HINT)) chunk.code = COMPILE_HINT + chunk.code
        }
      },
    },
  }
}
