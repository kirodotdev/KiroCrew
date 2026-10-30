/**
 * React Compiler opt-in list, shared by the build (vite.config.ts), the lint
 * config (eslint.config.js) and the bailout test
 * (src/test/reactCompilerAllowlist.test.ts).
 *
 * Only the files listed here are compiled. A listed file must compile with no
 * bailout: the test runs the compiler over each one and fails on any function
 * the compiler would leave uncompiled, and the lint block turns on the
 * compiler-backed react-hooks rules for these files only. Add a file once it
 * compiles cleanly and its tests pass under the compiler.
 *
 * Why a list instead of the whole tree: most of the tree still bails (refs read
 * in render, exhaustive-deps suppressions, try/finally), and a bailed function
 * is silently left uncompiled, so compiling everything would cost a Babel pass
 * over every module for code nobody checks. Babel only runs on listed files.
 */

/** Paths relative to website/, forward slashes, sorted. */
export const REACT_COMPILER_FILES = ['src/pages/chat/page/ChatPaneNotices.tsx']

/**
 * babel-plugin-react-compiler options. `target: '18'` imports the memo cache
 * from the react-compiler-runtime package, because the app runs React 18.
 * The build keeps the compiler's default panicThreshold ('none'): a function
 * that bails is emitted as written. The test is what refuses bailouts.
 */
export const REACT_COMPILER_OPTIONS = { target: '18' }

/**
 * One RegExp matching the module ids of the listed files under `root` (the
 * absolute website/ dir), with or without a Vite query suffix. Returns null for
 * an empty list so callers can skip the Babel plugin entirely.
 */
export function reactCompilerIdFilter(root, files = REACT_COMPILER_FILES) {
  if (files.length === 0) return null
  const base = root.replace(/\\/g, '/').replace(/\/$/, '')
  const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return new RegExp(`^(?:${files.map((f) => esc(`${base}/${f}`)).join('|')})(?:\\?|$)`)
}
