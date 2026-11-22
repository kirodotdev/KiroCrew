// Build the differential-update blockmap for the macOS update zip.
//
// electron-updater's MacUpdater downloads only the changed blocks of a new zip
// when `<zip url>.blockmap` exists for both the installed and the new version,
// and falls back to a full download when either is missing. electron-builder
// writes a blockmap during the build, but that zip never ships: the notarize
// job re-creates it with `ditto` after stapling, so the blockmap has to be built
// from the final bytes, here.
//
// It calls electron-builder's own blockmap builder (app-builder-lib, pinned
// exact through `electron-builder` in package.json), so every version's
// blockmap comes from the same chunker the client expects. A future
// electron-builder that changes the chunker only makes the first update after
// the bump a full download: no block matches, nothing breaks.
//
// Usage: node build-zip-blockmap.js <zip> <out.blockmap> <out-info.json>
// The info file records the sha512 (base64) of the zip the blockmap was built
// from, so the publish job can refuse to pair it with other bytes.

const fs = require('fs')
const { buildBlockMap } = require('app-builder-lib/out/targets/blockmap/blockmap')

async function main(argv) {
  const [zip, outBlockmap, outInfo] = argv
  if (!zip || !outBlockmap || !outInfo) {
    throw new Error('usage: build-zip-blockmap.js <zip> <out.blockmap> <out-info.json>')
  }
  // "gzip" is the format electron-updater reads for a separate .blockmap file
  // (it gunzips the download); "deflate" is only for an embedded one.
  const info = await buildBlockMap(zip, 'gzip', outBlockmap)
  fs.writeFileSync(outInfo, JSON.stringify({ sha512: info.sha512 }) + '\n')
  return info
}

if (require.main === module) {
  main(process.argv.slice(2)).then(
    (info) => console.log(`blockmap for ${info.size} bytes, sha512 ${info.sha512}`),
    (err) => {
      console.error(err && err.stack ? err.stack : err)
      process.exit(1)
    }
  )
}
