# Implementation

This document describes how bub-vm works internally. For installation and usage, see the [README](../README.md).

## Overview

bub-vm consists of three Nix files and a Python package:

| File          | Role                                                                                         |
|---------------|----------------------------------------------------------------------------------------------|
| `flake.nix`   | Flake entry point. Evaluates the guest system and builds the `bub-vm` package.               |
| `guest.nix`   | NixOS module for the guest. Defines the `bub.*` options and the guest configuration.         |
| `wrapper.nix` | Turns the evaluated guest into the `bub-vm` package with a JSON config.                      |
| `bub_vm/`     | Host side logic: state, shares, devShell, VM lifecycle, SSH sessions and VM management.      |

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

Installs the `bub_vm` package to `libexec/`, byte-compiles it and creates `bin/bub-vm` with `makeWrapper`. The
wrapper runs `bub_vm/__main__.py` with `python3 -I` (isolated mode, so `PYTHONPATH` and user site packages are
ignored, and the script directory is not added to `sys.path`; the launcher adds `libexec/` itself), prepends the runtime dependencies
to `PATH` and sets `BUB_VM_CONFIG` to a JSON file generated from the evaluated guest:

| Key           | Source                                                      |
|---------------|-------------------------------------------------------------|
| `runner`      | `microvm.declaredRunner`, the microvm.nix runner derivation |
| `hypervisor`  | `microvm.hypervisor` (`qemu` or `vfkit`)                    |
| `guestSystem` | Guest system, e.g. `x86_64-linux`                           |
| `guestUser`   | `bub.user`                                                  |
| `guestUid`    | `bub.uid`                                                   |
| `guestHome`   | Home directory of `bub.user`                                |
| `systemDirs`  | `bub.systemDirs`, guest paths that can't be mount points    |
| `shares`      | `bub.shares` as a list of `{ tag, source, target, readOnly }` |
| `network`     | `bub.network.enable`                                        |
| `keepRunning` | `bub.keepRunning`                                           |
| `devShell`    | `bub.devShell.enable`                                       |
| `forwardEnv`  | `bub.forwardEnv`                                            |

Runtime dependencies are git, openssh and socat, plus virtiofsd on Linux hosts. The script only uses the Python
standard library. The build byte-compiles it, so syntax errors fail the build.

## guest.nix

### Options

| Option            | Default                         | Description                                                    |
|-------------------|---------------------------------|----------------------------------------------------------------|
| `bub.user`        | `bub`                           | Name of the user inside the VM.                                |
| `bub.uid`         | `501` on macOS, `1000` on Linux | UID of the VM user. Must match the host user.                  |
| `bub.diskSize`    | `32768` (MiB)                   | Size of the sparse per-project root disk.                      |
| `bub.shares`      | `{ }`                           | Host directories mounted into the VM (alias `bub.homeShares`). |
| `bub.network.enable` | `true`                       | NAT network interface.                                         |
| `bub.sudo.enable` | `true`                          | Passwordless sudo for the VM user.                             |
| `bub.keepRunning` | `false`                         | Default for `BUB_KEEP_RUNNING`.                                |
| `bub.devShell.enable` | `true`                      | Default for `BUB_DEVSHELL`.                                    |
| `bub.forwardEnv`  | `[ "COLORTERM" ]`               | Variables sent by ssh (`SendEnv`) and accepted by sshd.        |

Each share has a `source` (host path, a leading `~` expands to the host home directory), a `target` and
`readOnly` (default `false`). A relative `target` is relative to the guest home directory, an absolute one is used
as is and `null` (the default) means the source path on the host. Its virtiofs tag is `s-<name>`, so names are
limited to letters, digits, `_` and `-` and 34 characters (virtiofs tags have at most 36), which is asserted.

The internal, read-only `bub.systemDirs` lists the guest paths that can't be mount points (`/etc*`, `/nix*`,
`/run*`, ...). It is used by both the guest and bub-vm.

A base set of packages (curl, fd, git, jq, ripgrep, ...) is added to `environment.systemPackages`.

### MicroVM configuration

- Hypervisor: `vfkit` on macOS hosts, `qemu` otherwise.
- 4 vCPUs and 4096 MiB memory by default (`lib.mkDefault`, so modules can override them).
- On QEMU, `microvm.balloon` is enabled by default. It adds a virtio-balloon device with free page reporting, so
  memory freed in the guest is returned to the host after a few seconds (the guest reports free 2 MiB blocks and
  QEMU punches holes into the shared memfd backing guest RAM). vfkit doesn't support ballooning. Page cache counts
  as used memory, so it is not returned until the guest drops it.
- No control socket, fixed machine id (`lib.mkDefault`, like `system.stateVersion` and the host name).
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
- Networking uses systemd-networkd with DHCP on any ethernet interface (only with `bub.network.enable`). The
  firewall is enabled.
- Users are immutable. The VM user has the configured UID and subuid and subgid ranges for rootless containers.
  NixOS only allows UIDs >= 1000 for normal users, so UIDs below 1000 (macOS hosts) make it a system user instead,
  which is also why the ranges are set explicitly.
- With `bub.sudo.enable` the user is in `wheel` (passwordless sudo). A sudo rule always allows
  `systemctl poweroff`, which bub-vm uses to stop the VM.
- Podman is not enabled, see the README for an example.
- All terminfo entries are installed.

### SSH

sshd is socket activated and listens only on `vsock::22`. The firewall port is not opened. Authentication is by
public key only, read from `/run/bub/meta/authorized_keys`. Root login, agent forwarding, stream local forwarding
and X11 forwarding are disabled. TCP forwarding is limited to `local`. The variables in `bub.forwardEnv` are accepted from the client.

### Mounts

- `/run/bub/meta`: virtiofs tag `meta`, read-only, a regular NixOS file system. Contains `workdir`, `mounts`,
  `authorized_keys` and optionally `devenv.sh`.
- The project directory and all shares: mounted by `bub-mounts.service`, see below. Mirrored targets are only known
  at runtime, so none of them are NixOS file systems.

### bub-mounts.service

A oneshot service that runs after `/run/bub/meta` is mounted. It refuses a project path (`/run/bub/meta/workdir`)
equal to or inside the guest home directory. It then reads `/run/bub/meta/mounts`, one `<tag> <ro|rw> <target>`
line per mount, written by bub-vm in sorted order so parents are mounted before nested mounts. For each line it

- requires an absolute, normalized target that is neither `/`, a system directory nor the guest home directory,
- creates the target, as the VM user when it is inside the home directory, so parent directories like `~/.config`
  don't end up owned by root,
- mounts the virtiofs tag there unless it is already a mount point.

Finally it touches `/run/bub/ready`.

### bub-session

The command executed for every SSH session:

1. Waits up to 30 seconds for `/run/bub/ready`.
2. Changes into the project directory.
3. Sources `/etc/profile` if the NixOS environment is not set up yet, then `/run/bub/meta/devenv.sh` if present.
4. Runs the given command, or `bash -i` without arguments.

## bub_vm

| Module        | Content                                                                          |
|---------------|----------------------------------------------------------------------------------|
| `__main__.py` | Launcher, also allows `python3 -m bub_vm` from the source tree.                  |
| `cli.py`      | Argument parsing, `--stop`, `--destroy` and `--log`.                             |
| `state.py`    | `State`: the state directory of a project hash, its CID, MAC and status queries. |
| `vm.py`       | `Vm`: state setup, starting the hypervisor and virtiofsd, waiting and stopping.  |
| `shares.py`   | Path validation, share list and `meta/mounts`.                                   |
| `devshell.py` | Approving and building the devShell.                                             |
| `ssh.py`      | The isolated ssh invocation.                                                     |
| `session.py`  | Running a session, signal forwarding and the session locks.                      |
| `manage.py`   | `--list`, `--remove` and `--gc` over the state directories of all projects.      |
| `util.py`     | Logging, path matching, file, process and lock helpers.                          |

### Commands

| Invocation             | Action                                                           |
|------------------------|------------------------------------------------------------------|
| `bub-vm [cmd args...]` | Run a command (default: interactive shell) in the project VM.    |
| `bub-vm --stop`        | Stop the VM of the current directory.                            |
| `bub-vm --destroy`     | Stop the VM and delete its state directory, asks on a tty.       |
| `bub-vm --log`         | Print `vm.log` and `serial.log`.                                 |
| `bub-vm --list`        | List the VMs of all projects, see [Managing VMs](#managing-vms). |
| `bub-vm --remove VM... [--yes]` | Delete specific VMs, see [Managing VMs](#managing-vms). |
| `bub-vm --gc [--older-than DAYS] [--yes]` | Delete unused VMs, see [Managing VMs](#managing-vms). |

Environment variables: `BUB_DEVSHELL` (`0` or `1`) and `BUB_KEEP_RUNNING` (`0` or `1`) override
`bub.devShell.enable` and `bub.keepRunning`. Unset or empty means the configured default.

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
| `workdir`              | Project path, rewritten on every session start.           |
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
   - The host UID must equal `guestUid`, otherwise file ownership in shares would break.
   - Refuses to share `/`, `$HOME`, `~/.ssh`, `~/.gnupg`, `~/.config`, `~/.local`, `~/.local/share` and the
     bub-vm state directory.
   - Refuses paths that would shadow system directories in the guest (`/etc`, `/nix`, `/run`, ...) or the guest home directory.
   - Rejects paths with whitespace, commas, `=`, quotes, backslashes, `$`, backticks and glob characters, since
     they can't be passed safely in hypervisor arguments.
   - The state path must be shorter than 80 characters because of the unix socket path limit.
   - With QEMU, `/dev/kvm` and `/dev/vhost-vsock` must be writable.
2. `setup_state`: creates the state and `meta` directories (mode 700), writes the project path, generates the SSH
   key pair if missing and copies the public key to `meta/authorized_keys`.
3. `collect_shares`: builds the share list: `work` (project, mounted at its host path), `meta` and every share.
   Share sources get `~` expanded, are created if missing, resolved to physical paths and validated. Targets are
   resolved (relative to `guestHome`, absolute, or the resolved source) and normalized. Relative targets must stay
   inside the home directory, no target may be `/`, the home directory or a system directory, and two mounts can't
   share a target.
4. `write_mounts` writes `meta/mounts` for `bub-mounts.service`.
5. `prepare_devshell`, see [devShell](#devshell).
6. Takes the start lock. If the VM is not running, `start_vm`. If it is running with a different runner, a hint to
   restart it is logged.
7. Takes a shared session lock, releases the start lock and installs handlers that turn `HUP` and `TERM` into a
   regular exit.
8. `wait_ssh`, then runs `bub-session` via SSH with the shell-quoted arguments. A tty is requested when stdin and
   stdout are terminals. The exit code of the remote command is returned.

### devShell

The devShell is skipped when it is disabled (`bub.devShell.enable`, `BUB_DEVSHELL`), the project has no `flake.nix` or `nix` is not installed on
the host.

The project directory is writable from inside the VM, so its Nix files are untrusted and evaluating them runs
code on the host. Before loading, `approve_flake` hashes all `*.nix` files and `flake.lock` (tracked and untracked
but not ignored files in a git repository, otherwise only `flake.nix` and `flake.lock`) and compares the result to
`devshell.approved`. On a mismatch the user is asked for confirmation. Without a tty the devShell is skipped.

After approval, `devShells.<guest-system>.bub` is used if it exists, otherwise `.default`. It is built on the host
with `nix print-dev-env --profile` and written to `meta/devenv.sh`, which `bub-session` sources. Its store paths
reach the guest through the shared store. All nix calls disable import from derivation and `accept-flake-config`.

On any failure the devShell is removed and the session continues without it.

### Starting the VM

Old sockets and logs are removed first. Background processes are started with `detach`, which runs them in the
state directory, in a new session (`start_new_session`, so Ctrl-C and hangups of the terminal don't reach them),
with stdin from `/dev/null` and output appended to a log file. Lock files are not inherited, since Python opens files
as non-inheritable and `subprocess` closes all other descriptors.

Processes started by the current invocation are direct children of it and stay zombies after they exit, so
`kill(pid, 0)` would still succeed. Their liveness is therefore checked with `Popen.poll()`, which also reaps them.
Processes started by other invocations are checked with `kill(pid, 0)`.

QEMU (Linux):

- One `virtiofsd` per share (`--sandbox=none --cache=auto`): `ro-store` for `/nix/store`, `meta` and read-only
  home shares with `--readonly`, the others writable. bub-vm waits up to 5 seconds for all sockets to appear.
- Extra arguments: user mode networking with the project MAC (if `network` is set), `vhost-vsock-pci` with the project CID and a
  `vhost-user-fs-pci` device per share.

vfkit (macOS):

- NAT networking with the project MAC (if `network` is set).
- A vsock device that connects guest port 22 to the unix socket `ssh.sock`.
- A `virtio-fs` device per share. The read-only store share comes from the runner itself.

The arguments are joined with spaces and passed as `BUB_HYPERVISOR_ARGS`, the runner's `microvm-run` is detached and its PID and store
path are recorded in `vm.pid` and `vm.runner`.

### SSH

`ssh_command` builds an ssh invocation with an isolated configuration:

- `-F /dev/null` ignores the user SSH config, `SSH_AUTH_SOCK` is unset and only the project key is used.
- `SHELL` is set to `/bin/sh`, because ssh runs the proxy command with `$SHELL`, which may not be a POSIX shell.
- Agent, X11 and all other forwardings are disabled.
- Host key checking is off and no known hosts file is used. The transport is a local vsock or unix socket.
- The proxy command is `socat - VSOCK-CONNECT:<cid>:22` (QEMU) or `socat - UNIX-CONNECT:ssh.sock` (vfkit).
- The variables in `forwardEnv` are sent with `SendEnv`.

`wait_ssh` polls for up to about 90 seconds. If the VM process exits in the meantime, the last console output is
printed.

The interactive session runs ssh as a child process. While it runs, `INT`, `HUP` and `TERM` are forwarded to ssh
instead of interrupting bub-vm, so cleanup only starts once ssh has exited. The exit code of the remote command is
returned, or `128 + signal` if ssh was killed by a signal.

### Stopping the VM

`stop_vm` sends `systemctl poweroff` through SSH and waits up to 30 seconds for the hypervisor to exit. After that
it sends `SIGTERM` and, 2 seconds later, `SIGKILL`. Finally the virtiofsd processes are killed and the PID files and
sockets are removed.

### Sessions and locking

Two lock files coordinate concurrent invocations in the same project:

- `start.lock` (exclusive `flock`): serializes starting and stopping the VM.
- `sessions.lock` (shared `flock`): held by every running session.

When a session ends, `finish` (run from a `finally` block) releases its shared lock, takes the start lock and tries
to take the session lock exclusively without blocking. This only succeeds when no other session is active, in which
case the VM is stopped. With `keepRunning` this step is skipped. Ctrl-C before the interactive session exits
with code 130, `HUP` and `TERM` with 129 and 143, and `finish` runs in all cases.

### Managing VMs

`--list`, `--remove` and `--gc` don't need `BUB_VM_CONFIG` (`--remove` only to stop a running VM) and work on every directory under the state root whose name is a
12 digit hex hash. For each of them:

- The project is read from `workdir`. It counts as missing if the file is absent or the directory doesn't exist.
- The last use is the mtime of `workdir`, i.e. the start of the last session.
- A VM is in use if the PID in `vm.pid` is alive or `sessions.lock` can't be locked exclusively.
- The disk usage is the allocated size (`st_blocks`) of all files, so the sparse `root.img` only counts the
  blocks that were written. Symlinks (the devShell profile) are not followed.

`--gc` deletes the state directories that are not in use and whose project is missing or, with
`--older-than DAYS`, that were last used more than `DAYS` days ago. It lists them and asks for confirmation,
which `--yes` skips. Without a tty `--yes` is required. Each directory is deleted while holding `start.lock`
(directories whose lock is held are skipped), after checking again that it is still unused.

`--remove` takes one or more VMs, each given as a hash, a unique prefix of one, or a project path (anything with a
`/`, or `.`/`..`, which is resolved to a physical path and hashed, so it also works after the project was deleted).
It lists them and asks for confirmation (`--yes` skips it and is required without a tty). Each VM is then deleted
while holding `start.lock`, and running VMs are stopped first like with `--destroy`.
