// The installer's running-app detection contract (#16745), split out of
// packaging.test.js; shared NSIS helpers live in packaging-test-helpers.js.
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { normalizedNsisBlock } = require("./packaging-test-helpers");

const REPO_ROOT = path.resolve(__dirname, "..", "..", "..");

// Issue #16745: the installer reported that Kiro Crew could not be closed when no
// Kiro Crew owned by this install was running. electron-builder's own
// _CHECK_APP_RUNNING matches a running process with a BARE prefix,
// $_.Path.StartsWith('$INSTDIR', ...), with no trailing separator, so a stable
// install at %LOCALAPPDATA%\Programs\KiroCrew matched a running
// %LOCALAPPDATA%\Programs\KiroCrew Nightly\KiroCrew.exe (the documented
// side-by-side nightly) -- and, with a widened $INSTDIR/InstallLocation, even an
// unrelated %LOCALAPPDATA%\Kiro-Cli\kiro-cli.exe. installer.nsh overrides the
// detection through the sanctioned customCheckAppRunning hook and matches only
// the install's own paths: $INSTDIR\<app exe> or anything under $INSTDIR\resources\.
describe("running-app detection cannot false-positive on a sibling install (#16745)", () => {
  const ROOT_LOCAL = path.resolve(__dirname, "..");
  const installerNsh = fs.readFileSync(path.join(ROOT_LOCAL, "build", "installer.nsh"), "utf8");
  const appRunning = fs.readFileSync(
    path.join(
      ROOT_LOCAL,
      "node_modules",
      "app-builder-lib",
      "templates",
      "nsis",
      "include",
      "allowOnlyOneInstallerInstance.nsh"
    ),
    "utf8"
  );

  it("defines the customCheckAppRunning hook CHECK_APP_RUNNING prefers over upstream", () => {
    // CHECK_APP_RUNNING dispatches to customCheckAppRunning when it is defined
    // (!ifmacrodef) and only falls back to _CHECK_APP_RUNNING otherwise, so
    // defining it is the override point that leaves the dependency pin intact.
    // Pin that upstream dispatch so a dependency bump that removes the hook
    // fails here rather than silently restoring the buggy prefix match.
    assert.match(installerNsh, /!macro customCheckAppRunning\b[\s\S]*?!macroend/);
    assert.match(
      appRunning,
      /!ifmacrodef customCheckAppRunning\s*\n\s*!insertmacro customCheckAppRunning/,
      "upstream must still prefer customCheckAppRunning over _CHECK_APP_RUNNING"
    );
    // Defining the hook makes allowOnlyOneInstallerInstance.nsh SKIP its own
    // include of getProcessInfo.nsh and `Var pid`, so installer.nsh must pull
    // both in or ${GetProcessInfo}/$pid are undefined where the macro expands.
    assert.match(
      appRunning,
      /!ifmacrondef customCheckAppRunning\s*\n\s*!include "getProcessInfo\.nsh"\s*\n\s*Var pid/,
      "the template still guards its getProcessInfo/Var pid include on the hook being undefined"
    );
    assert.match(installerNsh, /!include "getProcessInfo\.nsh"/);
    assert.match(installerNsh, /^\s*Var pid\s*$/m);
  });

  it("matches only this install's own paths in both the find and the kill predicate", () => {
    // A process is ours iff its path IS $INSTDIR\${APP_EXECUTABLE_FILENAME} or
    // starts with $INSTDIR\resources\. Assert over executable lines only: NSIS
    // comments start with ';', and the comment documenting the bug quotes the
    // bare-$INSTDIR form on purpose.
    const code = source => source
      .split("\n")
      .filter(line => !line.trim().startsWith(";"))
      .join("\n");
    const predicate =
      /\? \{\$\$_\.Path -and \(\$\$_\.Path -ieq '(\$\d)\\\$\{APP_EXECUTABLE_FILENAME\}' -or \$\$_\.Path\.StartsWith\('(\$\d)\\resources\\', 'CurrentCultureIgnoreCase'\)\)\}/;
    for (const name of ["KiroFindAppProcess", "KiroKillAppProcess"]) {
      const macro = code(installerNsh.match(new RegExp(`!macro ${name}\\b[\\s\\S]*?!macroend`))[0]);
      const match = macro.match(predicate);
      assert.ok(match, `${name} must match the app executable or the resources tree`);
      assert.equal(match[1], match[2], `${name} must test both paths against one root`);
      // That root is $INSTDIR with its apostrophes escaped for the PowerShell
      // single-quoted literal, never the raw $INSTDIR.
      assert.match(
        macro,
        new RegExp(`!insertmacro KiroPsSingleQuote "\\$INSTDIR" \\${match[1]}\\n[^\\n]*nsExec::Exec[^\\n]*Win32_Process`),
        `${name} must escape $INSTDIR into ${match[1]} right before the PowerShell call`
      );
      assert.doesNotMatch(macro, /'\$INSTDIR/, `${name} must not interpolate a raw $INSTDIR into a PowerShell literal`);
    }
    // Every StartsWith in executable code is the resources prefix; the bare
    // '$INSTDIR' prefix of the upstream bug must not survive anywhere.
    const starts = [...code(installerNsh).matchAll(/\.StartsWith\('([^']*)'/g)].map(m => m[1]);
    assert.equal(starts.length, 2, "expected exactly the find and kill predicates to test a prefix");
    for (const prefix of starts) assert.match(prefix, /^\$\d\\resources\\$/);
    assert.doesNotMatch(code(installerNsh), /\.StartsWith\('\$INSTDIR'/);
  });

  it("drops the product-leaf guard so a genuine install at any root is still detected", () => {
    // A leaf guard (bail when the install dir's basename is not ${APP_FILENAME})
    // FAILS OPEN: KiroEnsureAppInstallDir keeps a registered, /D or --updated
    // root as-is, so a genuine install at D:\Tools\Kiro would read as "not
    // running" and the update would overwrite files the running app holds.
    for (const name of ["KiroFindAppProcess", "KiroKillAppProcess"]) {
      const macro = normalizedNsisBlock(installerNsh, new RegExp(`!macro ${name}\\b[\\s\\S]*?!macroend`));
      assert.doesNotMatch(macro, /GetFileName/, `${name} must not gate on the install dir's leaf`);
      assert.doesNotMatch(macro, /APP_FILENAME\b/, `${name} must not compare against the product leaf`);
    }
    const find = normalizedNsisBlock(installerNsh, /!macro KiroFindAppProcess\b[\s\S]*?!macroend/);
    assert.match(find, /^!macro KiroFindAppProcess _RETURN\nPush \$0\n\$\{if\} \$IsPowerShellAvailable == 0\n/);
    assert.match(find, /\$\{endIf\}\nPop \$0\n!macroend$/);
    const kill = normalizedNsisBlock(installerNsh, /!macro KiroKillAppProcess\b[\s\S]*?!macroend/);
    assert.match(kill, /^!macro KiroKillAppProcess _FORCE\nPush \$0\nPush \$1\n\$\{if\} \$\{_FORCE\} == 1\n/);
    assert.match(kill, /\$\{endIf\}\nPop \$1\nPop \$0\n!macroend$/);
  });

  it("escapes apostrophes in $INSTDIR for the PowerShell single-quoted literal", () => {
    const helper = normalizedNsisBlock(installerNsh, /!macro KiroPsSingleQuote\b[\s\S]*?!macroend/);
    // Doubling ' to '' is the only escape a PowerShell single-quoted literal has.
    assert.match(helper, /\$\{If\} \$R5 == "'"\nStrCpy \$R3 "\$R3''"\n\$\{Else\}\nStrCpy \$R3 "\$R3\$R5"/);
    // Every register it uses is saved and restored, and the result leaves via
    // the stack, so the caller's $0/$1/$R0/$R1 survive.
    assert.match(helper, /^!macro KiroPsSingleQuote _IN _OUT\nPush \$R2\nPush \$R3\nPush \$R4\nPush \$R5\n/);
    assert.match(helper, /\nPop \$R5\nPop \$R4\nExch \$R3\nExch\nPop \$R2\nPop \$\{_OUT\}\n!macroend$/);
    // Model the helper and the resulting PowerShell literal.
    const psQuote = s => s.replaceAll("'", "''");
    const root = "C:\\Users\\O'Brien\\AppData\\Local\\Programs\\KiroCrew";
    const literal = `'${psQuote(root)}\\resources\\'`;
    assert.equal(literal, "'C:\\Users\\O''Brien\\AppData\\Local\\Programs\\KiroCrew\\resources\\'");
    // Read back as PowerShell does: inside '...', '' is one apostrophe.
    assert.equal(literal.slice(1, -1).replaceAll("''", "'"), root + "\\resources\\");
  });

  it("matches a genuinely running install at any root but not a sibling, nothing, or a dead PID", () => {
    // Model the PowerShell predicate: a LIVE process belongs to the install at
    // root iff its path, case-insensitively, IS root\<app exe> or starts with
    // root\resources\. It reads no stored PID, so a stale PID or lock file
    // naming a dead/reused process is simply never in the live set.
    // Win32_Process.Path is null for some system processes; the `$_.Path -and`
    // guard means a pathless entry never matches.
    const appExe = path.win32.basename("C:\\Programs\\KiroCrew.exe");
    const isOurs = (p, root) => {
      if (typeof p !== "string") return false;
      const lower = p.toLowerCase();
      return lower === `${root}\\${appExe}`.toLowerCase() ||
        lower.startsWith(`${root}\\resources\\`.toLowerCase());
    };
    const anyOursRunning = (livePaths, root) => livePaths.some(p => isOurs(p, root));
    const installDir = "C:\\Users\\me\\AppData\\Local\\Programs\\KiroCrew";
    const nightlyExe = `${installDir} Nightly\\${appExe}`;
    const widenedRoot = "C:\\Users\\me\\AppData\\Local";
    const kiroCli = `${widenedRoot}\\Kiro-Cli\\kiro-cli.exe`;
    const unrelated = ["C:\\Windows\\explorer.exe", null];

    // Stable running: the app window and the bundled backend are both ours.
    for (const live of [
      `${installDir}\\${appExe}`,
      `${installDir}\\resources\\backend-dist\\kiro-cli\\kiro-cli.exe`,
      `${installDir}\\${appExe}`.toLowerCase(), // case-insensitive
    ]) {
      assert.equal(anyOursRunning([...unrelated, live], installDir), true, `a real install process must match: ${live}`);
    }

    // A running nightly sibling shares the prefix and the image name, but is
    // neither of this install's own paths.
    assert.equal(anyOursRunning([...unrelated, nightlyExe], installDir), false);
    assert.equal(isOurs(nightlyExe, `${installDir} Nightly`), true, "the nightly is still ours to its own installer");

    // A widened root: another product directly under it is not the app.
    assert.equal(anyOursRunning([...unrelated, kiroCli, nightlyExe], widenedRoot), false);

    // A GENUINE install at a non-product-leaf root (a registered or /D root) is
    // still detected: the leaf guard that failed open here is gone.
    const toolsRoot = "D:\\Tools\\Kiro";
    assert.notEqual(path.win32.basename(toolsRoot).toLowerCase(), path.win32.basename(installDir).toLowerCase());
    assert.equal(anyOursRunning([...unrelated, `${toolsRoot}\\${appExe}`], toolsRoot), true);
    assert.equal(anyOursRunning([...unrelated, `${toolsRoot}\\resources\\backend-dist\\python.exe`], toolsRoot), true);
    // ...while other software at that root is not.
    assert.equal(anyOursRunning([...unrelated, `${toolsRoot}\\Other\\other.exe`], toolsRoot), false);

    // Nothing running, and a dead or reused PID: the install's process is gone
    // from the live set, or the PID now names an unrelated image elsewhere.
    assert.equal(anyOursRunning([], installDir), false);
    assert.equal(anyOursRunning([...unrelated, nightlyExe, kiroCli], installDir), false, "nothing of ours is running");
    assert.equal(
      anyOursRunning([...unrelated, "C:\\Windows\\System32\\svchost.exe"], installDir),
      false,
      "a reused PID now owned by an unrelated image is not ours"
    );
  });

  it("falls back to upstream's exact app-executable match without PowerShell", () => {
    // The PowerShell-unavailable branch mirrors upstream: tasklist filtered on
    // IMAGENAME eq ${APP_EXECUTABLE_FILENAME} (KiroCrew.exe), anchored with
    // findstr /B so it is an exact CSV match. tasklist has no path filter, so
    // this upstream fallback matches by image name only; it never matches the
    // standalone kiro-cli.exe, and it runs only when PowerShell is unavailable.
    const findMacro = installerNsh.match(/!macro KiroFindAppProcess[\s\S]*?!macroend/)[0];
    assert.match(findMacro, /IMAGENAME eq \$\{APP_EXECUTABLE_FILENAME\}/);
    assert.match(findMacro, /findstr\.exe" \/B \/I \/C:"\\"\$\{APP_EXECUTABLE_FILENAME\}\\""/);
    assert.doesNotMatch(findMacro, /kiro-cli/i, "the fallback must not match a bare kiro-cli name");
    // The kill fallback is likewise scoped to the app executable and excludes
    // the installer's own process, as upstream does.
    const killMacro = installerNsh.match(/!macro KiroKillAppProcess[\s\S]*?!macroend/)[0];
    assert.match(killMacro, /taskkill[^\n]*\/IM "\$\{APP_EXECUTABLE_FILENAME\}"[^\n]*\/FI "PID ne \$pid"/);
  });

  it("allows only upstream's app-close waits and no Sleep elsewhere", () => {
    const macroPattern = /^!macro (?:customCheckAppRunning|KiroFindAppProcess|KiroKillAppProcess)\b[\s\S]*?^!macroend/gm;
    const macros = [...installerNsh.matchAll(macroPattern)].map(match => match[0]);
    assert.equal(macros.length, 3);
    const sleepArguments = source => [...source.matchAll(/^\s*Sleep\b([^\r\n]*)/gim)]
      .map(match => match[1].trim());
    const upstream = normalizedNsisBlock(appRunning, /!macro _CHECK_APP_RUNNING\b[\s\S]*?!macroend/);
    // Five call sites, with three distinct delays. Pin the sequence as well as
    // its upstream source: neither a new wait nor a changed delay is exempt.
    const upstreamWaits = sleepArguments(upstream);
    assert.deepEqual(upstreamWaits, ["300", "1000", "300", "1000", "2000"]);
    const appCloseWaits = sleepArguments(macros.join("\n"));
    assert.deepEqual(appCloseWaits, upstreamWaits);
    assert.deepEqual(sleepArguments(installerNsh), appCloseWaits, "no Sleep outside the app-close macros");
  });

  it("pins the upstream bare-prefix match and mirrored retry contract", () => {
    for (const name of ["FIND_PROCESS", "KILL_PROCESS"]) {
      const upstream = normalizedNsisBlock(appRunning, new RegExp(`!macro ${name}\\b[\\s\\S]*?!macroend`));
      assert.ok(
        upstream.includes("$$_.Path.StartsWith('$INSTDIR', 'CurrentCultureIgnoreCase')"),
        "upstream no longer uses the bare prefix -- re-derive the override"
      );
    }
    const flow = (source, pattern) => normalizedNsisBlock(source, pattern)
      .split("\n")
      .filter(line => !/^[;#]/.test(line))
      .map(line => line.replace(/\s+#.*$/, ""))
      .join("\n");
    const upstream = flow(appRunning, /!macro _CHECK_APP_RUNNING\b[\s\S]*?!macroend/);
    const ours = flow(installerNsh, /!macro customCheckAppRunning\b[\s\S]*?!macroend/)
      .replace("!macro customCheckAppRunning", "!macro _CHECK_APP_RUNNING")
      .replace("!insertmacro IS_POWERSHELL_AVAILABLE\n", "")
      .replaceAll("KiroFindAppProcess", 'FIND_PROCESS "${APP_EXECUTABLE_FILENAME}"')
      .replaceAll("KiroKillAppProcess", 'KILL_PROCESS "${APP_EXECUTABLE_FILENAME}"')
      .replaceAll("kiroDoStopProcess", "doStopProcess")
      .replaceAll("kiroCheckLoop", "loop")
      .replaceAll("kiroNotRunning", "not_running");
    assert.equal(ours, upstream, "app-close retries and prompts must stay aligned with upstream");
    assert.match(upstream, /MessageBox MB_RETRYCANCEL\|MB_ICONEXCLAMATION "\$\(appCannotBeClosed\)" \/SD IDCANCEL IDRETRY loop/);
  });

  it("preserves upstream's retry-then-prompt flow so a real stuck app still surfaces the prompt", () => {
    // Narrowing the match must not drop the behavior that a genuinely running
    // app the installer cannot close still asks the user: the retry loop and
    // the $(appCannotBeClosed) prompt stay, driven by the narrowed find/kill.
    const macro = installerNsh.match(/!macro customCheckAppRunning[\s\S]*?!macroend/)[0];
    assert.match(macro, /MessageBox MB_RETRYCANCEL\|MB_ICONEXCLAMATION "\$\(appCannotBeClosed\)"/);
    assert.match(macro, /!insertmacro KiroFindAppProcess \$R0/);
    assert.match(macro, /!insertmacro KiroKillAppProcess 1/);
    assert.match(macro, /\$\{if\} \$\{isUpdated\}[\s\S]*?Goto kiroDoStopProcess/);
  });

  it("compiles the running-app check into the uninstaller too, not only the installer", () => {
    // electron-builder compiles this header twice, once with BUILD_UNINSTALLER
    // defined. The uninstaller's un.checkAppRunning runs CHECK_APP_RUNNING from
    // un.onInit under /S (how an update runs the old uninstaller), so if the hook
    // sat inside `!ifndef BUILD_UNINSTALLER` the uninstaller would fall back to
    // upstream's bare-prefix check and could still raise the false prompt.
    const uninstallerTemplate = fs.readFileSync(
      path.join(ROOT_LOCAL, "node_modules", "app-builder-lib", "templates", "nsis", "uninstaller.nsh"),
      "utf8"
    );
    assert.match(uninstallerTemplate, /Function un\.checkAppRunning\s*\n\s*!insertmacro CHECK_APP_RUNNING/);
    // getProcessInfo.nsh provides the uninstaller-side function ${GetProcessInfo}
    // calls when BUILD_UNINSTALLER is defined, so including it once serves both.
    const getProcessInfo = fs.readFileSync(
      path.join(ROOT_LOCAL, "node_modules", "app-builder-lib", "templates", "nsis", "include", "getProcessInfo.nsh"),
      "utf8"
    );
    assert.match(getProcessInfo, /!ifdef BUILD_UNINSTALLER\s*\n\s*Function un\._GetProcessInfo/);

    // The preprocessor conditionals enclosing each line, ignoring comments.
    const enclosing = new Map();
    const stack = [];
    for (const raw of installerNsh.split("\n")) {
      const line = raw.trim();
      if (line.startsWith(";") || line.startsWith("#")) continue;
      if (/^!if/.test(line)) {
        stack.push(line);
        continue;
      }
      if (/^!endif\b/.test(line)) {
        stack.pop();
        continue;
      }
      if (!enclosing.has(line)) enclosing.set(line, [...stack]);
    }
    assert.equal(stack.length, 0, "installer.nsh preprocessor conditionals must balance");
    const guardsOf = line => {
      assert.ok(enclosing.has(line), `expected a top-level line: ${line}`);
      return enclosing.get(line);
    };
    for (const line of [
      '!include "getProcessInfo.nsh"',
      "Var pid",
      "!macro customCheckAppRunning",
      "!macro KiroPsSingleQuote _IN _OUT",
      "!macro KiroFindAppProcess _RETURN",
      "!macro KiroKillAppProcess _FORCE",
    ]) {
      assert.deepEqual(guardsOf(line), [], `${line} must be compiled in both builds, outside every conditional`);
    }
    // The installer-only guard still exists and still holds the installer-only
    // code, so the check above is not passing on a file that lost its guard.
    assert.deepEqual(guardsOf("!macro customInit"), ["!ifndef BUILD_UNINSTALLER"]);
    // One `Var pid` only: allowOnlyOneInstallerInstance.nsh declares its own
    // solely when the hook is undefined, which is now never.
    assert.equal([...installerNsh.matchAll(/^\s*Var pid\s*$/gm)].length, 1);
  });

  it("gives the Windows runtime check the shipped probe programs to run", () => {
    // test-windows-installer.ps1 reads the PowerShell program out of each macro's
    // own nsExec line and runs it against a live process, so the Windows job
    // tests the shipped text. Mirror its extraction here: if a macro is renamed or
    // its probe line changes shape, this fails on every platform instead of only
    // on the Windows runner.
    const runtimeHarness = fs.readFileSync(
      path.join(REPO_ROOT, ".github", "scripts", "test-windows-installer.ps1"),
      "utf8"
    );
    assert.match(
      runtimeHarness,
      /Get-RunningAppPredicate \(Join-Path \(Get-Location\) "build\\installer\.nsh"\) "KiroFindAppProcess" '\$0'/
    );
    assert.match(
      runtimeHarness,
      /"node_modules\\app-builder-lib\\templates\\nsis\\include\\allowOnlyOneInstallerInstance\.nsh"\s*\) "FIND_PROCESS" '\$INSTDIR'/
    );
    const dir = "D:\\a\\_temp\\kci-1\\O'Brien\\KiroCrew";
    const probe = (source, macro, token) => {
      const body = source.match(new RegExp(`^!macro ${macro}\\b[\\s\\S]*?^!macroend`, "m"));
      assert.ok(body, `!macro ${macro} exists`);
      const line = body[0].match(/"\$PowerShellPath" -C "([^\r\n]*)"`/);
      assert.ok(line, `!macro ${macro} has a PowerShell probe line`);
      return line[1]
        .split(token)
        .join(dir.replace(/'/g, "''"))
        .split("${APP_EXECUTABLE_FILENAME}")
        .join("KiroCrew.exe")
        .split("$$")
        .join("$");
    };
    const quoted = dir.replace(/'/g, "''");
    const own = probe(installerNsh, "KiroFindAppProcess", "$0");
    assert.ok(own.includes(`$_.Path -ieq '${quoted}\\KiroCrew.exe'`), own);
    assert.ok(own.includes(`$_.Path.StartsWith('${quoted}\\resources\\', 'CurrentCultureIgnoreCase')`), own);
    assert.match(own, /\{ exit 0 \} else \{ exit 1 \}$/);
    // One matching process must count: Windows PowerShell 5.1 gives a bare
    // single CimInstance no .Count, so the matches are wrapped in @(...).
    assert.match(own, /^if \(@\(Get-CimInstance -ClassName Win32_Process \| \? \{/);
    assert.match(own, /\}\)\.Count -gt 0\)/);
    assert.doesNotMatch(own, /\$\$|\$0|\$\{/);
    const upstream = probe(appRunning, "FIND_PROCESS", "$INSTDIR");
    assert.ok(upstream.includes(`$_.Path.StartsWith('${quoted}', 'CurrentCultureIgnoreCase')`), upstream);
    assert.match(upstream, /\{ exit 0 \} else \{ exit 1 \}$/);
  });
});
