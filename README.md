# bub
Small sandboxing tool using [bubblewrap](https://github.com/containers/bubblewrap).

## Use case
Need to use coding agents on your computer but don't trust them with secrets / private data?
Bubblewrap can be used on Linux systems in order to sandbox single programs.
This is similar to what is done with containers and also in use by Flatpak.

This repo provides a small wrapper using namespaces and a [tinyproxy](https://github.com/tinyproxy/tinyproxy) to control allowed network traffic.

## Features
- user namespace with isolated filesystem and network
- http proxy with explicit allow list (see `tinyproxy/allow`)
- bind-mount the current working directory and some system dirs to the namespace
- arbitrary process isolation, e.g. for `claude`
- access to only explicitly allowed files
- no access to ssh / gpg / age keys inside the sandbox
- no access to any sockets on the host (no ssh socket, no wayland socket, no dbus etc.)

## Prerequisites 
- Arch Linux
- Podman
- bubblewrap
- a modern kernel (default on Arch)

## Installation

Install the `bub` and the tinyproxy setup using the install script:

```bash
./install
```

This will install:
- a container file at `~/.config/containers/systemd/bub-tinyproxy.container`
- a socket unit at `~/.config/systemd/user/bub-proxy.socket`
- a service unit at `~/.config/systemd/user/bub-proxy.service`
- the `bub` script to `~/.local/bin/bub`

Make sure to add `~/.local/bin/` to your path, if you don't have it already.

## Usage
Execute any program by passing it to bub:

```bash
bub claude
```

### Testing the sandbox

You can start an interactive bash session in the terminal in order to test file availability / commands:

```bash
bub bash
```

## Functionality of the proxy

The sandbox has its own network namespace, which is why all networking doesn't work default.
If you want to request a network request, HTTP/s requests are sent over the proxy which decides which requests go through and which don't.

The proxy is provided by a unix socket, bind-mounted into the sandbox to `/run/proxy.sock` within the sandbox.
Inside the sandbox, `socat` is used in order to bind the socket to `127.0.0.1:3128`.
Environment variables `HTTPS_PROXY` etc. then specify what proxy to use for all common tools.

On the host, tinyproxy is running within a rootless container via Podman user quadlet.
The tinyproxy container is started and stopped on demand, once the socket is requested.
Systemd triggers `bub-proxy.service` once `bub-proxy.socket` is requested, given that the socket unit is active (done at the beginning of the bub script).
`bub-proxy.service` then proxies the socket to `tinyproxy`, running on `127.0.0.1:8888`.

## Network requests fail / result in forbidden / take a long time
You can check if the request was caught by `tinyproxy` using the journal:

```bash
journalctl --user -eu bub-tinyproxy
```

From there on, you can add missing required hosts to `tinyproxy/allow` as a regex.
After modifying the file, install the config and restart tinyproxy:

```bash
./install
systemctl --user restart bub-tinyproxy
```

## Acknowledgement
This setup was based on [A detailed writeup of Claude Code constrained by Bubblewrap.](https://patrickmccanna.net/a-detailed-writeup-of-claude-code-constrained-by-bubblewrap/) by Patrick McCanna.
