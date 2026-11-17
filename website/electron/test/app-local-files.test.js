/**
 * The shell's one copy of the local-file rules: reveal an existing regular file,
 * open an existing regular image. A path from a page is untrusted, so each rule
 * is pinned with the file system and shell faked.
 */

const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const { IMAGE_EXTS, openLocalImage, revealLocalFile } = require("../app-local-files");

/** files: path -> "file" | "dir" | {link: target}. */
function fakeFs(files) {
  const resolve = (p) => {
    const entry = files[p];
    if (entry === undefined) throw new Error(`ENOENT ${p}`);
    return typeof entry === "object" ? resolve(entry.link) : p;
  };
  return {
    statSync: (p) => ({ isFile: () => files[resolve(p)] === "file" }),
    realpathSync: resolve,
  };
}

function deps(files, opened = [], answer = "") {
  return {
    fs: fakeFs(files),
    path,
    shell: { showItemInFolder: (p) => opened.push(["reveal", p]) },
    openPathHardened: async (_shell, p) => {
      opened.push(["open", p]);
      return answer;
    },
  };
}

test("reveal shows an existing regular file and nothing else", () => {
  const calls = [];
  const d = deps({ "/a/notes.txt": "file", "/a/dir": "dir" }, calls);
  assert.strictEqual(revealLocalFile(d, "/a/notes.txt"), true);
  assert.strictEqual(revealLocalFile(d, "/a/dir"), false);
  assert.strictEqual(revealLocalFile(d, "/a/missing"), false);
  assert.strictEqual(revealLocalFile(d, ""), false);
  assert.strictEqual(revealLocalFile(d, { path: "/a/notes.txt" }), false);
  assert.deepStrictEqual(calls, [["reveal", "/a/notes.txt"]]);
});

test("open-image judges a link by its target, and opens only a regular image", async () => {
  const calls = [];
  const refused = [];
  const d = {
    ...deps({
      "/a/cat.PNG": "file",
      "/a/key.png": { link: "/home/me/.ssh/id_rsa" },
      "/home/me/.ssh/id_rsa": "file",
      "/a/shot.png": { link: "/a/cat.PNG" },
      "/a/folder.png": "dir",
      "/a/run.sh": "file",
    }, calls),
    onRefused: (err) => refused.push(String(err && err.message)),
  };
  assert.strictEqual(await openLocalImage(d, "/a/cat.PNG"), true);
  assert.strictEqual(await openLocalImage(d, "/a/shot.png"), true, "a link to an image opens the image");
  assert.strictEqual(await openLocalImage(d, "/a/key.png"), false, "a .png link to a key is not an image");
  assert.strictEqual(await openLocalImage(d, "/a/folder.png"), false);
  assert.strictEqual(await openLocalImage(d, "/a/run.sh"), false);
  assert.strictEqual(await openLocalImage(d, "/a/gone.png"), false);
  assert.strictEqual(await openLocalImage(d, 7), false);
  assert.deepStrictEqual(calls, [["open", "/a/cat.PNG"], ["open", "/a/cat.PNG"]]);
  assert.deepStrictEqual(refused, ["ENOENT /a/gone.png"]);
});

test("open-image reports the system viewer refusing", async () => {
  assert.strictEqual(await openLocalImage(deps({ "/a/x.gif": "file" }, [], "no handler"), "/a/x.gif"), false);
});

test("the image list is the one the shell's windows have always used", () => {
  assert.deepStrictEqual([...IMAGE_EXTS], [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"]);
});
