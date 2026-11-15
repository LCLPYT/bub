"""SSH into the guest over vsock (QEMU) or a unix socket (vfkit), with an isolated configuration."""
import os
import subprocess

DEVNULL = subprocess.DEVNULL


def ssh_command(vm, *args):
    if vm.hypervisor == "qemu":
        proxy = f"socat - VSOCK-CONNECT:{vm.state.cid}:22"
    else:
        proxy = f"socat - UNIX-CONNECT:{vm.state.path}/ssh.sock"

    return [
        "ssh",
        "-F", "/dev/null",
        "-i", str(vm.state.key),
        "-l", vm.config["guestUser"],
        "-o", "IdentitiesOnly=yes",
        "-o", "IdentityAgent=none",
        "-o", "ForwardAgent=no",
        "-o", "ForwardX11=no",
        "-o", "ClearAllForwardings=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", f"ProxyCommand={proxy}",
        *args,
    ]


def ssh_env():
    env = dict(os.environ)
    env.pop("SSH_AUTH_SOCK", None)
    # ssh runs the ProxyCommand with $SHELL, which may not be a POSIX shell.
    env["SHELL"] = "/bin/sh"
    return env


def ssh_quiet(vm, *args):
    return subprocess.run(ssh_command(vm, "-T", *args), env=ssh_env(),
                          stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL).returncode
