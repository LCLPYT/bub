# Implementation

This document describes how bub-vm works internally. For installation and usage, see the [README](../README.md).

## Overview

bub-vm consists of four files:

| File          | Role                                                                                         |
|---------------|----------------------------------------------------------------------------------------------|
| `flake.nix`   | Flake entry point. Evaluates the guest system and builds the `bub-vm` package.               |
| `guest.nix`   | NixOS module for the guest. Defines the `bub.*` options and the guest configuration.         |
| `wrapper.nix` | Turns the evaluated guest into the `bub-vm` shell application.                               |
| `bub-vm.sh`   | Host side logic: state, shares, devShell, VM lifecycle and SSH sessions.                     |

The guest is a NixOS system built on the host for the guest architecture. It boots from the host `/nix/store`,
which is shared read-only, and keeps its mutable state on a per-project root disk.

## flake.nix

Inputs are `nixpkgs` (nixos-unstable) and `microvm.nix`.

Supported host systems are `x86_64-linux`, `aarch64-linux` and `aarch64-darwin`. The guest system is derived from
the host system by replacing `-darwin` with `-linux`.

The flake exports:

- `lib.mkVm { system, modules ? [ ] }`: evaluates a NixOS system with the microvm.nix module, `guest.nix` and the
  given extra modules. The host package set is passed to the modules as `hostPkgs`.
- `lib.mkBub { system, modules ? [ ] }`: calls `wrapper.nix` with the result of `mkVm`, producing the `bub-vm`
  package.
- `nixosModules.default`: `guest.nix`.
- `packages.<system>.default` and `apps.<system>.default`: `bub-vm` with the default configuration.

Custom configurations (e.g. via home-manager) use `mkBub` and pass extra NixOS modules.

## wrapper.nix

Builds `bub-vm` with `writeShellApplication`. Values from the evaluated guest are prepended to `bub-vm.sh` as
shell variables:

| Variable           | Source                                                      |
|--------------------|-------------------------------------------------------------|
| `BUB_RUNNER`       | `microvm.declaredRunner`, the microvm.nix runner derivation |
| `BUB_HYPERVISOR`   | `microvm.hypervisor` (`qemu` or `vfkit`)                    |
| `BUB_GUEST_SYSTEM` | Guest system, e.g. `x86_64-linux`                           |
| `BUB_GUEST_USER`   | `bub.user`                                                  |
| `BUB_GUEST_UID`    | `bub.uid`                                                   |
| `BUB_HOME_SHARES`  | `bub.homeShares` as `tag<TAB>source` lines                  |

Runtime dependencies are coreutils, findutils, flock, git, openssh, perl and socat, plus virtiofsd on Linux hosts.

## guest.nix

### Options

| Option            | Default                         | Description                                                    |
|-------------------|---------------------------------|----------------------------------------------------------------|
| `bub.user`        | `bub`                           | Name of the user inside the VM.                                |
| `bub.uid`         | `501` on macOS, `1000` on Linux | UID of the VM user. Must match the host user.                  |
| `bub.diskSize`    | `32768` (MiB)                   | Size of the sparse per-project root disk.                      |
| `bub.homeShares`  | `{ }`                           | Host directories mounted into the home directory of the user.  |
| `bub.packages`    | curl, fd, git, jq, ripgrep, ... | Packages available inside the VM.                              |

Each home share has a `source` (host path, a leading `~` expands to the host home directory) and a `target`
(relative to the guest home directory). Its virtiofs tag is `h-<name>`.

### MicroVM configuration

- Hypervisor: `vfkit` on macOS hosts, `qemu` otherwise.
- 4 vCPUs and 4096 MiB memory by default (`lib.mkDefault`, so modules can override them).
- No control socket, fixed machine id.
- One static share: the host `/nix/store`, mounted read-only at `/nix/.ro-store`.
- One volume: `root.img` with label `bub-root`, mounted at `/` with size `bub.diskSize`. The image is created
  relative to the working directory of the runner, which is the per-project state directory.
- `extraArgsScript` prints `BUB_HYPERVISOR_ARGS`. microvm.nix bakes shares into the runner at build time, so this is
  how project specific devices are added at runtime.
- On macOS, vfkit is wrapped: vfkit puts stdin into raw mode for `virtio-serial,stdio` and fails without a tty,
  which is the case once the VM runs in the background. The wrapper replaces that argument with
  `virtio-serial,logFilePath=$PWD/serial.log`.

### System

- `vmw_vsock_virtio_transport` is loaded, `systemd.ssh_auto=no` disables the systemd SSH generator and `/tmp` is a
  tmpfs.
- Nix and documentation are disabled. Nix is not available inside the VM.
- Networking uses systemd-networkd with DHCP on any ethernet interface. The firewall is enabled.
- Users are immutable. The VM user has the configured UID, is in `wheel` (passwordless sudo) and has subuid and
  subgid ranges for rootless Podman. NixOS only allows UIDs >= 1000 for normal users, so UIDs below 1000 (macOS
  hosts) make it a system user instead.
- Podman is enabled with Docker compatibility and DNS on the default network. `DOCKER_HOST` points to the user
  Podman socket when `XDG_RUNTIME_DIR` is set.
- All terminfo entries are installed.

### SSH

sshd is socket activated and listens only on `vsock::22`. The firewall port is not opened. Authentication is by
public key only, read from `/run/bub/meta/authorized_keys`. Root login, agent forwarding, stream local forwarding
and X11 forwarding are disabled. TCP forwarding is limited to `local`. `COLORTERM` is accepted from the client.

### Mounts

- `/run/bub/meta`: virtiofs tag `meta`, read-only. Contains `workdir`, `authorized_keys` and optionally
  `devenv.sh`.
- `~/<target>` for every home share: virtiofs tag `h-<name>`.
- The project directory: mounted by `bub-workdir.service`, see below.

### bub-workdir.service

A oneshot service that runs after `/run/bub/meta` is mounted. It reads the project path from
`/run/bub/meta/workdir`, requires it to be absolute and refuses system directories as well as the guest home
directory. It then creates the directory, mounts the virtiofs tag `work` there (so the project has the same path
as on the host) and touches `/run/bub/ready`.

### bub-session

The command executed for every SSH session:

1. Waits up to 30 seconds for `/run/bub/ready`.
2. Changes into the project directory.
3. Sources `/etc/profile` if the NixOS environment is not set up yet, then `/run/bub/meta/devenv.sh` if present.
4. Runs the given command, or `bash -i` without arguments.

## bub-vm.sh

### Commands

| Invocation             | Action                                                           |
|------------------------|------------------------------------------------------------------|
| `bub-vm [cmd args...]` | Run a command (default: interactive shell) in the project VM.    |
| `bub-vm --stop`        | Stop the VM of the current directory.                            |
| `bub-vm --destroy`     | Stop the VM and delete its state directory, asks on a tty.       |
| `bub-vm --log`         | Print `vm.log` and `serial.log`.                                 |

Environment variables: `BUB_DEVSHELL=0` skips the devShell, `BUB_KEEP_RUNNING=1` keeps the VM running after the
last session.

### Per-project identity

The physical working directory (`pwd -P`) identifies the project. The first 12 hex characters of its SHA-256 hash
determine:

- the state directory `${XDG_STATE_HOME:-~/.local/state}/bub-vm/<hash>/`
- the vsock CID (first 7 hex characters plus 3, since CIDs 0 to 2 are reserved)
- the MAC address `02:xx:xx:xx:xx:xx` (locally administered)

### State directory

| Path                   | Content                                                   |
|------------------------|-----------------------------------------------------------|
| `root.img`             | Sparse root disk of the guest.                            |
| `workdir`              | Project path, for reference.                              |
| `id_ed25519{,.pub}`    | SSH key pair of the project.                              |
| `meta/`                | Shared read-only with the guest as `meta`.                |
| `devshell.approved`    | Hash of the approved Nix files.                           |
| `devshell*`            | Nix profile of the devShell (keeps it from being GC'd).   |
| `vm.pid`, `vm.runner`  | PID and runner store path of the running VM.              |
| `virtiofsd.pids`       | PIDs of the virtiofsd processes (Linux).                  |
| `*.sock`               | virtiofs and vsock sockets.                               |
| `vm.log`, `serial.log` | Hypervisor output and console log (vfkit).                |
| `virtiofsd.log`        | virtiofsd output (Linux).                                 |
| `start.lock`, `sessions.lock` | Lock files, see [Sessions and locking](#sessions-and-locking). |

### Startup sequence

1. `validate`
   - The host UID must equal `BUB_GUEST_UID`, otherwise file ownership in shares would break.
   - Refuses to share `/`, `$HOME`, `~/.ssh`, `~/.gnupg`, `~/.config`, `~/.local`, `~/.local/share` and the
     bub-vm state directory.
   - Refuses paths that would shadow system directories in the guest (`/etc`, `/nix`, `/run`, ...).
   - Rejects paths with whitespace, commas, `=`, quotes, backslashes, `$`, backticks and glob characters, since
     they can't be passed safely in hypervisor arguments.
   - The state path must be shorter than 80 characters because of the unix socket path limit.
   - With QEMU, `/dev/kvm` and `/dev/vhost-vsock` must be writable.
2. `setup_state`: creates the state and `meta` directories (mode 700), writes the project path, generates the SSH
   key pair if missing and copies the public key to `meta/authorized_keys`.
3. `collect_shares`: builds the share list: `work` (project), `meta` and every home share. Home share sources get
   `~` expanded, are created if missing, resolved to physical paths and validated.
4. `prepare_devshell`, see [devShell](#devshell).
5. Takes the start lock. If the VM is not running, `start_vm`. If it is running with a different runner, a hint to
   restart it is logged.
6. Takes a shared session lock, releases the start lock and installs the exit traps.
7. `wait_ssh`, then runs `bub-session` via SSH with the shell-quoted arguments. A tty is requested when stdin and
   stdout are terminals. The exit code of the remote command is returned.

### devShell

The devShell is skipped when `BUB_DEVSHELL` is not `1`, the project has no `flake.nix` or `nix` is not installed on
the host.

The project directory is writable from inside the VM, so its Nix files are untrusted and evaluating them runs
code on the host. Before loading, `approve_flake` hashes all `*.nix` files and `flake.lock` (tracked and untracked
but not ignored files in a git repository, otherwise only `flake.nix` and `flake.lock`) and compares the result to
`devshell.approved`. On a mismatch the user is asked for confirmation. Without a tty the devShell is skipped.

After approval, `devShells.<guest-system>.bub` is used if it exists, otherwise `.default`. It is built on the host
with `nix print-dev-env --profile` and written to `meta/devenv.sh`, which `bub-session` sources. Its store paths
reach the guest through the shared store. All nix calls disable import from derivation.

On any failure the devShell is removed and the session continues without it.

### Starting the VM

Old sockets and logs are removed first. Background processes are started with `detach`, which runs them in the
state directory, in a new session (`setsid`, so Ctrl-C and hangups of the terminal don't reach them), with stdin
from `/dev/null` and output appended to a log file.

QEMU (Linux):

- One `virtiofsd` per share (`--sandbox=none --cache=auto`): `ro-store` for `/nix/store` and `meta` read-only,
  the others writable. bub-vm waits up to 5 seconds for all sockets to appear.
- Extra arguments: user mode networking with the project MAC, `vhost-vsock-pci` with the project CID and a
  `vhost-user-fs-pci` device per share.

vfkit (macOS):

- NAT networking with the project MAC.
- A vsock device that connects guest port 22 to the unix socket `ssh.sock`.
- A `virtio-fs` device per share. The read-only store share comes from the runner itself.

The arguments are exported as `BUB_HYPERVISOR_ARGS`, the runner's `microvm-run` is detached and its PID and store
path are recorded in `vm.pid` and `vm.runner`.

### SSH

`vm_ssh` runs ssh with an isolated configuration:

- `-F /dev/null` ignores the user SSH config, `SSH_AUTH_SOCK` is unset and only the project key is used.
- Agent, X11 and all other forwardings are disabled.
- Host key checking is off and no known hosts file is used. The transport is a local vsock or unix socket.
- The proxy command is `socat - VSOCK-CONNECT:<cid>:22` (QEMU) or `socat - UNIX-CONNECT:ssh.sock` (vfkit).
- `COLORTERM` is passed to the guest.

`wait_ssh` polls for up to about 90 seconds. If the VM process exits in the meantime, the last console output is
printed.

### Stopping the VM

`stop_vm` sends `systemctl poweroff` through SSH and waits up to 30 seconds for the hypervisor to exit. After that
it sends `SIGTERM` and, 2 seconds later, `SIGKILL`. Finally the virtiofsd processes are killed and the PID files and
sockets are removed.

### Sessions and locking

Two lock files coordinate concurrent invocations in the same project:

- `start.lock` (fd 8, exclusive): serializes starting and stopping the VM.
- `sessions.lock` (fd 9, shared): held by every running session.

When a session ends, the `finish` trap releases its shared lock, takes the start lock and tries to take the
session lock exclusively without blocking. This only succeeds when no other session is active, in which case the
VM is stopped. With `BUB_KEEP_RUNNING=1` this step is skipped. `HUP`, `INT` and `TERM` exit with the usual codes so
the trap runs.
