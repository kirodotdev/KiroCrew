// electron-builder afterPack hook: keep backend-dist out of the universal merge.
//
// `electron-builder --universal` packs a full x64 app and a full arm64 app, then
// @electron/universal walks both before it merges them: one `file` process per
// path, a SHA of every plain file, and a SHA of every Mach-O file. backend-dist
// holds two complete python-build-standalone trees (about 50,000 files each, the
// same bytes in both packs), against about 270 files for the Electron shell, so
// that walk took 20 to 43 minutes of the macOS build. Nothing in the merge needs
// those files: `build.mac.x64ArchFiles` only waves them through.
//
// So the hook moves them around the merge:
//   x64 pack    -> rename backend-dist next to the temp app (same volume, instant)
//   arm64 pack  -> delete its identical copy
//   merged app  -> rename the x64 copy back into place
// The universal app ends up with the same backend-dist the x64 pack produced,
// which is also the copy @electron/universal starts from.
//
// Directory names are electron-builder's own (app-builder-lib macPackager:
// `${appOutDir}-${arch}-temp`). If it renames all three, the hook does nothing
// and the merge walks everything again: slow, never wrong. If it renames only
// the temp ones, the merged step below fails the build on a missing stash.

const fs = require('fs')
const path = require('path')

const STASH = '.backend-dist-x64'

exports.default = async function universalBackendDist(context) {
  const { appOutDir, electronPlatformName, packager } = context
  if (electronPlatformName !== 'darwin') {
    return
  }

  const kind = {
    'mac-universal-x64-temp': 'x64',
    'mac-universal-arm64-temp': 'arm64',
    'mac-universal': 'merged'
  }[path.basename(appOutDir)]
  if (!kind) {
    return
  }

  const backendDist = path.join(
    appOutDir,
    `${packager.appInfo.productFilename}.app`,
    'Contents',
    'Resources',
    'backend-dist'
  )
  const stash = path.join(path.dirname(appOutDir), STASH)

  if (kind === 'x64') {
    fs.rmSync(stash, { recursive: true, force: true })
    fs.renameSync(backendDist, stash)
  } else if (kind === 'arm64') {
    fs.rmSync(backendDist, { recursive: true, force: true })
  } else {
    // Throws ENOENT when the x64 hook never ran: a universal app without its
    // backend must fail the build, not ship.
    fs.renameSync(stash, backendDist)
  }
}
