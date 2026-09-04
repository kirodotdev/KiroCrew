/** Pixel-diff two base64 PNGs inside the page, returning the changed extent.
 *
 * Runs in the page rather than in Node because the decode is `Image` +
 * `<canvas>`: a harness that shells out to a Node image library would add a
 * dependency to compare two screenshots it already holds as data URLs.
 *
 * `tol` is a per-channel threshold, and it is a parameter rather than a constant
 * because it is a property of the surface under test, not of the diff. Subpixel
 * antialiasing shifts a text run by a few units across the whole header while the
 * element under assertion differs by many tens, so counting the noise would let
 * the bounding box swallow the header and make a "confined to X" assertion
 * vacuous. Alpha is compared alongside RGB: a glow's edge differs in alpha before
 * opacity is flattened onto the page background.
 *
 * Both counts are returned on purpose. `n` is the thresholded signal the caller
 * asserts on; `rawN` is every pixel that differs at all, reported so the noise
 * the threshold discards stays visible instead of silently vanishing.
 *
 * @param {import('playwright').Page} page
 * @param {string} aB64 base64 PNG, without a data-URL prefix
 * @param {string} bB64
 * @param {number} tol per-channel tolerance, in 0-255 units
 * @returns {Promise<{n: number, rawN: number, minX: number, maxX: number,
 *   minY: number, maxY: number}>} `min`/`max` bound the thresholded pixels, and
 *   stay at their initial `Infinity`/`-1` when nothing crossed the threshold.
 */
export async function diffPngs(page, aB64, bB64, tol) {
  return page.evaluate(async ([a, b, t]) => {
    const load = (b64) => new Promise((res) => {
      const img = new Image()
      img.onload = () => {
        const c = document.createElement('canvas')
        c.width = img.naturalWidth; c.height = img.naturalHeight
        c.getContext('2d').drawImage(img, 0, 0)
        res(c.getContext('2d').getImageData(0, 0, c.width, c.height))
      }
      img.src = `data:image/png;base64,${b64}`
    })
    const [ia, ib] = await Promise.all([load(a), load(b)])
    let n = 0, rawN = 0, minX = Infinity, maxX = -1, minY = Infinity, maxY = -1
    for (let y = 0; y < ia.height; y++) {
      for (let x = 0; x < ia.width; x++) {
        const i = (ia.width * y + x) << 2
        let d = 0
        for (let k = 0; k < 4; k++) d = Math.max(d, Math.abs(ia.data[i + k] - ib.data[i + k]))
        if (d > 0) rawN++
        if (d > t) {
          n++
          if (x < minX) minX = x
          if (x > maxX) maxX = x
          if (y < minY) minY = y
          if (y > maxY) maxY = y
        }
      }
    }
    return { n, rawN, minX, maxX, minY, maxY }
  }, [aB64, bB64, tol])
}
