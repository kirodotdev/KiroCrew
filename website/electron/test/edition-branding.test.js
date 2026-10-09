"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { before, describe, it } = require("node:test");
const { resolveLoadingPagePath } = require("../gateway-supervisor");

let desktop;
before(async () => {
  desktop = await import("../../scripts/lib/editionDesktop.mjs");
});

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "kc-edition-desktop-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const editionDir = path.join(root, "edition");
  const electronDir = path.join(root, "electron");
  fs.mkdirSync(path.join(editionDir, "desktop"), { recursive: true });
  fs.mkdirSync(electronDir, { recursive: true });
  return { editionDir, electronDir };
}

describe("desktop edition asset staging", () => {
  it("stages an allowlisted loading page under a distinct packaged name", (t) => {
    const { editionDir, electronDir } = fixture(t);
    fs.writeFileSync(
      path.join(editionDir, "desktop", "loading.html"),
      "<!doctype html><html><head><title>Edition</title></head><body>EDITION</body></html>",
    );

    const staged = desktop.stageEditionDesktop({
      editionDir,
      allowEdition: "1",
      electronDir,
    });

    assert.deepEqual(staged, [path.join(electronDir, "edition-loading.html")]);
    const output = fs.readFileSync(staged[0], "utf8");
    assert.match(output, /http-equiv="Content-Security-Policy"/);
    assert.match(output, /default-src 'none'/);
    assert.match(output, /connect-src 'none'/);
    assert.match(output, /<body>EDITION<\/body>/);
  });

  it("rejects an edition page that has no head for the enforced CSP", (t) => {
    const { editionDir, electronDir } = fixture(t);
    const source = path.join(editionDir, "desktop", "loading.html");
    fs.writeFileSync(source, "<html><body>unsafe edition</body></html>");

    assert.throws(
      () => desktop.stageEditionDesktop({ editionDir, allowEdition: "1", electronDir }),
      /must contain a <head> element/,
    );
    assert.equal(fs.existsSync(path.join(electronDir, "edition-loading.html")), false);
  });

  it("injects the CSP into the document head, not a commented head tag", (t) => {
    const { editionDir, electronDir } = fixture(t);
    fs.writeFileSync(
      path.join(editionDir, "desktop", "loading.html"),
      "<!doctype html><!-- <head> --><html><head><title>Edition</title></head><body>EDITION</body></html>",
    );

    const [staged] = desktop.stageEditionDesktop({
      editionDir,
      allowEdition: "1",
      electronDir,
    });

    const output = fs.readFileSync(staged, "utf8");
    const realHead = output.indexOf("<html><head>");
    const csp = output.indexOf('http-equiv="Content-Security-Policy"');
    assert.ok(realHead >= 0);
    assert.ok(csp > realHead);
    assert.equal(output.match(/Content-Security-Policy/g)?.length, 1);
  });

  it("fails closed when the edition opt-in is absent", (t) => {
    const { editionDir, electronDir } = fixture(t);
    assert.throws(
      () => desktop.stageEditionDesktop({ editionDir, allowEdition: "", electronDir }),
      /KIROCREW_ALLOW_EDITION=1/,
    );
  });

  it("rejects files outside the fixed desktop overlay allowlist", (t) => {
    const { editionDir, electronDir } = fixture(t);
    fs.writeFileSync(path.join(editionDir, "desktop", "main.js"), "shadow core");
    assert.throws(
      () => desktop.stageEditionDesktop({ editionDir, allowEdition: "1", electronDir }),
      /outside the desktop overlay allowlist.*main\.js/,
    );
  });

  it("cleans stale edition output when no edition is configured", (t) => {
    const { electronDir } = fixture(t);
    const stale = path.join(electronDir, "edition-loading.html");
    fs.writeFileSync(stale, "STALE");

    assert.deepEqual(
      desktop.stageEditionDesktop({ editionDir: "", allowEdition: "", electronDir }),
      [],
    );
    assert.equal(fs.existsSync(stale), false);
  });
});

describe("desktop edition loading page resolution", () => {
  for (const [platform, paths] of Object.entries({ posix: path.posix, win32: path.win32 })) {
    it(`prefers the packaged edition page when present (${platform})`, () => {
      const editionPage = paths.join("/app", "edition-loading.html");
      const selected = resolveLoadingPagePath("/app", {
        fs: { existsSync: (candidate) => candidate === editionPage },
        path: paths,
      });
      assert.equal(selected, editionPage);
    });

    it(`falls back to the stock page when no edition page is packaged (${platform})`, () => {
      const selected = resolveLoadingPagePath("/app", {
        fs: { existsSync: () => false },
        path: paths,
      });
      assert.equal(selected, paths.join("/app", "loading.html"));
    });
  }

  it("keeps the optional edition page in the electron-builder file list", () => {
    const pkg = JSON.parse(
      fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"),
    );
    assert.ok(pkg.build.files.includes("edition-loading.html"));
  });
});
