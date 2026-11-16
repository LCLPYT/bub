# bub (Apple containers)

> [!NOTE]
> This version is archived and no longer maintained. See the [main README](../../README.md) for the current microVM-based version.

Small sandboxing tool for macOS using [Apple containers](https://github.com/apple/container).

MacOS doesn't use the Linux kernel and thus doesn't have user namespaces, so the [bubblewrap version](../bubblewrap/README.md) doesn't work there.
Instead, `bub` runs programs in a Debian container. Apple containers create a small VM for each container, so separation is even better than with the bubblewrap version.

## Features
- every invocation runs in a fresh container (`--rm`), which is its own lightweight VM
- only the current working directory is mounted, at the same path as on the host
- refuses to mount overly broad or sensitive directories (`/`, `$HOME`, `~/Library`, `~/.ssh`, `~/.config`, ...)
- Claude Code preinstalled, with a sandbox-only config at `~/.config/bub-sandbox/claude`
- image is rebuilt automatically when the `Containerfile` changes

## Prerequisites
- macOS on Apple Silicon
- the Apple [`container`](https://github.com/apple/container) CLI, with its system service started (`container system start`)

## Installation

Install `bub` and the `Containerfile` using the install script:

```bash
./install
```

This will install:
- the `Containerfile` to `~/.local/share/bub-sandbox/Containerfile`
- the `bub` script to `~/.local/bin/bub`

Make sure to add `~/.local/bin/` to your path, if you don't have it already.

## Usage
Execute any program by passing it to bub:

```bash
bub claude
bub bash      # interactive shell, e.g. to test the sandbox
```

The image `bub-sandbox` is built on first use.

| Variable          | Default | Description                                     |
|-------------------|---------|-------------------------------------------------|
| `BUB_CPUS`        | `4`     | CPUs of the container.                          |
| `BUB_MEMORY`      | `4G`    | Memory of the container.                        |
| `BUB_REBUILD`     | `0`     | Set to `1` to rebuild the image without cache.  |

## Customization
The container doesn't have access to programs installed on the host.
If you need additional programs, edit `~/.local/share/bub-sandbox/Containerfile`; the image is rebuilt on the next run.

## Limitations
- no network proxy: the container has unrestricted network access
- the image is Debian-based, programs from the host are not available
