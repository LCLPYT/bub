# bub

A general sandboxing setup using a micro vm.
Utilizes [nix](https://nixos.org/download/) in order to setup a reusable microvm with NixOS as guest system.

## Why a micro vm?

There a few sandboxing approaches, each with pros and cons.
Using containers, you can get good sandboxing with filesystem isolation. 
However if you ever need to run more complex setups such as a coding agent, they may need to interact with Docker / Podman themselves.
Docker-in-Docker is painful to setup, so this is maybe not ideal.
Using a VM makes it possible to run a complete Linux system and provides a way to do nearly everything that you can do on a real host.
Additonally, using a hypervisor accomplishes strong isolation from programs like coding agents from your host file system.

**TLDR**: Use Micro VMs, because:
- stronger isolation compared to containers
- more flexibility in running software like Podman inside the sandbox

## MicroVM setup explained
This setup is packaged as a nix-flake that you can add to your system.
It utilized [microvm.nix](https://github.com/microvm-nix/microvm.nix) in order to setup the micro vm.

Microvm.nix supports different hypervisors, however this project puts emphasis on using virtiofs, thus the following hypervisors are used:

| Host             | Hypervisor | Guest           |
|------------------|------------|-----------------|
| `x86_64-linux`   | QEMU (KVM) | `x86_64-linux`  |
| `aarch64-linux`  | QEMU (KVM) | `aarch64-linux` |
| `aarch64-darwin` | vfkit      | `aarch64-linux` |

### Isolation

- The current directory is mounted at the **same path** inside the VM
- You can share additional directories from your system (configurable)
- The host `/nix/store` is shared read-only, so the VM needs no store image of its own.
- No host sockets are reachable: no SSH agent, Wayland, D-Bus or Podman socket.
- Networking is **unrestricted** NAT for now (QEMU user networking / Virtualization.framework NAT).
  The VM can reach the internet, the LAN and, with QEMU, services on the host loopback via `10.0.2.2`.

## Prerequisites
- modern Linux or macOS (Apple Silicon)
- Nix package manager

On macOS, you'll also need a Linux aarch64 builder. It can be set up using [nix-darwin](https://github.com/nix-darwin/nix-darwin), for example.

## Try it
You can try the sandbox without installing it using nix:

```bash
nix run github:LCLPYT/bub -- bash
```

## Installation

Per system:

```bash
nix profile install github:LCLPYT/bub
```

Or via home-manager (supports customization):

```nix
home.packages = [
  (inputs.bub-vm.lib.mkBub {
    system = pkgs.stdenv.hostPlatform.system;
    modules = [
      ({ pkgs, ... }: {
        # custom user id within the vm
        bub.uid = 1001;

        # customize packages installed in the vm
        bub.packages = with pkgs; [ 
          claude-code 
          git 
          nodejs 
        ];

        # customize ram
        microvm.mem = 8192;
      })
    ];
  })
];
```

## Usage

```bash
bub-vm claude        # run a program
bub-vm               # interactive shell
bub-vm --stop        # stop the VM of this directory
bub-vm --destroy     # delete the VM disk of this directory
bub-vm --log         # console output
```

Each project directory gets its own VM with a persistent, sparse root disk under `~/.local/state/bub-vm/<hash>/`. 
The VM is started by the first session and stopped after the last one (`BUB_KEEP_RUNNING=1` keeps it running). 
More sessions in the same directory share the running VM.

## Project tools

If the project has a `flake.nix` with `devShells.<guest-system>.bub` (or `.default`), it is built **on the host**
with `nix print-dev-env` and sourced in every session. Its store paths reach the VM through the shared store.

Because the project directory is writable from inside the VM, its Nix files are untrusted: building them runs code
on the host (inside the Nix build sandbox on Linux, which is off by default on macOS). bub-vm therefore asks for
confirmation whenever the `*.nix` files or `flake.lock` change. Import from derivation is disabled. Set
`BUB_DEVSHELL=0` to skip the devShell.

Nix itself is not available inside the VM.

## How it works

- `flake.nix` evaluates a NixOS system (`guest.nix`) for the guest architecture and wraps its microvm.nix runner in
  the `bub-vm` script (`wrapper.nix`, `bub-vm.sh`).
- microvm.nix bakes share paths into the runner at build time. Project-specific devices (project dir, the `meta`
  dir, home shares, NIC, vsock) are added at runtime through `microvm.extraArgsScript`.
- On Linux, `bub-vm` starts one unprivileged `virtiofsd` per share. vfkit has virtio-fs built in.
- The guest mounts the `meta` share at `/run/bub/meta`. It contains the project path, the SSH public key, the
  devShell environment and your git `user.name`/`user.email`. `bub-workdir.service` mounts the project at its
  host path.
- SSH (for a proper TTY with resizing) runs over vsock: `socat VSOCK-CONNECT` on Linux, the vfkit vsock unix socket on macOS. 
  sshd in the guest is socket-activated on `vsock::22`, and the key pair is generated per project.
- The guest user has the same UID as the host user (`bub.uid`, default 1000 on Linux, 501 on macOS), so files created in the project keep the right owner.
