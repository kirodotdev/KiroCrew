// Knowledge-context envelope markers: wire-format protocol strings, NOT UI copy.
//
// The send path wraps a user's selected knowledge library items in this
// envelope and prepends it to the LLM wire text (see `expandKnowledgeBlock` in
// `useKnowledgeFetch.ts`). These exact byte sequences are a contract with that
// producer: `stripKnowledgeEnvelope` (in `utils/pasteTokens.ts`) finds and
// removes the envelope on reconcile so it never surfaces in the user's chat
// bubble. They are matched verbatim and MUST NOT be translated — a translated
// fragment would break both the producer and the stripper. This module is a
// named i18n boundary (listed in `eslint.i18n.config.js` ignores), exactly like
// the other wire-format/CLI/CSS constant modules there.
export const KNOWLEDGE_ENVELOPE_OPEN = '[KNOWLEDGE CONTEXT'
export const KNOWLEDGE_ENVELOPE_END = '[END KNOWLEDGE CONTEXT]'
