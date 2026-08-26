# ──────────────────────────────────────────────────────────────────────
# Playwright CLI installer for Windows (no pre-existing Node/npm required).
#
#   irm https://raw.githubusercontent.com/kirodotdev/KiroCrew/main/playwright-cli.ps1 | iex
#
# Read before you run (many enterprises forbid piping a script into a shell):
#   irm https://raw.githubusercontent.com/kirodotdev/KiroCrew/main/playwright-cli.ps1 -OutFile playwright-cli.ps1
#   notepad playwright-cli.ps1
#   powershell -ExecutionPolicy Bypass -File .\playwright-cli.ps1 -Version 0.1.18
#
# The PowerShell twin of playwright-cli.sh: same flags in PowerShell spelling,
# same exit codes, same enterprise-network diagnostics. Installs the
# @playwright/cli npm package into a PRIVATE prefix (no admin rights, no writes
# outside the user profile) and writes a playwright-cli.cmd wrapper. When Node
# is missing or below the package's floor it downloads the release build for the
# detected architecture and verifies it against that release's SHASUMS256
# manifest before running it.
#
# Exit codes:
#    0  success                 12  Node checksum mismatch
#    1  unclassified failure    13  registry rejected auth (login/token needed)
#    2  usage error             14  registry unreachable (DNS/proxy/TLS)
#   10  missing prerequisite    15  package or version does not exist
#   11  Node bootstrap failed   16  browser download blocked
#                               17  prefix or bin dir not writable
#                               18  installed CLI failed to run
# ──────────────────────────────────────────────────────────────────────
param(
    [string]$Version = "latest",
    [string]$Registry = "",
    [switch]$IsolatedNpmrc,
    [string]$NodeVersion = "22.23.2",
    [string]$NodeMirror = "",
    [string]$DownloadHost = "",
    [switch]$SkipBrowsers,
    [string]$Prefix = "",
    [string]$BinDir = "",
    [switch]$Force,
    [switch]$DryRun,
    [switch]$Help
)

$ErrorActionPreference = "Stop"

# Windows PowerShell 5.1 still negotiates TLS 1.0 by default, which every
# registry and CDN in this script now refuses.
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {
    # PowerShell 7 manages this itself and the type may be unavailable.
}

$Self = "playwright-cli-install"

# The package this installer exists to deliver.
$Package = "@playwright/cli"
$WrapperName = "playwright-cli"

# The public registry is pinned rather than inherited, because a corporate
# .npmrc that redirects the DEFAULT registry at a private mirror makes a PUBLIC
# package 401 as soon as that mirror's token expires.
$PublicNpmRegistry = "https://registry.npmjs.org/"
# The floor that matters is not the package's own engines.node (>= 18) but the one
# Kiro Crew's browsing requires of this CLI: MIN_NODE_MAJOR in
# src/kiro_crew/browser_cli/install.py. Accepting less would install a CLI the
# product refuses to drive. A test binds the two together. A bootstrap installs
# Node 22 LTS.
$MinNodeMajor = 20
$NodeOfficialMirror = "https://nodejs.org/dist"
# Written into a Node tree this installer unpacked. Its ABSENCE is what stops the
# bootstrap from recursively deleting a `node` directory it did not create
# (reachable with -Prefix $HOME on a machine that also has ~\node).
$NodeStampName = ".kirocrew-playwright-cli-node"

$ExUsage = 2
$ExMissingTool = 10
$ExNodeBootstrap = 11
$ExChecksum = 12
$ExRegistryAuth = 13
$ExRegistryUnreachable = 14
$ExPackageNotFound = 15
$ExBrowserDownload = 16
$ExNotWritable = 17
$ExVerify = 18

# Every native invocation runs through here. Under the global 'Stop' preference,
# Windows PowerShell 5.1 turns a native command's stderr into a terminating
# NativeCommandError -- so a Node that merely prints a startup warning (a
# NODE_OPTIONS notice, say) would abort the installer. Native commands are judged
# on their exit code, never on whether they wrote to stderr.
function Invoke-Native([scriptblock]$Action) {
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $Action
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Say([string]$Message) { Write-Host "${Self}: $Message" }
function Warn([string]$Message) { [Console]::Error.WriteLine("${Self}: $Message") }

# Every failure exits through here, so each exit code is deliberate and
# documented in Show-Usage rather than being an incidental $LASTEXITCODE.
function Die([int]$Code, [string]$Message) {
    [Console]::Error.WriteLine("${Self}: $Message")
    exit $Code
}

function Show-Usage {
    Write-Host @"
Install the Playwright CLI (@playwright/cli), bootstrapping Node if needed.

Usage: powershell -ExecutionPolicy Bypass -File .\playwright-cli.ps1 [options]

Options:
  -Version <X.Y.Z>       package version to install (default: latest)
  -Registry <url>        npm registry to install FROM (default: the public
                         registry, so an ambient .npmrc pointing at an expired
                         private mirror cannot break a public package). Point
                         this at a corporate mirror when the public registry is
                         unreachable.
  -IsolatedNpmrc         ignore the user and global npm config entirely
  -NodeVersion <X.Y.Z>   Node version to bootstrap when none is usable
  -NodeMirror <url>      base URL serving <base>/v<ver>/<file> (default nodejs.org)
  -DownloadHost <url>    PLAYWRIGHT_DOWNLOAD_HOST for the browser binaries
  -SkipBrowsers          do not download browser binaries during install
  -Prefix <dir>          private install prefix
  -BinDir <dir>          where the playwright-cli.cmd wrapper is written
  -Force                 reinstall even when the pinned version is present
  -DryRun                print the resolved plan and exit without changes
  -Help                  this text

Environment:
  KIROCREW_HOME                 data home (default ~\.kiro\crew)
  KIROCREW_PLAYWRIGHT_CLI_HOME  overrides -Prefix
  KIROCREW_NPM_REGISTRY         overrides -Registry
  KIROCREW_NODE_BIN_DIR         an existing Node bin dir to reuse
  HTTPS_PROXY                   honored by npm and the Node download
  NO_PROXY                      honored by npm only
  NODE_EXTRA_CA_CERTS           CA bundle for a TLS-terminating proxy

Exit codes:
   0  success                 12  Node checksum mismatch
   1  unclassified failure    13  registry rejected auth (login/token needed)
   2  usage error             14  registry unreachable (DNS/proxy/TLS)
  10  missing prerequisite    15  package or version does not exist
  11  Node bootstrap failed   16  browser download blocked
                              17  prefix or bin dir not writable
                              18  installed CLI failed to run
"@
}

if ($Help) { Show-Usage; exit 0 }

# $IsWindows exists only in PowerShell 6+; 5.1 is Windows-only and sets $env:OS.
$onWindows = if ($null -ne (Get-Variable -Name IsWindows -ErrorAction SilentlyContinue)) {
    $IsWindows
} else {
    $env:OS -eq "Windows_NT"
}
if (-not $onWindows) {
    Die $ExUsage "this script targets Windows; on macOS and Linux run playwright-cli.sh instead"
}

# ── argument validation ──────────────────────────────────────────────
# The version string reaches both an npm spec and a URL path.
if ($Version -notmatch '^[A-Za-z0-9._+-]+$') {
    Die $ExUsage "invalid -Version '$Version'"
}
if ($NodeVersion -notmatch '^[0-9]+(\.[0-9]+)*$') {
    Die $ExUsage "invalid -NodeVersion '$NodeVersion' (expected X.Y.Z)"
}

if ([string]::IsNullOrWhiteSpace($Registry)) {
    $Registry = if ($env:KIROCREW_NPM_REGISTRY) { $env:KIROCREW_NPM_REGISTRY } else { $PublicNpmRegistry }
}

if ([string]::IsNullOrWhiteSpace($DownloadHost)) {
    # `npx playwright install` inherits PLAYWRIGHT_DOWNLOAD_HOST from this
    # process regardless of what the parameter holds, so the ambient value has
    # to land in the parameter to reach Require-Https below. Left empty, an
    # http:// mirror already in the environment would be validated by nothing.
    $DownloadHost = if ($env:PLAYWRIGHT_DOWNLOAD_HOST) { $env:PLAYWRIGHT_DOWNLOAD_HOST } else { "" }
}

# Bytes are only ever fetched over TLS: an http:// mirror would let anything on
# the path substitute its own Node tarball, and the SHASUMS manifest travels the
# same channel it is supposed to protect.
# Any URL this script prints goes through here first: three of the four
# URL-valued parameters are caller-supplied and may legitimately carry userinfo,
# so redaction is a property of PRINTING a URL, not of one parameter.
# Two shapes carry a credential: USERINFO (`//user:token@host`) and a QUERY
# (`?token=...`), which is how some mirrors authenticate instead. The query is
# replaced wholesale from its first `?` rather than by parameter name, because
# naming the sensitive parameters would miss the next one, and it is anchored on
# the scheme so an ordinary `?` in npm's prose is left alone.
function Redact-Url([string]$Url) {
    $redacted = $Url -replace '//[^/\s]*@', '//***@'
    return ($redacted -replace '(https?://[^?\s]*)\?\S*', '$1?***')
}
function Require-Https([string]$Label, [string]$Url) {
    if ($Url -notmatch '^https://') {
        Die $ExUsage "$Label must be an https:// URL (got '$(Redact-Url $Url)')"
    }
}
Require-Https "-Registry" $Registry
# A registry URL may legitimately carry userinfo (https://user:token@host/), and
# the documented enterprise path invites exactly that. Anything this script
# prints uses the redacted form, because the failure path echoes it and also
# dumps npm's log, where a terminal or CI log would capture the token.
$RegistryDisplay = Redact-Url $Registry
$DownloadHostDisplay = Redact-Url $DownloadHost

# npm echoes the resolved registry URL into its own output, so redacting only the
# values THIS script prints would still leak a token through the log and the tail
# printed on failure. Rewrite the captured log itself, which covers both.
# Set when the log could not be scrubbed, so Show-LogTail refuses to print it
# instead of deciding from whether the file still exists.
$script:LogSuppressed = $false

function Redact-Log([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    try {
        $raw = Get-Content -LiteralPath $Path -Raw
        if ($null -eq $raw) { return }
        $scrubbed = $raw -replace '//[^/\s]*@', '//***@'
        $scrubbed = $scrubbed -replace '(https?://[^?\s]*)\?\S*', '$1?***'
        # A credential does not only arrive URL-shaped: npm echoes .npmrc
        # assignments in its own output, and this log is both kept on disk and
        # tailed to the terminal on failure. `_auth` is last and requires the `=`
        # immediately after it, so it cannot truncate `_authToken=`.
        $scrubbed = $scrubbed -replace '(_authToken|_password|_auth|NPM_TOKEN)=\S*', '$1=***'
        Set-Content -LiteralPath $Path -Value $scrubbed -NoNewline
    } catch {
        # A log we cannot rewrite must not be printed at all. Deleting it is only
        # best effort -- a file locked by a scanner survives the Remove-Item and
        # the token with it -- so the decision is RECORDED here rather than
        # inferred by Show-LogTail from the file being gone. Inferring it is how
        # this fails open: the delete quietly fails, the file is still there, and
        # the tail prints the unredacted token.
        $script:LogSuppressed = $true
        Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    }
}
if ($NodeMirror) { Require-Https "-NodeMirror" $NodeMirror }
# Same reasoning as the shell installer: the mirror URL is passed to
# Invoke-WebRequest as an argument, and a process command line is readable by other
# principals (Win32_Process exposes it to an administrator, and to the same user's
# other sessions). Refused rather than redacted -- redaction covers what this script
# prints, not what the OS reports about it. The registry URL travels in the
# environment instead, so it keeps its support for userinfo.
# A credential passed as a PARAMETER sits in this script's own command line, which
# other principals on the host can read (Win32_Process exposes it to an
# administrator, and to the same user's other sessions) -- and it lands in
# PSReadLine history besides. The environment is the supported route.
#
# Provenance, not value: `$Registry` also holds an env-supplied value, and refusing
# that would break the very escape this message recommends. `$PSBoundParameters` is
# what distinguishes "the caller typed it" from "it came from the environment".
function Deny-UrlCredential([string]$Label, [string]$Url, [string]$Alternative) {
    if ([string]::IsNullOrWhiteSpace($Url)) { return }
    $authority = ($Url -replace '^[^:]+://', '') -replace '/.*$', ''
    if ($authority.Contains("@")) {
        Die $ExUsage ("$Label may not embed a credential: the URL becomes part of this " +
            "script's command line, which other accounts on this host can read. $Alternative")
    }
    # Wholesale, not by parameter name: naming today's `?token=` would miss
    # tomorrow's `?sig=`, which is why the log sanitiser also replaces a whole query.
    if ($Url.Contains("?")) {
        Die $ExUsage ("$Label may not carry a query string: it becomes part of this " +
            "script's command line, where a token in it is readable by other accounts " +
            "on this host. $Alternative")
    }
}
if ($PSBoundParameters.ContainsKey("Registry")) {
    Deny-UrlCredential "-Registry" $Registry ("Set KIROCREW_NPM_REGISTRY in the " +
        "environment instead, or run 'npm login --registry <url>' first.")
}
if ($PSBoundParameters.ContainsKey("DownloadHost")) {
    Deny-UrlCredential "-DownloadHost" $DownloadHost ("Set PLAYWRIGHT_DOWNLOAD_HOST in " +
        "the environment instead; this script passes an already-set value through.")
}
if ($NodeMirror) {
    Deny-UrlCredential "-NodeMirror" $NodeMirror ("Use a proxy, or the mirror's own " +
        "authentication.")
}
if ($DownloadHost) { Require-Https "-DownloadHost" $DownloadHost }

$dataHome = if ($env:KIROCREW_HOME) { $env:KIROCREW_HOME } else { Join-Path $HOME ".kiro\crew" }
if ([string]::IsNullOrWhiteSpace($Prefix)) {
    $Prefix = if ($env:KIROCREW_PLAYWRIGHT_CLI_HOME) {
        $env:KIROCREW_PLAYWRIGHT_CLI_HOME
    } else {
        Join-Path $dataHome "playwright-cli"
    }
}
if ([string]::IsNullOrWhiteSpace($BinDir)) {
    $BinDir = Join-Path $HOME ".local\bin"
}
$Prefix = $Prefix.TrimEnd('\', '/')
$BinDir = $BinDir.TrimEnd('\', '/')

# TrimEnd strips every trailing separator, so a root collapses: "C:\" becomes
# "C:" and "\" becomes "". Neither is harmless. An empty string resolves against
# the current location, and "C:" is DRIVE-RELATIVE -- it means "the working
# directory on C:", not the drive root -- so either would silently install into
# the working directory instead of the root that was named. Refused rather than
# preserved, for the same reason as the shell installer: the contract is a
# private prefix inside the user profile, which no root satisfies.
if ([string]::IsNullOrEmpty($Prefix) -or $Prefix -match '^[A-Za-z]:$') {
    Die $ExUsage "-Prefix may not be a filesystem root"
}
if ([string]::IsNullOrEmpty($BinDir) -or $BinDir -match '^[A-Za-z]:$') {
    Die $ExUsage "-BinDir may not be a filesystem root"
}

# `;` separates PATH entries on Windows and PATH has no escaping mechanism, so a
# bootstrapped Node under such a prefix would be prepended as two nonexistent
# entries and npm's own shim would not find the interpreter just installed. A
# drive colon is fine and expected -- it is `;` that cannot survive. Checked after
# the root test so `C:` is already rejected by the more specific message.
foreach ($pair in @(@("-Prefix", $Prefix), @("-BinDir", $BinDir))) {
    if ($pair[1].Contains(";")) {
        Die $ExUsage "$($pair[0]) may not contain ';', which separates PATH entries"
    }
}

# Both are written into a .cmd wrapper that outlives this run, so a relative one
# ("-Prefix .\pw") would resolve against the caller's directory and the wrapper
# would work from there and nowhere else. GetUnresolvedProviderPathFromPSPath is
# used rather than [IO.Path]::GetFullPath because the latter resolves against
# [Environment]::CurrentDirectory, which in PowerShell is NOT kept in step with
# the session's own location -- so it would silently anchor to the wrong
# directory. This one also does not require the path to exist yet.
function Get-AbsolutePath([string]$Path) {
    return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
}
$Prefix = Get-AbsolutePath $Prefix
$BinDir = Get-AbsolutePath $BinDir

# ── platform detection ───────────────────────────────────────────────
# PROCESSOR_ARCHITECTURE reports the architecture of the *current process*, so a
# 32-bit PowerShell on an ARM64 machine would claim x86 and download a Node that
# cannot run natively. RuntimeInformation reports the OS instead.
function Get-NodeArch {
    try {
        switch ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture) {
            "Arm64" { return "arm64" }
            "X64"   { return "x64" }
            "X86"   { return "x86" }
        }
    } catch {
        # .NET Framework builds without RuntimeInformation fall through.
    }
    if ($env:PROCESSOR_ARCHITEW6432 -eq "ARM64" -or $env:PROCESSOR_ARCHITECTURE -eq "ARM64") { return "arm64" }
    if ($env:PROCESSOR_ARCHITEW6432 -or $env:PROCESSOR_ARCHITECTURE -eq "AMD64") { return "x64" }
    return "x86"
}
$nodeArch = Get-NodeArch
if ($nodeArch -eq "x86") {
    Die $ExMissingTool "32-bit Windows is not supported by current Node releases"
}

# ── locate a usable Node ─────────────────────────────────────────────
$script:NodeExe = ""
$script:NodeBinDir = ""
$script:NpmCmd = ""
$script:NpmCliJs = ""
# Set by Install-Node so the npm fallback cannot loop: no npm in a tree we just
# unpacked means a broken archive, not a reusable-Node problem.
$script:NodeWasBootstrapped = $false

function Try-Node([string]$Candidate) {
    if ([string]::IsNullOrWhiteSpace($Candidate)) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    # Parity with the .sh guard against ':' in a candidate's directory. ';' is legal
    # in an NTFS name and is also the PATH separator, so accepting it would splice
    # bogus entries into the npm subprocess PATH and into the generated .cmd
    # wrapper. Reachable via KIROCREW_NODE_BIN_DIR, the node-bin-dir marker, or a
    # PATH lookup; $Prefix is already screened for it upstream.
    if ((Split-Path -Parent $Candidate) -match ';') { return $false }
    # $ErrorActionPreference is 'Stop' globally, and Windows PowerShell 5.1 wraps
    # a native command's stderr as a NativeCommandError that 'Stop' promotes to
    # terminating. A Node that writes any warning to stderr during this probe
    # would then be classed unusable and a second Node downloaded, so the probe
    # runs at 'Continue' and is judged on its exit code alone.
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $major = & $Candidate -p 'process.versions.node.split(".")[0]' 2>$null
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($LASTEXITCODE -ne 0 -or $major -notmatch '^[0-9]+$') { return $false }
    if ([int]$major -lt $MinNodeMajor) { return $false }
    $script:NodeExe = $Candidate
    # Embedded in the generated wrapper too, and every candidate funnels through
    # here -- $env:KIROCREW_NODE_BIN_DIR, the marker file and a PATH lookup can
    # each be relative.
    $script:NodeBinDir = Get-AbsolutePath (Split-Path -Parent $Candidate)
    return $true
}

# Preference order, most specific first: a Node this installer bootstrapped
# earlier, then one the caller named, then whatever is on PATH.
function Resolve-Node {
    if (Try-Node (Join-Path $Prefix "node\node.exe")) { return $true }
    if ($env:KIROCREW_NODE_BIN_DIR -and (Try-Node (Join-Path $env:KIROCREW_NODE_BIN_DIR "node.exe"))) { return $true }
    $marker = Join-Path $dataHome "node-bin-dir"
    if (Test-Path -LiteralPath $marker -PathType Leaf) {
        # An empty marker yields $null, and .Trim() on it throws -- which under the
        # global 'Stop' preference would abort before PATH or the bootstrap is even
        # considered. The cast makes an empty file read as an empty string.
        $marked = ([string](Get-Content -LiteralPath $marker -TotalCount 1)).Trim()
        if ($marked -and (Try-Node (Join-Path $marked "node.exe"))) { return $true }
    }
    $onPath = Get-Command node -ErrorAction SilentlyContinue
    if ($onPath -and (Try-Node $onPath.Source)) { return $true }
    return $false
}

# npm ships beside node.exe as npm.cmd; a PATH-provided node may have it
# elsewhere on PATH (nvm-windows, Chocolatey, Scoop).
# npm.cmd is a BATCH file, so running it means cmd.exe parses a command line --
# and cmd expands %VAR% in that line, including inside the paths we pass it. A
# directory legitimately named `%PATH%` (legal on NTFS) would therefore be
# rewritten before npm ever saw it. npm.cmd exists only to hand npm's own
# npm-cli.js to node, so when that file can be found the installer calls node
# with it directly and cmd.exe is out of the picture. When it CANNOT be found --
# an npm laid out unlike the bundled-Node/global-npm shape this expects -- the
# fallback does invoke npm.cmd, and cmd.exe does parse that line. That is
# tolerable only because the single argument passed there is the package spec,
# which is charset-validated upstream and cannot contain '%'. It is a property of
# the input, not a guarantee the code structure provides.
function Resolve-NpmCliJs {
    $script:NpmCliJs = ""
    foreach ($base in @($script:NodeBinDir, (Split-Path -Parent $script:NpmCmd))) {
        if ([string]::IsNullOrWhiteSpace($base)) { continue }
        $candidate = Join-Path $base "node_modules\npm\bin\npm-cli.js"
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $script:NpmCliJs = $candidate
            return
        }
    }
}

function Resolve-Npm {
    $beside = Join-Path $script:NodeBinDir "npm.cmd"
    if (Test-Path -LiteralPath $beside -PathType Leaf) {
        $script:NpmCmd = $beside
        Resolve-NpmCliJs
        return $true
    }
    $onPath = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $onPath) { $onPath = Get-Command npm -ErrorAction SilentlyContinue }
    if ($onPath) {
        $script:NpmCmd = $onPath.Source
        Resolve-NpmCliJs
        return $true
    }
    return $false
}

function Get-NodeArtifact {
    $mirror = if ($NodeMirror) { $NodeMirror.TrimEnd('/') } else { $NodeOfficialMirror }
    return [pscustomobject]@{
        Mirror = $mirror
        Base   = "node-v$NodeVersion-win-$nodeArch"
    }
}

function Install-Node {
    $script:NodeWasBootstrapped = $true
    $artifact = Get-NodeArtifact
    $zipName = "$($artifact.Base).zip"
    $zipUrl = "$($artifact.Mirror)/v$NodeVersion/$zipName"
    $sumsUrl = "$($artifact.Mirror)/v$NodeVersion/SHASUMS256.txt"

    Say "no usable Node found (need >= $MinNodeMajor); installing Node $NodeVersion for win-$nodeArch"
    # Staging lives under $Prefix, not %TEMP%: Move-Item cannot move a directory
    # across volumes, and TEMP=D:\Temp with a C:\ prefix would abort the
    # bootstrap after the download was already verified.
    $staging = Join-Path $Prefix ("node.incoming-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    try {
        $zipPath = Join-Path $staging $zipName
        $sumsPath = Join-Path $staging "SHASUMS256.txt"
        try {
            # -MaximumRedirection 0 is the fail-closed posture: Invoke-WebRequest
            # otherwise follows up to five hops with no way to require that each
            # stays HTTPS, and it is the checksum MANIFEST travelling this
            # channel -- one 302 to http:// would let an attacker supply both the
            # archive and the hash that blesses it. Both default mirrors serve
            # these paths directly, so a redirect means the mirror is not the one
            # this contract describes.
            # Passed explicitly because Invoke-WebRequest on 5.1 (.NET Framework)
            # reads only the WinINet/system proxy and ignores HTTPS_PROXY entirely --
            # so without this, the enterprise Windows user this installer exists for
            # cannot fetch Node at all when their proxy is configured only in the
            # environment. NO_PROXY has no Invoke-WebRequest equivalent and is
            # documented as npm-only in the usage banner.
            $webArgs = @{ UseBasicParsing = $true; MaximumRedirection = 0 }
            $proxy = $env:HTTPS_PROXY
            if (-not $proxy) { $proxy = $env:https_proxy }
            if ($proxy) { $webArgs["Proxy"] = $proxy }
            Invoke-WebRequest -Uri $sumsUrl -OutFile $sumsPath @webArgs
            Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath @webArgs
        } catch {
            Die $ExNodeBootstrap ("could not download Node from $(Redact-Url $zipUrl) - check proxy access, or pass " +
                "-NodeMirror pointing at an internal Node mirror that serves the artifact without a " +
                "redirect. $($_.Exception.Message)")
        }

        $wanted = $null
        foreach ($line in Get-Content -LiteralPath $sumsPath) {
            $fields = $line -split '\s+', 2
            if ($fields.Count -eq 2 -and $fields[1].Trim().TrimStart('.', '/', '*') -eq $zipName) {
                $wanted = $fields[0].Trim()
                break
            }
        }
        if (-not $wanted) {
            Die $ExChecksum "$zipName is absent from $(Redact-Url $sumsUrl) - refusing to install an unverified Node"
        }
        $got = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($got -ne $wanted.ToLowerInvariant()) {
            Die $ExChecksum "Node checksum mismatch for $zipName (expected $wanted, got $got) - refusing to install"
        }
        Say "verified Node SHA-256"

        Expand-Archive -LiteralPath $zipPath -DestinationPath $staging -Force
        $extracted = Join-Path $staging $artifact.Base
        # BOTH binaries, before anything is promoted. The shell installer has always
        # required `bin/node` and `bin/npm`; this side checked only node.exe, so a
        # checksum-valid archive from a custom mirror that omitted npm would promote,
        # destroy the stamped Node it replaced, and only then fail npm resolution --
        # with the rollback already gone. A missing npm here is a broken archive, and
        # it costs nothing to say so while the old tree is still untouched.
        foreach ($required in @("node.exe", "npm.cmd")) {
            if (-not (Test-Path -LiteralPath (Join-Path $extracted $required) -PathType Leaf)) {
                Die $ExNodeBootstrap "the Node archive is missing $required"
            }
        }

        # -Prefix is caller-supplied, so $Prefix\node can name a directory this
        # installer never created. Only a tree carrying our stamp may be replaced.
        $target = Join-Path $Prefix "node"
        if (Test-Path -LiteralPath $target) {
            if (-not (Test-Path -LiteralPath (Join-Path $target $NodeStampName) -PathType Leaf)) {
                Die $ExNodeBootstrap ("$target already exists and was not created by this installer; " +
                    "move it aside or pass a different -Prefix")
            }
        }
        New-Item -ItemType File -Path (Join-Path $extracted $NodeStampName) -Force | Out-Null

        # The existing tree is MOVED aside, not deleted, before the new one is
        # promoted. Deleting first leaves a window in which no Node exists at all,
        # and an interruption inside it takes out a working install -- the wrapper
        # pins this exact directory, so the user's CLI stops running until they
        # reinstall. Reachable on an ordinary path now that a stamped, runnable Node
        # with no npm is re-bootstrapped rather than reused. The backup is restored
        # if promotion fails and removed only once the new tree is in place.
        # PROBED BEFORE PROMOTION. A checksum-valid archive can still hold a Node
        # that does not run here, and probing after promotion left an interval in
        # which an UNVALIDATED tree was in place while the old one sat in staging --
        # so a Ctrl-C during the probe ran `finally`, which saw the target present,
        # declined to restore, and deleted the rollback. Probing the staged tree
        # removes that interval instead of guarding it.
        if (-not (Try-Node (Join-Path $extracted "node.exe"))) {
            Die $ExNodeBootstrap ("the Node just downloaded does not run on this host " +
                "(wrong architecture for $($artifact.Base))")
        }

        $backup = ""
        if (Test-Path -LiteralPath $target) {
            $backup = Join-Path $staging "previous"
            Move-Item -LiteralPath $target -Destination $backup
        }
        try {
            Move-Item -LiteralPath $extracted -Destination $target
        } catch {
            if ($backup -and (Test-Path -LiteralPath $backup)) {
                Move-Item -LiteralPath $backup -Destination $target -ErrorAction SilentlyContinue
            }
            Die $ExNodeBootstrap "cannot move the verified Node into $target"
        }

        # The probe above set these to the STAGING path, which the rename has just
        # invalidated, so the final location is recorded directly. Deliberately not
        # re-probed: the same bits already ran, and a second probe would re-open the
        # window this ordering closed. Once a VALIDATED tree is in place, losing the
        # backup is harmless -- that is what makes the new order safe.
        $script:NodeExe = Join-Path $target "node.exe"
        $script:NodeBinDir = $target
    } finally {
        # The backup lives INSIDE $staging, so this cleanup would destroy the
        # rollback -- and `finally` runs on Ctrl-C too, which is the realistic
        # interruption. If the target is absent at this point, promotion never
        # completed, so the backup goes back FIRST and only then is staging removed.
        if ($backup -and (Test-Path -LiteralPath $backup) -and
            -not (Test-Path -LiteralPath $target)) {
            Move-Item -LiteralPath $backup -Destination $target -ErrorAction SilentlyContinue
            Warn "restored the previous Node at $target"
        }
        if (Test-Path -LiteralPath $staging) {
            Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    Say "installed Node $NodeVersion at $Prefix\node"
}

# ── plan ─────────────────────────────────────────────────────────────
$spec = "$Package@$Version"
# Namespaced for the same reason as the shell installer: -Prefix is caller-supplied,
# and this script deletes then recreates the log, so a generic name could destroy an
# unrelated file under a prefix such as the user profile root.
$logPath = Join-Path $Prefix "$WrapperName-install.log"
$haveNode = Resolve-Node

if ($DryRun) {
    $artifact = Get-NodeArtifact
    Write-Host "${Self}: plan"
    Write-Host "  package        $spec"
    Write-Host "  registry       $RegistryDisplay"
    Write-Host "  isolated npmrc $([int]$IsolatedNpmrc.IsPresent)"
    Write-Host "  platform       win-$nodeArch"
    Write-Host "  prefix         $Prefix"
    Write-Host "  wrapper        $BinDir\$WrapperName.cmd"
    if ($SkipBrowsers) {
        Write-Host "  browsers       skipped"
    } elseif ($DownloadHost) {
        Write-Host "  browsers       download from $DownloadHostDisplay"
    } else {
        Write-Host "  browsers       download"
    }
    # A Node with no npm beside it is NOT reused by the real run -- it bootstraps
    # instead -- so the plan resolves npm before claiming a reuse.
    if ($haveNode -and (Resolve-Npm)) {
        Write-Host "  node           reuse $script:NodeExe"
    } elseif ($haveNode) {
        Write-Host "  node           install (found $script:NodeExe, but no npm beside it)"
    } else {
        Write-Host "  node           install $(Redact-Url $artifact.Mirror)/v$NodeVersion/$($artifact.Base).zip"
    }
    exit 0
}

foreach ($dir in @($Prefix, $BinDir)) {
    try {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    } catch {
        Die $ExNotWritable "cannot create $dir"
    }
}

# `New-Item -Force` SUCCEEDS on a directory that already exists, whatever its
# ACL, so creation proves nothing about writability. Without an explicit probe an
# unwritable -BinDir is discovered only when the wrapper is written -- after npm
# has installed the package and possibly downloaded Node and the browsers --
# leaving a partial install behind and reporting an unclassified failure instead
# of exit 17. The shell installer probes with `[ -w ]`; this is that check.
foreach ($dir in @($Prefix, $BinDir)) {
    $probe = Join-Path $dir (".write-probe-" + [guid]::NewGuid().ToString("N"))
    try {
        [IO.File]::WriteAllText($probe, "")
    } catch {
        Die $ExNotWritable "$dir is not writable"
    }
    Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
}

# npm writes the registry URL into its output, so the log can hold a token. The
# `> $logPath` redirect inside Invoke-NpmInstall would create it with whatever
# ACL it inherits from $Prefix, which on a shared prefix can be readable by other
# principals for the whole install -- the window before Redact-Log runs. Create
# it first with inheritance disabled and one ACE for the current user; the later
# redirect truncates an existing file without changing its DACL, exactly as the
# shell installer relies on the mode surviving its own redirect.
try {
    if (Test-Path -LiteralPath $logPath) { Remove-Item -LiteralPath $logPath -Force }
    $null = New-Item -ItemType File -Path $logPath -Force
    $logAcl = Get-Acl -LiteralPath $logPath
    $logAcl.SetAccessRuleProtection($true, $false)
    foreach ($ace in @($logAcl.Access)) { $null = $logAcl.RemoveAccessRule($ace) }
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $logAcl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
        $me, "FullControl", "Allow")))
    Set-Acl -LiteralPath $logPath -AclObject $logAcl
} catch {
    Die $ExNotWritable "cannot restrict the install log $logPath to owner-only"
}

if (-not $haveNode) { Install-Node }
if (-not (Resolve-Npm)) {
    # Same reasoning as the shell installer: a Node without npm beside it is a
    # real configuration, and sending the user off to install npm would hand back
    # the prerequisite this installer exists to remove. Abandon the reusable Node
    # and bootstrap our own, which bundles npm.
    if ($script:NodeWasBootstrapped) {
        Die $ExNodeBootstrap ("the Node just installed at $Prefix\node has no npm; " +
            "the archive may be truncated")
    }
    Say "found Node at $script:NodeExe but no npm beside it; installing a private Node that bundles npm"
    $script:NodeExe = ""
    $script:NodeBinDir = ""
    Install-Node
    if (-not (Resolve-Npm)) {
        Die $ExNodeBootstrap ("the Node just installed at $Prefix\node has no npm; " +
            "the archive may be truncated")
    }
}
Say "using Node $(Invoke-Native { & $script:NodeExe -p 'process.versions.node' }) at $script:NodeExe"

# ── already installed? ───────────────────────────────────────────────
# Only a PINNED version can be compared; `latest` is a moving target, so npm
# gets to make that decision itself.
# The executable must exist too, not just a matching manifest version: an install
# interrupted after npm wrote package.json but before it created the global shim
# would otherwise be skipped on every retry, failing verification every time,
# until the user discovered -Force.
$skipInstall = $false
$installedShim = Join-Path $Prefix "$WrapperName.cmd"
if (-not $Force -and $Version -ne "latest" -and (Test-Path -LiteralPath $installedShim -PathType Leaf)) {
    $manifest = Join-Path $Prefix "node_modules\$($Package -replace '/', '\')\package.json"
    if (Test-Path -LiteralPath $manifest -PathType Leaf) {
        try {
            $installed = (Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json).version
        } catch {
            $installed = $null
        }
        if ($installed -eq $Version) {
            Say "$spec is already installed in $Prefix (use -Force to reinstall)"
            $skipInstall = $true
        }
    }
}

# ── install ──────────────────────────────────────────────────────────
# npm's own output is the only evidence available when an enterprise network
# refuses the install, so it is kept on disk and classified rather than streamed
# past the user at whatever verbosity npm chose.
function Invoke-NpmInstall {
    $saved = @{}
    # --global writes into npm_config_prefix, which keeps the install inside the
    # user profile and means elevation is never involved.
    $overrides = @{
        "npm_config_registry"          = $Registry
        "npm_config_prefix"            = $Prefix
        "npm_config_fund"              = "false"
        "npm_config_audit"             = "false"
        "npm_config_update_notifier"   = "false"
        "PATH"                         = "$script:NodeBinDir;$env:PATH"
    }
    if ($IsolatedNpmrc) {
        # Two DISTINCT empty files, not NUL twice: npm refuses to load one path as
        # two scopes ("double-loading config as global, previously loaded as user")
        # and exits before resolving anything, which would break the very flag that
        # exists to rescue an enterprise install.
        $isolatedDir = Join-Path $Prefix "isolated-npmrc"
        New-Item -ItemType Directory -Path $isolatedDir -Force | Out-Null
        foreach ($scope in @("user", "global")) {
            $scopeFile = Join-Path $isolatedDir $scope
            if (-not (Test-Path -LiteralPath $scopeFile -PathType Leaf)) {
                New-Item -ItemType File -Path $scopeFile -Force | Out-Null
            }
        }
        $overrides["npm_config_userconfig"] = Join-Path $isolatedDir "user"
        $overrides["npm_config_globalconfig"] = Join-Path $isolatedDir "global"
    }
    if ($SkipBrowsers) { $overrides["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1" }
    if ($DownloadHost) { $overrides["PLAYWRIGHT_DOWNLOAD_HOST"] = $DownloadHost }

    foreach ($key in $overrides.Keys) {
        $saved[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
        [Environment]::SetEnvironmentVariable($key, $overrides[$key], "Process")
    }
    $previous = $ErrorActionPreference
    try {
        # npm writes to stderr on ORDINARY runs (deprecation notices), and under
        # the global 'Stop' preference Windows PowerShell 5.1 wraps merged native
        # stderr as a NativeCommandError and promotes it to terminating. That
        # would abort the script before the failure classifier below -- the whole
        # reason this installer exists -- and on the success path it would skip
        # writing the wrapper for a package that installed correctly. The native
        # call is judged on its exit code alone.
        $ErrorActionPreference = 'Continue'
        # No cmd.exe: the call operator hands argv to the process directly, so a
        # `%` in any path is never re-parsed. `*>` captures npm's stdout AND
        # stderr into the log the classifier reads.
        if ($script:NpmCliJs) {
            & $script:NodeExe $script:NpmCliJs install --global $spec *> $logPath
        } else {
            & $script:NpmCmd install --global $spec *> $logPath
        }
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $previous
        foreach ($key in $saved.Keys) {
            [Environment]::SetEnvironmentVariable($key, $saved[$key], "Process")
        }
    }
}

# Order matters: the browser CDN and the registry fail with the SAME transport
# errors, so the more specific signature is matched first.
function Get-FailureClass {
    $log = if (Test-Path -LiteralPath $logPath) { Get-Content -LiteralPath $logPath -Raw } else { "" }
    if ($log -match '(?i)cdn\.playwright\.dev|playwright\.azureedge\.net|Failed to (download|install) (browser|chromium|firefox|webkit)|Download failure.*(chromium|firefox|webkit)') {
        return $ExBrowserDownload
    }
    if ($log -match '(?i)\bE401\b|\bE403\b|ENEEDAUTH|EAUTHUNKNOWN|401 Unauthorized|403 Forbidden|Incorrect or missing password|npm login|auth(entication)? (required|token)') {
        return $ExRegistryAuth
    }
    if ($log -match '(?i)\bE404\b|404 Not Found|\bETARGET\b|No matching version|is not in this registry') {
        return $ExPackageNotFound
    }
    if ($log -match '(?i)ENOTFOUND|EAI_AGAIN|ECONNREFUSED|ECONNRESET|ETIMEDOUT|ERR_SOCKET_TIMEOUT|network timeout|SELF_SIGNED_CERT|UNABLE_TO_GET_ISSUER_CERT|UNABLE_TO_VERIFY_LEAF_SIGNATURE|CERT_HAS_EXPIRED|DEPTH_ZERO_SELF_SIGNED_CERT|tunneling socket could not be established|proxy') {
        return $ExRegistryUnreachable
    }
    return 1
}

function Show-LogTail {
    Warn ""
    Warn "  full npm log: $logPath"
    Warn "  -- last 20 lines of npm output ---------------------------"
    if ($script:LogSuppressed) {
        Warn "  | npm's output could not be scrubbed of credentials, so it was discarded"
        Warn "  | rather than shown. Re-run with -IsolatedNpmrc to reproduce without a token."
    } elseif (Test-Path -LiteralPath $logPath) {
        foreach ($line in (Get-Content -LiteralPath $logPath -Tail 20)) { Warn "  | $line" }
    } else {
        Warn "  | npm produced no readable output."
    }
    Warn "  ---------------------------------------------------------"
}

if (-not $skipInstall) {
    Say "installing $spec into $Prefix (registry: $RegistryDisplay)"
    if (-not (Invoke-NpmInstall)) {
        # Classify BEFORE redacting: Get-FailureClass greps this log, and
        # Redact-Log both rewrites it and, when it cannot, DELETES it -- so
        # redacting first can leave the classifier with no evidence at all and
        # downgrade a diagnosable enterprise failure to "unclassified". The shell
        # installer orders these the same way for the same reason.
        $class = Get-FailureClass
        Redact-Log $logPath
        switch ($class) {
            $ExRegistryAuth {
                Warn "the npm registry refused the request: authentication required."
                Warn ""
                Warn "  registry used: $RegistryDisplay"
                Warn ""
                Warn "  This is what an enterprise network looks like when npm is pointed at a"
                Warn "  private mirror that needs a login, or whose token has expired. Pick the"
                Warn "  branch that matches your situation:"
                Warn ""
                Warn "  a) your .npmrc redirects npm at a corporate mirror but this package is"
                Warn "     public - bypass the ambient config entirely:"
                Warn "       .\playwright-cli.ps1 -IsolatedNpmrc"
                Warn ""
                Warn "  b) the public registry is firewalled and the mirror is the only way out -"
                Warn "     log in to it, then install from it:"
                Warn "       npm login --registry https://npm.your-company.example/"
                Warn "       .\playwright-cli.ps1 -Registry https://npm.your-company.example/"
                Warn ""
                Warn "  c) the mirror does not carry these packages yet - ask whoever runs it to"
                Warn "     proxy $Package, playwright and playwright-core"
                Show-LogTail
                exit $ExRegistryAuth
            }
            $ExRegistryUnreachable {
                Warn "could not reach the npm registry $RegistryDisplay (DNS, proxy or TLS failure)."
                Warn ""
                Warn "  Behind an HTTP proxy, set it and re-run:"
                Warn "    `$env:HTTPS_PROXY = 'http://proxy.your-company.example:8080'"
                Warn "    `$env:NO_PROXY = 'localhost,127.0.0.1,.your-company.example'"
                Warn "  Where the network terminates TLS with an internal certificate authority,"
                Warn "  point Node at that CA bundle rather than disabling verification:"
                Warn "    `$env:NODE_EXTRA_CA_CERTS = 'C:\path\to\corporate-ca.pem'"
                Warn "  Where the public registry is blocked outright, install from the mirror:"
                Warn "    .\playwright-cli.ps1 -Registry https://npm.your-company.example/"
                Show-LogTail
                exit $ExRegistryUnreachable
            }
            $ExPackageNotFound {
                Warn "the registry has no '$spec'."
                Warn ""
                Warn "  Check which versions it actually carries:"
                Warn "    npm view $Package versions --registry $RegistryDisplay"
                Warn "  A private mirror often holds only the versions someone already pulled"
                Warn "  through it, so a version that exists publicly can still 404 there."
                Show-LogTail
                exit $ExPackageNotFound
            }
            $ExBrowserDownload {
                Warn "the npm package installed, but downloading the browser binaries failed."
                Warn ""
                Warn "  Browser builds come from the Playwright CDN, not the npm registry, so a"
                Warn "  network that allows npm can still block them. Either mirror them:"
                Warn "    .\playwright-cli.ps1 -DownloadHost https://playwright.your-company.example/"
                Warn "  or install the CLI now and supply browsers separately:"
                Warn "    .\playwright-cli.ps1 -SkipBrowsers"
                Show-LogTail
                exit $ExBrowserDownload
            }
            default {
                Warn "npm failed to install $spec, and the failure matched no known cause."
                Show-LogTail
                exit 1
            }
        }
    }
    Redact-Log $logPath
    Say "installed $spec"
}

# ── wrapper ──────────────────────────────────────────────────────────
# The npm-generated shim resolves `node` against the CALLER's PATH, so a user
# whose Node this installer had to bootstrap would get "node is not recognized"
# from a tool that installed cleanly. The wrapper pins the exact Node that was
# verified at install time.
$target = Join-Path $Prefix "$WrapperName.cmd"
if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
    Die $ExVerify "npm reported success but $target does not exist; see $logPath"
}

$wrapper = Join-Path $BinDir "$WrapperName.cmd"
# -BinDir $Prefix makes the wrapper AND its target the same file, so the wrapper
# would call itself until the process ran out of stack.
# GetFullPath is LEXICAL: it normalises separators and `..` but never follows a
# reparse point, so a -BinDir junction pointing at $Prefix compares as a different
# path while resolving to the same directory. The wrapper would then overwrite the
# npm shim it is supposed to call, and every invocation would re-enter itself until
# the process ran out of stack. Windows PowerShell 5.1 cannot resolve a junction's
# target without P/Invoke, so a reparse point on either directory is refused rather
# than resolved -- the shell installer gets this for free from `cd -P`, which
# follows symlinks before comparing.
# A reparse point ANYWHERE in the ancestry aliases the path, not just one on the
# leaf: `-BinDir C:\junction\bin` and `-Prefix C:\real\bin` can be the same
# directory while both leaves look ordinary and the strings differ. So every
# existing ancestor is checked. The shell installer needs none of this because
# `cd -P` resolves every component of the path before the comparison.
function Find-ReparsePointAncestor([string]$Path) {
    $current = $Path
    while (-not [string]::IsNullOrWhiteSpace($current)) {
        $item = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
        if ($null -ne $item -and
            ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
            return $current
        }
        $parent = Split-Path -Parent $current
        if ($parent -eq $current) { break }
        $current = $parent
    }
    return ""
}

foreach ($pair in @(@("-BinDir", $BinDir), @("-Prefix", $Prefix))) {
    $alias = Find-ReparsePointAncestor $pair[1]
    if ($alias) {
        Die $ExUsage ("$($pair[0]) resolves through a junction or symlink ($alias); pass the " +
            "directory it resolves to, so the wrapper cannot be written over the tool it wraps")
    }
}
if ([System.IO.Path]::GetFullPath($wrapper) -eq [System.IO.Path]::GetFullPath($target)) {
    Die $ExUsage ("-BinDir must not be the installed package's own directory ($Prefix); " +
        "the wrapper would replace the tool it wraps")
}
# cmd.exe re-expands % in a batch line, so a literal one in either path (a
# directory such as C:\100%Done\node is legal) must be doubled or the wrapper
# invokes a different, non-existent target.
$escapedNodeBin = $script:NodeBinDir -replace '%', '%%'
$escapedTarget = $target -replace '%', '%%'
# `setlocal DisableDelayedExpansion` is load-bearing, not hygiene. A path may
# legally contain `!`, and when this wrapper is invoked from a shell started with
# `cmd /V:ON` that setting is INHERITED -- delayed expansion would then eat the
# `!` and its neighbours out of the PATH line, pointing the wrapper at a
# directory that does not exist. Turning it off is scoped to the wrapper and
# leaves `%ERRORLEVEL%` alone, which is ordinary expansion.
$wrapperBody = @"
@echo off
REM Generated by playwright-cli.ps1 - re-run that installer to regenerate.
setlocal DisableDelayedExpansion
set "PATH=$escapedNodeBin;%PATH%"
"$escapedTarget" %*
exit /b %ERRORLEVEL%
"@
# OEM is the code page cmd.exe reads a batch file in, and it carries no BOM
# (which cmd would execute as a command). ASCII would replace every non-ASCII
# character in the path with `?`, so a default profile such as C:\Users\Jose
# with an accent would point the wrapper at a target that does not exist.
# OEM is the code page cmd.exe reads a batch file in, and it covers every
# character in the user's own legacy code page -- but not one outside it (CJK on
# a Latin code page, an emoji). Such a character would be written as `?`, and the
# wrapper would silently point at a path that does not exist. Detect that by
# round-tripping through the same encoder and refuse, rather than installing a
# wrapper that cannot work.
$oemEncoding = [System.Text.Encoding]::GetEncoding(
    [System.Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage)
if ($oemEncoding.GetString($oemEncoding.GetBytes($wrapperBody)) -ne $wrapperBody) {
    Die $ExVerify ("the install paths contain characters the console code page " +
        "(OEM $($oemEncoding.CodePage)) cannot represent, so a working .cmd wrapper cannot be " +
        "written; re-run with -Prefix and -BinDir under a path in that code page")
}
$wrapperTmp = "$wrapper.incoming"
Set-Content -LiteralPath $wrapperTmp -Value $wrapperBody -Encoding OEM
# `Move-Item -Force` onto an existing file is delete-then-move on 5.1, not the
# atomic rename the .sh twin gets from POSIX `mv`. A locked destination -- the
# wrapper is running, or an indexer/AV has it open, both ordinary on Windows --
# throws, and uncaught that surfaces as a PowerShell stack trace with an exit code
# outside the documented table.
try {
    Move-Item -LiteralPath $wrapperTmp -Destination $wrapper -Force
} catch {
    Remove-Item -LiteralPath $wrapperTmp -Force -ErrorAction SilentlyContinue
    Die $ExNotWritable ("cannot replace $wrapper -- is playwright-cli currently " +
        "running? Close it and re-run. ($($_.Exception.Message))")
}

# ── verify ───────────────────────────────────────────────────────────
$versionOut = ""
$previous = $ErrorActionPreference
try {
    # Same native-stderr hazard as the install call: a tool that greets on stderr
    # must not be reported as a broken install.
    $ErrorActionPreference = 'Continue'
    # Invoked through the call operator rather than a cmd.exe command STRING, so
    # PowerShell does the quoting and a `%` in the wrapper's path is not re-parsed
    # by a shell of our own making.
    $versionOut = (& $wrapper --version 2>$null | Select-Object -First 1)
    if ([string]::IsNullOrWhiteSpace($versionOut)) {
        & $wrapper --help *> $null
        if ($LASTEXITCODE -ne 0) {
            Die $ExVerify "$wrapper was installed but does not run; see $logPath"
        }
        $versionOut = "(version unavailable)"
    }
} catch {
    Die $ExVerify "$wrapper was installed but does not run; see $logPath"
} finally {
    $ErrorActionPreference = $previous
}

Say "$WrapperName $versionOut"

# ── browsers ─────────────────────────────────────────────────────────
# Same reasoning as the shell installer: the browser comes from the Playwright CDN
# rather than the npm registry, so doing it here under the classified environment
# turns a blocked CDN into exit 16 with a mirror remedy instead of a stall inside
# the user's first browse.
if (-not $SkipBrowsers) {
    Say "downloading browser binaries"
    $savedHost = [Environment]::GetEnvironmentVariable("PLAYWRIGHT_DOWNLOAD_HOST", "Process")
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        if ($DownloadHost) {
            [Environment]::SetEnvironmentVariable("PLAYWRIGHT_DOWNLOAD_HOST", $DownloadHost, "Process")
        }
        & $wrapper install-browser *>> $logPath
        $browserFailed = ($LASTEXITCODE -ne 0)
    } catch {
        $browserFailed = $true
    } finally {
        $ErrorActionPreference = $previous
        [Environment]::SetEnvironmentVariable("PLAYWRIGHT_DOWNLOAD_HOST", $savedHost, "Process")
    }
    if ($browserFailed) {
        Redact-Log $logPath
        Warn "the CLI installed, but downloading the browser binaries failed."
        Warn ""
        Warn "  Browser builds come from the Playwright CDN, not the npm registry, so a"
        Warn "  network that allows npm can still block them. Either mirror them:"
        Warn "    .\playwright-cli.ps1 -DownloadHost https://playwright.your-company.example/"
        Warn "  or keep the CLI as installed and supply browsers separately:"
        Warn "    .\playwright-cli.ps1 -SkipBrowsers"
        Show-LogTail
        exit $ExBrowserDownload
    }
    # Also on success: Playwright echoes the download host it used, so a
    # credentialed -DownloadHost would otherwise persist in install.log.
    Redact-Log $logPath
}

Say "installed at $wrapper"
$pathParts = ($env:PATH -split ';') | ForEach-Object { $_.TrimEnd('\') }
if ($pathParts -notcontains $BinDir) {
    Write-Host ""
    Write-Host "$BinDir is not on your PATH. Add it for future sessions with:"
    Write-Host "  [Environment]::SetEnvironmentVariable('Path', `"$BinDir;`" + [Environment]::GetEnvironmentVariable('Path','User'), 'User')"
    Write-Host "then open a new terminal."
}
Write-Host ""
Write-Host "Next steps:"
Write-Host "  $WrapperName --help"
if ($SkipBrowsers) {
    Write-Host "  browsers were skipped - supply them from your own mirror before first use"
}
