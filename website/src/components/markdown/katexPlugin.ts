// The lazy half of `lazyKatex.ts`: rehype-katex, KaTeX and its stylesheet, in
// one chunk the first load does not fetch.
import 'katex/dist/katex.min.css'

export { default } from 'rehype-katex'
