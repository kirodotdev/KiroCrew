# Vendored browser libraries for the dashboard package renderer

`kiro_crew.dashboard_package_render` inlines these files into the document it
builds for a `kind="dashboard"` artifact. They are checked in rather than
fetched because the page runs in an iframe under CSP `default-src 'none'` with
no network at all: a CDN reference would not load, and a page that silently
draws nothing is worse than a page that cannot be built.

Both files are **byte-identical to the upstream npm tarball**, licence header
included. Nothing here is edited, minified again, or re-bundled — a reviewer
checks provenance with one `sha256sum`.

| file | library | version | upstream path | sha256 | bytes |
| --- | --- | --- | --- | --- | --- |
| `three-0.159.0.min.js` | [three.js](https://threejs.org) | r159 (`three@0.159.0`) | `package/build/three.min.js` | `7b1c5d75b28d9de15042e2b374f83566d8c7146697af8fdeb4558b0fb528a585` | 668024 |
| `anime-3.2.2.min.js` | [anime.js](https://animejs.com) | 3.2.2 (`animejs@3.2.2`) | `package/lib/anime.min.js` | `b5ce1be3c3f530f192e0f2571d1942846096d66119cbada34bfdc912c4873f35` | 17384 |

Tarballs, verified against the registry's own `dist.shasum` before extraction:

* `https://registry.npmjs.org/three/-/three-0.159.0.tgz` — sha1
  `6576b1210805b14f0765bac41fd0e4ec18e43b2e`
* `https://registry.npmjs.org/animejs/-/animejs-3.2.2.tgz` — sha1
  `59be98c58834339d5847f4a70ddba74ac75b6afc`

Licences: `LICENSE-three.js.txt` and `LICENSE-anime.js.txt`, copied from the same
tarballs. Both MIT.

## Why r159 and not the current three.js

r159 is the **last release that ships a single self-contained classic script**
(`build/three.min.js`, a UMD bundle that defines `window.THREE`). From r160 the
only browser builds are ES modules, and `build/three.module.min.js` `import`s
`./three.core.js` — a second file, fetched by URL. Under `default-src 'none'`
with no network there is no URL that resolves, and the alternatives are all
worse than an older release:

* concatenating the two module files and deleting the `import` line edits
  upstream bytes, so the sha256 above would no longer mean anything;
* a `data:` URL import needs `script-src data:`, which hands the page a way to
  execute a string it composed itself.

So the renderer pays an old release to keep one unedited file. r159's
`WebGLRenderer`, `Scene`, `PerspectiveCamera`, `Group`, `Mesh`,
`SphereGeometry`, `TorusGeometry` and `MeshBasicMaterial` are the whole API the
`orbit` block uses, and none of them changed in r160.

`three-0.159.0.min.js` opens with upstream's own `console.warn` saying the
non-module builds are deprecated. It is left in place: stripping it would be an
edit, and the warning is true.

## anime.js, and the copy already in the repo

`website/src/lib/anime.es.js` is the same library at the same version, bundled
into the dashboard SPA by Vite. It is not reusable here: it is an ES module
living in the frontend build, and this renderer needs a classic script it can
paste into a `<script>` element from Python. `anime-3.2.2.min.js` is the UMD
build of the identical release, so the two cannot disagree about behaviour.
