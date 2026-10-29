import { namedCeiling } from './namedCeiling'

/**
 * The wait for anything the Lexical composer renders: with `lexicalComposer` on,
 * the editor sits behind the `React.lazy` boundary in
 * `components/chat-input/engine.tsx` (`import('../LexicalComposerInput')`), and
 * under a loaded parallel run that chunk takes longer than the 1000ms `findBy*`
 * default (#24213; "A `React.lazy` boundary races the 1000ms default" in
 * website/docs/testing.md). Pass it as a findBy's THIRD argument; the second is
 * matcher options.
 */
export const LAZY_COMPOSER_MOUNT = namedCeiling('LAZY_COMPOSER_MOUNT', 5000)
