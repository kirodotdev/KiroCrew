# Installation

Source: https://kiro.dev/docs/getting-started/installation/ (fetched 2026-09-06)

Upstream retired `/docs/cli/installation/`; installation is now one
surface-agnostic page with a CLI tab. Requirements for the CLI: macOS, Windows 11
(PowerShell), or Linux on glibc 2.34+ or the musl variant.

## Install script

```bash
curl -fsSL https://cli.kiro.dev/install | bash    # macOS and Linux
```

```powershell
irm 'https://cli.kiro.dev/install.ps1' | iex      # Windows
```

Homebrew is not a supported install path.

## Linux package alternatives

Check glibc with `ldd --version`; below 2.34 use the musl zip.

**AppImage:**

```bash
wget https://desktop-release.q.us-east-1.amazonaws.com/latest/kiro-cli.appimage
chmod +x kiro-cli.appimage
./kiro-cli.appimage
```

**Ubuntu (.deb):**

```bash
wget https://desktop-release.q.us-east-1.amazonaws.com/latest/kiro-cli.deb
sudo dpkg -i kiro-cli.deb
sudo apt-get install -f
```

**Zip (glibc 2.34+):**

```bash
# x86_64
curl --proto '=https' --tlsv1.2 -sSf 'https://desktop-release.q.us-east-1.amazonaws.com/latest/kirocli-x86_64-linux.zip' -o 'kirocli.zip'
# ARM aarch64
curl --proto '=https' --tlsv1.2 -sSf 'https://desktop-release.q.us-east-1.amazonaws.com/latest/kirocli-aarch64-linux.zip' -o 'kirocli.zip'
```

**Zip (musl, glibc below 2.34):**

```bash
# x86_64
curl --proto '=https' --tlsv1.2 -sSf 'https://desktop-release.q.us-east-1.amazonaws.com/latest/kirocli-x86_64-linux-musl.zip' -o 'kirocli.zip'
# ARM aarch64
curl --proto '=https' --tlsv1.2 -sSf 'https://desktop-release.q.us-east-1.amazonaws.com/latest/kirocli-aarch64-linux-musl.zip' -o 'kirocli.zip'
```

Install: `unzip kirocli.zip && ./kirocli/install.sh` (installs to `~/.local/bin`).

## Updating

The CLI auto-updates in the background and installs on exit. Only the latest
build is distributed, so there is no published previous CLI version to pin to;
holding a version means disabling auto-update:

```bash
kiro-cli settings "app.disableAutoupdates" "true"
```

## Proxy configuration

```bash
export HTTP_PROXY=http://proxy.company.com:8080
export HTTPS_PROXY=http://proxy.company.com:8080
export NO_PROXY=localhost,127.0.0.1,.company.com
# With auth: http://username:password@proxy.company.com:8080
```

Browser sign-in bypasses these: it runs through the operating system's network
stack, not the CLI's proxy configuration.

## Uninstalling

```bash
kiro-cli uninstall                    # macOS
sudo apt-get remove kiro-cli          # Ubuntu
```

## Debugging

```bash
kiro-cli doctor    # identify and fix common issues
kiro-cli issue     # report a bug
```
