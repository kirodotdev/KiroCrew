# Installation

Source: https://kiro.dev/docs/cli/installation/

## macOS

```bash
curl -fsSL https://cli.kiro.dev/install | bash
```

## Linux AppImage

```bash
wget https://desktop-release.q.us-east-1.amazonaws.com/latest/kiro-cli.appimage
chmod +x kiro-cli.appimage
./kiro-cli.appimage
```

## With a zip file

Requires glibc 2.34+ (or use musl version). Check: `ldd --version`

**Standard (glibc 2.34+):**

```bash
# x86_64
curl --proto '=https' --tlsv1.2 -sSf 'https://desktop-release.q.us-east-1.amazonaws.com/latest/kirocli-x86_64-linux.zip' -o 'kirocli.zip'
# ARM aarch64
curl --proto '=https' --tlsv1.2 -sSf 'https://desktop-release.q.us-east-1.amazonaws.com/latest/kirocli-aarch64-linux.zip' -o 'kirocli.zip'
```

**Musl (glibc < 2.34):**

```bash
# x86_64
curl --proto '=https' --tlsv1.2 -sSf 'https://desktop-release.q.us-east-1.amazonaws.com/latest/kirocli-x86_64-linux-musl.zip' -o 'kirocli.zip'
# ARM aarch64
curl --proto '=https' --tlsv1.2 -sSf 'https://desktop-release.q.us-east-1.amazonaws.com/latest/kirocli-aarch64-linux-musl.zip' -o 'kirocli.zip'
```

Install: `unzip kirocli.zip && ./kirocli/install.sh` (installs to `~/.local/bin`).

## Ubuntu (.deb)

```bash
wget https://desktop-release.q.us-east-1.amazonaws.com/latest/kiro-cli.deb
sudo dpkg -i kiro-cli.deb
sudo apt-get install -f
```

## Proxy configuration (v1.8.0+)

```bash
export HTTP_PROXY=http://proxy.company.com:8080
export HTTPS_PROXY=http://proxy.company.com:8080
export NO_PROXY=localhost,127.0.0.1,.company.com
# With auth: http://username:password@proxy.company.com:8080
```

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
