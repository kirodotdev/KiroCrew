// Migration of the per-user electron-store directory across the npm `name`
// rename ("kirocrew-electron-mac" -> "kirocrew-desktop" / "-nightly").
//
// WHY THIS EXISTS. Electron derives userData from the npm package name, so renaming
// it repoints electron-store at a brand-new directory and every setting in the old
// one is silently orphaned. Observed on a machine that took the nightly rename:
// %APPDATA%\kirocrew-electron-mac\config.json still held
//
//     "updateChannel": "stable",  "windowState": {...},  "themeAccent": "#8e48ff"
//
// while the freshly created kirocrew-desktop-nightly\config.json had
// `updateChannel: ""` and default geometry.
//
// The worst loss is updateChannel, because it is the stable<->insider switcher and
// resolveChannel() treats an unset preference as STABLE. An insider user whose
// preference is orphaned is migrated onto the stable feed with no consent and no
// message -- the exact outcome resolveChannel's contract calls out as the thing a
// channel decision must never do implicitly.
//
// HOW IT AVOIDS OVERWRITING ANYTHING. The migration SEEDS the destination
// config.json before electron-store opens it, and only ever when that file does not
// exist. That single condition carries the whole state machine, which is why there
// is no marker, no pending flag and no "is this store fresh?" verdict to get wrong:
//
//   file absent    -> there is nothing to overwrite, by definition
//   seeding throws -> file stays absent -> the next launch retries identically
//   seeding works  -> file exists       -> never eligible again
const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const Store = require("electron-store");

const {
  seedRenamedStore,
  legacyStoreFile,
  MIGRATED_KEYS,
  LEGACY_STORE_NAME,
} = require("../store-rename");

// The real defaults main.js constructs the store with.
const DEFAULTS = {
  remoteHost: "",
  kirocrewBinPath: "~/.local/bin/kirocrew",
  remoteHosts: {},
  sshTimeoutMs: 20000,
  windowState: null,
  globalHotkey: null,
  lastNudgedVersion: "",
  themeAccent: "",
  updateChannel: "",
  runLocalGateway: true,
  linuxFrameless: null,
};

function tmpUserData() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "kc-store-"));
}

// Open a REAL electron-store the way main.js does, so assertions run against the
// library's own merge of file + defaults rather than a hand-rolled fake. An earlier
// fake returned `undefined` for untouched keys and hid a bug: electron-store writes
// its defaults to config.json on construction, so `get()` really returns 20000 /
// true / {} for keys nobody ever set.
function openStore(userData) {
  return new Store({ cwd: userData, defaults: DEFAULTS });
}

test("seeds every orphaned setting into a store that does not exist yet", () => {
  const userData = tmpUserData();
  const legacy = {
    updateChannel: "insider",
    themeAccent: "#8e48ff",
    windowState: { x: 116, y: 0, width: 1354, height: 913 },
    globalHotkey: "Alt+Space",
    remoteHosts: { 5476: { host: "box", binPath: "/usr/bin/kirocrew" } },
    sshTimeoutMs: 30000,
    runLocalGateway: false,
    linuxFrameless: true,
  };
  assert.strictEqual(seedRenamedStore(userData, { readLegacy: () => legacy }), true);

  // Read back through electron-store, which is what main.js will see.
  const store = openStore(userData);
  assert.strictEqual(store.get("updateChannel"), "insider");
  assert.strictEqual(store.get("themeAccent"), "#8e48ff");
  assert.deepStrictEqual(store.get("windowState"), legacy.windowState);
  assert.strictEqual(store.get("globalHotkey"), "Alt+Space");
  assert.deepStrictEqual(store.get("remoteHosts"), legacy.remoteHosts);
  // Behavioural keys are carried too. There is no ambiguity to fear here: the
  // destination did not exist, so `runLocalGateway: false` cannot be overriding a
  // choice. A stable user who runs as a pure client keeps that setup.
  assert.strictEqual(store.get("sshTimeoutMs"), 30000);
  assert.strictEqual(store.get("runLocalGateway"), false);
  assert.strictEqual(store.get("linuxFrameless"), true);
});

test("carries the PRE-remoteHosts single-host keys so the chained migration still runs", () => {
  // Two migrations in series, and this one runs first. host-config.js's
  // migrateRemoteHostConfig converts the old single-host pair (remoteHost +
  // kirocrewBinPath) into the per-port remoteHosts map, and it reads those keys from
  // the store. An install old enough to predate remoteHosts therefore loses its
  // remote connection twice over if the rename seed drops the pair: the seed carries
  // an empty remoteHosts, and the host migration then finds nothing to convert.
  const userData = tmpUserData();
  assert.strictEqual(
    seedRenamedStore(userData, {
      readLegacy: () => ({ remoteHost: "box.example", kirocrewBinPath: "/opt/bin/kirocrew" }),
    }),
    true
  );
  const store = openStore(userData);
  assert.strictEqual(store.get("remoteHost"), "box.example");
  assert.strictEqual(store.get("kirocrewBinPath"), "/opt/bin/kirocrew");

  // ...and the chained migration can now do its job.
  const { migrateRemoteHostConfig } = require("../host-config");
  assert.strictEqual(migrateRemoteHostConfig(store, 5476), true);
  assert.deepStrictEqual(store.get("remoteHosts"), {
    5476: { host: "box.example", binPath: "/opt/bin/kirocrew" },
  });
});

test("NEVER touches a destination that already exists", () => {
  // The one condition the whole design rests on. An established store -- every
  // nightly install that already crossed the rename, and every launch after the
  // first -- is left completely alone, so no deliberate choice can be overwritten
  // and no cleared value can come back.
  const userData = tmpUserData();
  const existing = openStore(userData);
  existing.set("remoteHosts", {});     // deliberately cleared
  existing.set("updateChannel", "stable");

  assert.strictEqual(
    seedRenamedStore(userData, {
      readLegacy: () => ({ remoteHosts: { 5476: { host: "old-box" } }, updateChannel: "insider" }),
    }),
    false,
    "an existing destination must never be seeded"
  );

  const store = openStore(userData);
  assert.deepStrictEqual(store.get("remoteHosts"), {}, "a cleared host came back");
  assert.strictEqual(store.get("updateChannel"), "stable", "a deliberate choice was overwritten");
});

test("writes atomically, so an interrupted write cannot leave malformed JSON", () => {
  // config.json is the file electron-store parses on the NEXT launch, and a truncated
  // write is worse than no migration: the store either throws or resets. Write to a
  // temp sibling and rename, which is atomic within a directory, so the destination
  // only ever appears complete.
  const userData = tmpUserData();
  const seen = [];
  seedRenamedStore(userData, {
    readLegacy: () => ({ updateChannel: "insider" }),
    writeFile: (file, data) => { seen.push(file); fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, data); },
  });
  assert.strictEqual(seen.length, 1);
  assert.notStrictEqual(
    path.basename(seen[0]),
    "config.json",
    "the payload must land on a temp sibling, then be renamed into place"
  );
  // ...and the finished file is valid, complete JSON.
  const raw = fs.readFileSync(path.join(userData, "config.json"), "utf8");
  assert.deepStrictEqual(JSON.parse(raw), { updateChannel: "insider" });
  // No temp file is left behind.
  assert.deepStrictEqual(fs.readdirSync(userData), ["config.json"]);
});

test("cleanup never deletes a destination this call did not create", () => {
  // Two first launches can race: Electron's single-instance lock is taken later, so
  // both may reach the seed. One rename wins; the loser's rename fails. If the loser's
  // cleanup then removed `target`, it would delete the WINNER's freshly migrated
  // settings and hand the user defaults. Only the temp sibling is ever this call's to
  // remove.
  const userData = tmpUserData();
  const target = path.join(userData, "config.json");
  fs.mkdirSync(userData, { recursive: true });
  // Stand in for the winner's completed file, appearing after the eligibility check.
  const winner = JSON.stringify({ updateChannel: "insider" });

  const lost = seedRenamedStore(userData, {
    readLegacy: () => ({ themeAccent: "#8e48ff" }),
    writeFile: (file, data) => {
      fs.writeFileSync(file, data);        // the temp sibling lands
      fs.writeFileSync(target, winner);    // ...and the rival finishes first
      throw new Error("EEXIST: rename lost the race");
    },
  });

  assert.strictEqual(lost, false);
  assert.strictEqual(
    fs.readFileSync(target, "utf8"),
    winner,
    "the losing launch deleted the winner's migrated settings"
  );
  assert.deepStrictEqual(
    fs.readdirSync(userData),
    ["config.json"],
    "the temp sibling must still be cleaned up"
  );
});

test("each invocation writes its OWN temp sibling, so racing launches cannot tear it", () => {
  // Two first launches can both reach the seed before Electron's single-instance
  // lock is taken. A SHARED temp name would let one process truncate the sibling
  // while the other renames it, exposing a partial config.json that electron-store
  // fails to parse on every later boot. Unique siblings make both renames
  // whole-file: last writer wins, both seeds complete.
  const seen = [];
  for (let launch = 0; launch < 2; launch += 1) {
    const userData = tmpUserData();
    seedRenamedStore(userData, {
      readLegacy: () => ({ updateChannel: "insider" }),
      writeFile: (file) => { seen.push(path.basename(file)); throw new Error("stop before rename"); },
    });
  }
  assert.strictEqual(seen.length, 2);
  assert.ok(seen[0].startsWith("config.json.migrating."), seen[0]);
  assert.notStrictEqual(seen[0], seen[1], "temp sibling must be unique per invocation");
});

test("a failed write leaves the destination absent, so the next launch retries", () => {
  // No persisted retry state exists, and none is needed: the destination file's own
  // absence IS the retry flag. A write that throws must therefore leave no partial
  // file behind, or the retry would be silently disarmed.
  const userData = tmpUserData();
  const failed = seedRenamedStore(userData, {
    readLegacy: () => ({ updateChannel: "insider" }),
    writeFile: () => { throw new Error("EPERM"); },
  });
  assert.strictEqual(failed, false);
  assert.strictEqual(
    fs.existsSync(path.join(userData, "config.json")),
    false,
    "a failed seed must not leave a file that would block the retry"
  );

  // The retry succeeds and recovers everything.
  assert.strictEqual(
    seedRenamedStore(userData, { readLegacy: () => ({ updateChannel: "insider" }) }),
    true
  );
  assert.strictEqual(openStore(userData).get("updateChannel"), "insider");
});

test("retries a failing legacy read, because a later launch cannot", () => {
  // The asymmetry that justifies the retry: a failed READ has no second chance. The
  // seed writes nothing, main.js constructs the store anyway (boot cannot be held
  // hostage to a convenience), and the destination then exists forever — so a
  // momentary lock from the other channel or a scanner would cost the settings
  // permanently. A couple of in-process attempts ride that out.
  const userData = tmpUserData();
  let attempts = 0;
  const sleeps = [];
  const migrated = seedRenamedStore(userData, {
    readLegacy: () => {
      attempts += 1;
      if (attempts < 3) throw new Error("EBUSY");
      return { updateChannel: "insider" };
    },
    sleep: (ms) => sleeps.push(ms),
  });
  assert.strictEqual(migrated, true, "a transient read failure must not lose the settings");
  assert.strictEqual(attempts, 3);
  // The attempts must be SPACED, and by growing amounts. Three same-tick retries are
  // one attempt in disguise: a lock that outlives a microsecond defeats them all at
  // once, and the read failure is exactly the launch that is already about to lose
  // the settings for good.
  assert.deepStrictEqual(sleeps, [80, 240], "each failed attempt must back off before the next");
  assert.strictEqual(openStore(userData).get("updateChannel"), "insider");
});

test("a read that succeeds first try never sleeps", () => {
  // The backoff must cost the happy path nothing: it exists to ride out a lock, and
  // there is no lock to ride out when the first read comes back.
  const userData = tmpUserData();
  const sleeps = [];
  assert.strictEqual(
    seedRenamedStore(userData, {
      readLegacy: () => ({ updateChannel: "insider" }),
      sleep: (ms) => sleeps.push(ms),
    }),
    true
  );
  assert.deepStrictEqual(sleeps, [], "no failed attempt, so nothing to wait out");
});

test("a CORRUPTED legacy store is final and loud, never retried", () => {
  // SyntaxError is not a lock: the same bytes fail identically on every attempt,
  // this launch or any other, so waiting between retries would only slow the boot
  // that is already losing the settings. One attempt, one log line, no sleep — and
  // no exception may escape to the caller, because this runs before any window
  // exists. Exercised through the REAL read path (a real malformed file on disk),
  // since the JSON.parse throw is what production hits.
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "kc-corrupt-"));
  const userData = path.join(parent, "kirocrew-desktop-nightly");
  fs.mkdirSync(userData, { recursive: true });
  const legacyDir = path.join(parent, LEGACY_STORE_NAME);
  fs.mkdirSync(legacyDir, { recursive: true });
  fs.writeFileSync(path.join(legacyDir, "config.json"), "{ not json");

  const notes = [];
  const sleeps = [];
  const migrated = seedRenamedStore(userData, {
    log: (m) => notes.push(m),
    sleep: (ms) => sleeps.push(ms),
  });
  assert.strictEqual(migrated, false);
  assert.deepStrictEqual(sleeps, [], "corruption is not transient, so retrying is pointless");
  assert.strictEqual(notes.length, 1, "the loss must be visible in the boot log");
  assert.ok(notes[0].includes("not valid JSON"), notes[0]);
  assert.strictEqual(
    fs.existsSync(path.join(userData, "config.json")),
    false,
    "a corrupted legacy store must seed nothing"
  );
});

test("an ABSENT legacy store is final and silent, never retried", () => {
  // ENOENT is not a failure to ride out -- there is simply nothing to carry, which is
  // every fresh install. Retrying it would waste two more stat calls on every launch
  // and log noise for the common case.
  const userData = tmpUserData();
  let attempts = 0;
  const notes = [];
  const migrated = seedRenamedStore(userData, {
    readLegacy: () => {
      attempts += 1;
      throw Object.assign(new Error("ENOENT: no such file"), { code: "ENOENT" });
    },
    log: (m) => notes.push(m),
  });
  assert.strictEqual(migrated, false);
  assert.strictEqual(attempts, 1, "an absent legacy store must not be retried");
  assert.deepStrictEqual(notes, [], "the common case must stay silent");
});

test("a read failure or absent legacy store seeds nothing at all", () => {
  // Nothing to carry, so no file is created -- which also means the next launch is
  // free to try again if the legacy store was merely unreadable. Both paths are
  // silent: an absent legacy store is the overwhelmingly common case (every fresh
  // install) and is not worth a log line.
  for (const readLegacy of [
    () => null,
    () => { throw Object.assign(new Error("ENOENT"), { code: "ENOENT" }); },
    () => { throw new Error("EACCES"); },
    () => "not an object",
  ]) {
    const userData = tmpUserData();
    // No-op sleep: the EACCES case exhausts every attempt, and the real backoff
    // would slow this test by the full wait for no extra coverage (the backoff
    // shape has its own test).
    assert.strictEqual(seedRenamedStore(userData, { readLegacy, sleep: () => {} }), false);
    assert.strictEqual(fs.existsSync(path.join(userData, "config.json")), false);
  }
});

test("carries only the allowlisted keys, including default-equal values", () => {
  // The legacy file is a whole electron-store dump, including keys a newer build may
  // have retired; copying it wholesale would resurrect settings this build no longer
  // understands. A value equal to its default IS carried: the destination never
  // exists at seed time, so re-stating a default changes nothing, and filtering it
  // would require a second defaults table that could silently drift from main.js and
  // misclassify a real legacy choice as "default, drop it".
  const userData = tmpUserData();
  assert.strictEqual(
    seedRenamedStore(userData, {
      readLegacy: () => ({
        updateChannel: "insider",
        someRetiredFlag: true,
        themeAccent: "",          // equal to its default: carried, and a no-op
      }),
    }),
    true
  );
  const raw = JSON.parse(fs.readFileSync(path.join(userData, "config.json"), "utf8"));
  assert.deepStrictEqual(Object.keys(raw).sort(), ["themeAccent", "updateChannel"]);
  assert.strictEqual(raw.someRetiredFlag, undefined);
  assert.strictEqual(raw.themeAccent, "");
});

test("preserves an explicitly emptied legacy value", () => {
  // "" is a REAL choice for globalHotkey: main.js documents null as "platform
  // default" and "" as "disabled". Treating empty-as-absent would silently re-enable
  // a hotkey the user turned off.
  const userData = tmpUserData();
  assert.strictEqual(
    seedRenamedStore(userData, { readLegacy: () => ({ globalHotkey: "" }) }),
    true
  );
  assert.strictEqual(openStore(userData).get("globalHotkey"), "");
});

test("locates the legacy store beside the CURRENT one, on every platform", () => {
  // The legacy directory is a SIBLING of the live userData directory, whatever
  // Electron chose: %APPDATA%\<name> on Windows, ~/Library/Application Support/<name>
  // on macOS, ~/.config/<name> on Linux. Deriving it from the live path keeps this
  // correct on all three -- hand-rolling APPDATA-or-~/.config silently resolved to a
  // nonexistent ~/.config path on macOS, so the lookup ENOENT'd and every setting
  // stayed orphaned there.
  const cases = [
    ["win32", "C:\\Users\\jane\\AppData\\Roaming\\kirocrew-desktop"],
    ["darwin", "/Users/jane/Library/Application Support/kirocrew-desktop"],
    ["linux", "/home/jane/.config/kirocrew-desktop-nightly"],
  ];
  for (const [platform, userData] of cases) {
    const file = legacyStoreFile(userData);
    assert.ok(file.includes(LEGACY_STORE_NAME), `${platform}: must name the legacy dir`);
    assert.ok(!file.includes("kirocrew-desktop"), `${platform}: must not point at the CURRENT store`);
    assert.match(file, /config\.json$/, `${platform}: must target electron-store's file`);
  }
});

test("main.js seeds BEFORE constructing the store", () => {
  // Ordering is the whole correctness of the approach: electron-store writes its
  // defaults on construction, so a seed placed after `new Store(...)` would find the
  // file already present and never run. Neither ordering fails a unit test on its
  // own, so pin the order in the source.
  const main = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  const seed = main.indexOf("seedRenamedStore(");
  const construct = main.indexOf("const store = new Store(");
  assert.notStrictEqual(seed, -1, "expected the seed call in main.js");
  assert.notStrictEqual(construct, -1, "expected the store construction in main.js");
  assert.ok(seed < construct, "the seed must precede `new Store(...)`");
});

test("every migrated key is a real store default in main.js", () => {
  // A key that no longer exists in the store's defaults would be seeded and then
  // ignored, which reads like a working migration while carrying nothing.
  const main = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  for (const key of MIGRATED_KEYS) {
    assert.match(
      main,
      new RegExp(`^\\s*${key}:`, "m"),
      `${key} is migrated but is not a store default in main.js`
    );
  }
});

test("the legacy directory name is the pre-rename npm package name", () => {
  // Pinned: this is how the migration finds the old store, and it is NOT derivable
  // from the current package.json (the rename removed it). A typo makes the
  // migration silently find nothing.
  assert.strictEqual(LEGACY_STORE_NAME, "kirocrew-electron-mac");
});

test("the Windows install guide does not contradict the code on the stable lane", () => {
  // A stale doc paragraph claimed "Stable has no Windows lane yet", named a
  // WINDOWS_CHANNELS set that does not exist in auto-update.js, and stated the
  // promotion bundle carries no Windows installer. All three are false: release.yml
  // runs publish-windows-x64 for the stable channel in promote mode,
  // release_promotion.py records windows_installer / windows_blockmap roles, and the
  // updater gates on channelHasLane() over KNOWN_CHANNELS with no win32 arm.
  //
  // That combination is worse than a typo: it tells a reader that a stable Windows
  // client cannot update, which is exactly the symptom this area's real bugs produce,
  // so it sends the next investigation down a dead end.
  const repo = path.resolve(__dirname, "..", "..", "..");
  const guide = fs.readFileSync(path.join(repo, "docs", "guides", "windows-install.md"), "utf8");
  const updater = fs.readFileSync(path.join(__dirname, "..", "auto-update.js"), "utf8");

  assert.ok(
    !/WINDOWS_CHANNELS/.test(guide) || /WINDOWS_CHANNELS/.test(updater),
    "the guide names a WINDOWS_CHANNELS symbol that auto-update.js does not define"
  );
  assert.doesNotMatch(
    guide,
    /Stable has no Windows lane/i,
    "the guide claims stable has no Windows lane, but release.yml's publish-windows-x64 "
      + "runs for the stable channel and release_promotion.py carries the installer roles"
  );
  assert.match(
    updater,
    /KNOWN_CHANNELS = new Set\(\["nightly", "insider", "stable"\]\)/,
    "stable left KNOWN_CHANNELS; the guide's per-channel wording must move with it"
  );
});

// ---------------------------------------------------------------------------
// Mochi's per-machine store (mochi-machine.json) crosses the same rename.
// mochi/index.js reuses seedRenamedStore with its own file name and allowlist.
// ---------------------------------------------------------------------------

const { MACHINE_STORE_DEFAULTS } = require("../mochi/machineStore");

// The exact allowlist mochi/index.js derives: the namespace segments of the
// dotted default keys. electron-store resolves dots via dot-notation, so every
// user-WRITTEN value in the raw file is nested under the top-level namespace
// object; the flat dotted spellings only ever appear as construction-time
// defaults, which carry no user intent.
const MOCHI_KEYS = [...new Set(Object.keys(MACHINE_STORE_DEFAULTS).map((k) => k.split(".")[0]))];

function openMochiStore(userData) {
  return new Store({ cwd: userData, name: "mochi-machine", defaults: MACHINE_STORE_DEFAULTS });
}

test("seeds Mochi's machine store across the rename, and the store reads it back", () => {
  // The raw legacy file as a real pre-rename install wrote it: the flat dotted
  // defaults from construction, plus the nested namespace object holding what the
  // user (and the one-shot gateway migration) actually wrote.
  const legacy = {
    "mochi.petInstance": "self",
    "mochi.shortcuts": null,
    "mochi.machinePrefsMigrated": false,
    "mochi.userSetPrefs": [],
    mochi: {
      petInstance: "remote-5476",
      shortcuts: { summon: "Alt+M" },
      machinePrefsMigrated: true,
      userSetPrefs: ["mochi.petInstance"],
    },
  };
  const userData = tmpUserData();
  assert.strictEqual(
    seedRenamedStore(userData, {
      storeFileName: "mochi-machine.json",
      keys: MOCHI_KEYS,
      readLegacy: () => legacy,
    }),
    true
  );

  // Only the nested namespace is carried; the flat construction defaults are dead
  // data and stay behind.
  const raw = JSON.parse(fs.readFileSync(path.join(userData, "mochi-machine.json"), "utf8"));
  assert.deepStrictEqual(Object.keys(raw), ["mochi"]);

  // Read back through a real electron-store the way mochi/index.js constructs it.
  const store = openMochiStore(userData);
  assert.strictEqual(store.get("mochi.petInstance"), "remote-5476");
  assert.deepStrictEqual(store.get("mochi.shortcuts"), { summon: "Alt+M" });
  // The one-shot flag survives, so the gateway import cannot re-run over choices
  // the user made after the rename.
  assert.strictEqual(store.get("mochi.machinePrefsMigrated"), true);
  assert.deepStrictEqual(store.get("mochi.userSetPrefs"), ["mochi.petInstance"]);
});

test("a legacy Mochi store that was never written seeds nothing", () => {
  // An install where the user never touched Mochi holds only the flat
  // construction-time defaults — no nested namespace object exists. There is no
  // intent to carry, so no file is written and construction regenerates defaults.
  const userData = tmpUserData();
  assert.strictEqual(
    seedRenamedStore(userData, {
      storeFileName: "mochi-machine.json",
      keys: MOCHI_KEYS,
      readLegacy: () => ({
        "mochi.petInstance": "self",
        "mochi.shortcuts": null,
        "mochi.machinePrefsMigrated": false,
        "mochi.userSetPrefs": [],
      }),
    }),
    false
  );
  assert.strictEqual(fs.existsSync(path.join(userData, "mochi-machine.json")), false);
});

test("seeding Mochi's store never touches the main config.json, and vice versa", () => {
  // Two stores, one directory, one mechanism: each seed is scoped to its own file,
  // so a Mochi seed on a machine whose config.json already exists (or the reverse)
  // must neither block on nor overwrite the sibling.
  const userData = tmpUserData();
  const existing = openStore(userData);
  existing.set("updateChannel", "stable");

  assert.strictEqual(
    seedRenamedStore(userData, {
      storeFileName: "mochi-machine.json",
      keys: MOCHI_KEYS,
      readLegacy: () => ({ mochi: { petInstance: "remote-1" } }),
    }),
    true,
    "an existing config.json must not make mochi-machine.json ineligible"
  );
  assert.strictEqual(openStore(userData).get("updateChannel"), "stable");
  assert.strictEqual(openMochiStore(userData).get("mochi.petInstance"), "remote-1");
});

test("locates the legacy Mochi store beside the legacy config.json", () => {
  const file = legacyStoreFile(
    "/Users/jane/Library/Application Support/kirocrew-desktop",
    "mochi-machine.json"
  );
  assert.ok(file.includes(LEGACY_STORE_NAME), "must name the legacy dir");
  assert.match(file, /mochi-machine\.json$/, "must target Mochi's own store file");
});

test("mochi/index.js seeds BEFORE constructing its store", () => {
  // Same load-bearing order as main.js: electron-store writes its defaults on
  // construction, so a seed placed after `new Store(...)` finds the file already
  // present and never runs. Neither ordering fails a unit test on its own, so pin
  // the order in the source.
  const src = fs.readFileSync(path.join(__dirname, "..", "mochi", "index.js"), "utf8");
  const seed = src.indexOf("seedRenamedStore(app.getPath");
  const construct = src.indexOf("const machineStore = new Store(");
  assert.notStrictEqual(seed, -1, "expected the seed call in mochi/index.js");
  assert.notStrictEqual(construct, -1, "expected the store construction in mochi/index.js");
  assert.ok(seed < construct, "the seed must precede `new Store(...)`");
});
