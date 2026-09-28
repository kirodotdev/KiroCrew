/**
 * Auto-detect sprite frame dimensions by finding repeating transparent grid lines.
 * Scans alpha channel for fully transparent columns/rows, then finds the most
 * common spacing interval.
 */

/** Find the most common interval between sorted positions */
function bestInterval(positions: number[], totalSize: number): number {
  if (positions.length === 0) return totalSize
  // Build candidate intervals from gaps between consecutive transparent lines
  const gaps: number[] = []
  let prev = 0
  for (const p of positions) {
    if (p > prev) gaps.push(p - prev)
    prev = p + 1
  }
  if (prev < totalSize) gaps.push(totalSize - prev)

  // Count frequency of each gap size (allow ±1 tolerance)
  const freq = new Map<number, number>()
  for (const g of gaps) {
    // Round to nearest candidate
    const key = g
    freq.set(key, (freq.get(key) || 0) + 1)
  }

  // Return the most frequent gap
  let best = totalSize
  let bestCount = 0
  for (const [size, count] of freq) {
    if (count > bestCount || (count === bestCount && size < best)) {
      best = size
      bestCount = count
    }
  }
  return best
}

export function detectFrameSize(imageData: ImageData): { frameWidth: number; frameHeight: number; offsetY: number } {
  const { width, height, data } = imageData

  // Find fully transparent columns
  const transparentCols: number[] = []
  for (let x = 0; x < width; x++) {
    let allTransparent = true
    for (let y = 0; y < height; y++) {
      if (data[(y * width + x) * 4 + 3] > 10) { allTransparent = false; break }
    }
    if (allTransparent) transparentCols.push(x)
  }

  // Find fully transparent rows
  const transparentRows: number[] = []
  for (let y = 0; y < height; y++) {
    let allTransparent = true
    for (let x = 0; x < width; x++) {
      if (data[(y * width + x) * 4 + 3] > 10) { allTransparent = false; break }
    }
    if (allTransparent) transparentRows.push(y)
  }

  // Find contiguous transparent column bands → grid lines
  const colLines = findGridLines(transparentCols)
  const rowLines = findGridLines(transparentRows)

  const frameWidth = bestInterval(colLines, width)
  const frameHeight = bestInterval(rowLines, height)

  // Detect offsetY: number of transparent rows at the top
  let offsetY = 0
  for (let y = 0; y < height; y++) {
    if (!transparentRows.includes(y)) break
    offsetY = y + 1
  }

  return { frameWidth, frameHeight, offsetY }
}

/** Collapse contiguous transparent pixel positions into center points of each band */
function findGridLines(positions: number[]): number[] {
  if (positions.length === 0) return []
  const lines: number[] = []
  let start = positions[0]
  let end = positions[0]
  for (let i = 1; i < positions.length; i++) {
    if (positions[i] === end + 1) {
      end = positions[i]
    } else {
      lines.push(Math.floor((start + end) / 2))
      start = positions[i]
      end = positions[i]
    }
  }
  lines.push(Math.floor((start + end) / 2))
  return lines
}
