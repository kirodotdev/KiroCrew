/**
 * Model-facing text for a quoted message: the attribution line closing the
 * blockquote `quoteBlock` writes into the send.
 *
 * English on purpose, like every `*.prompt.ts` module: this line is READ BY THE
 * AGENT, in the transcript's language of record, so it says whose words the
 * block carries. It also appears in the transcript, which is why the i18n
 * boundary is this named module rather than a shape exemption — the card the
 * user sees carries the localized author label instead (`QuoteCard`).
 *
 * `stripQuoteBlock` relies on this text being a pure function of the role, so a
 * change here changes what old rows fail to strip (they keep their text and
 * card, never lose anything — see messageQuote.ts). Keep it stable.
 */
export function quoteAttribution(role: 'user' | 'assistant'): string {
  return role === 'user'
    ? '— quoting an earlier message from the user'
    : '— quoting an earlier message from the assistant'
}
