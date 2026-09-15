/** Paste N image Files on the composer textarea in one native ClipboardEvent —
 *  the shape a real multi-screenshot clipboard produces. The bytes are real
 *  PNGs so the client-side resize path decodes (and, above the model's
 *  long-edge limit, downscales) them for real.
 *
 *  `files` is [{ name, b64 }] where b64 is the PNG bytes base64-encoded. */
export async function pasteImages(page, files) {
  await page.evaluate(async payload => {
    const ta = document.querySelector('textarea[data-composer-typo]')
    if (!ta) throw new Error('composer textarea not found')
    ta.focus()
    const dt = new DataTransfer()
    for (const { name, b64 } of payload) {
      const bin = atob(b64)
      const bytes = new Uint8Array(bin.length)
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
      dt.items.add(new File([bytes], name, { type: 'image/png' }))
    }
    ta.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: dt }))
  }, files)
}
