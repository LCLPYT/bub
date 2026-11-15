# bub

A general sandboxing setup using a micro vm.
Utilizes [Nix](https://nixos.org/download/) in order to set up reusable microvms.

NixOS is being used as guest system.
The guest system is built on the host and then mounted into the VM.

Each process gets its own VM instance, depending on the working directory you run them in.

## Why a micro vm?

There a few different sandboxing approaches, each with pros and cons.

Using containers, you can get good sandboxing with filesystem isolation. 
However if you ever need to run more complex setups, such as a coding agent, they may need to interact with Docker / Podman themselves in order to be more effective at what they are doing.
Docker-in-Docker would be possible, but also very painful to setup, so this is maybe not ideal.

Using a VM makes it possible to run a complete Linux system and provides a way to do nearly everything that you can do on a real host.
Additonally, using a hypervisor accomplishes strong isolation from programs like coding agents from your host file system.

**TLDR**: Use Micro VMs, because:
- stronger isolation compared to containers
- more flexibility in running software like Podman inside the sandbox

## Prerequisites
- a modern Linux system or macOS (Apple Silicon)
- Nix package manager

On macOS, you'll also need a Linux aarch64 builder. It can be set up using [nix-darwin](https://github.com/nix-darwin/nix-darwin), for example.

## Try it
You can try the sandbox without installing it using nix:

```bash
nix run github:LCLPYT/bub?dir=microvm -- bash
```

## Installation

### Default config
You can install the default configuration system-wide (no customization; see below for customizing the vm):

```bash
nix profile install github:LCLPYT/bub?dir=microvm
```

### Via home-manager (supports customization)

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

### Generic flake + profile (e.g. on non-NixOS systems)
You can write a small flake to customize the vm config and install it as profile:

```
# ~/.config/bub-vm/flake.nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    bub-vm = {
      url = "github:LCLPYT/bub?dir=microvm";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { bub-vm, ... }: {
    packages.x86_64-linux.default = bub-vm.lib.mkBub {
      system = "x86_64-linux";
      modules = [
        ({ options, pkgs, ... }: {
          bub.packages = options.bub.packages.default ++ with pkgs; [ nodejs ];
          microvm.mem = 8192;
        })
      ];
    };
  };
}
```

Then:

```
nix profile install ~/.config/bub-vm               # first install
nix flake update --flake ~/.config/bub-vm          # bump bub/nixpkgs
nix profile upgrade --all                          # rebuild after editing or updating
```

### Example: Claude Code

If you want to use Claude Code in the VM, you can add it as general package in the guest:

```nix
(inputs.bub-vm.lib.mkBub {
  system = pkgs.stdenv.hostPlatform.system;
  modules = [
    ({ config, lib, options, pkgs, ... }: {
      nixpkgs.config.allowUnfreePredicate = pkg: lib.getName pkg == "claude-code";

      bub.packages = options.bub.packages.default ++ [ pkgs.claude-code ];

      # mounts ~/.local/share/bub-vm/claude on the host at ~/.claude in the vm
      bub.homeShares.claude = {
        source = "~/.local/share/bub-vm/claude";
        target = ".claude";
      };

      environment.variables = {
        CLAUDE_CONFIG_DIR = "/home/${config.bub.user}/.claude";
        DISABLE_AUTOUPDATER = "1";
        DISABLE_TELEMETRY = "1";
        DISABLE_ERROR_REPORTING = "1";
      };
    })
  ];
})
```

Home shares are created on the host if they don't exist yet.

## Usage

```bash
bub-vm make test     # run a program
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

The nix flake utilizes [microvm.nix](https://github.com/microvm-nix/microvm.nix) in order to setup the micro vms.

Using virtiofs is crucial for this setup, so the following hypervisors are chosen:

| Host             | Hypervisor | Guest           |
|------------------|------------|-----------------|
| `x86_64-linux`   | QEMU (KVM) | `x86_64-linux`  |
| `aarch64-linux`  | QEMU (KVM) | `aarch64-linux` |
| `aarch64-darwin` | vfkit      | `aarch64-linux` |

- `flake.nix` evaluates a NixOS system (`guest.nix`) for the guest architecture on the host and wraps its microvm.nix runner in
  the `bub-vm` Python script (`wrapper.nix`, `bub-vm.py`).
- microvm.nix bakes share paths into the runner at build time. Project-specific devices (project dir, the `meta`
  dir, home shares, NIC, vsock) are added at runtime through `microvm.extraArgsScript`.
- On Linux, `bub-vm` starts one unprivileged `virtiofsd` per share. vfkit has virtio-fs built in.
- The guest mounts the `meta` share at `/run/bub/meta`. It contains the project path, the SSH public key and the
  devShell environment. `bub-workdir.service` mounts the project at its
  host path.
- SSH (for a proper TTY with resizing) runs over vsock: `socat VSOCK-CONNECT` on Linux, the vfkit vsock unix socket on macOS. 
  sshd in the guest is socket-activated on `vsock::22`, and the key pair is generated per project.
- The guest user has the same UID as the host user (`bub.uid`, default 1000 on Linux, 501 on macOS), so files created in the project keep the right owner.

## Future improvements
- Ballooning (+ free page reporting): free memory allocated by the vm when not needed
- Cleanup of stale VM disks
