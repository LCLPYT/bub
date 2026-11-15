"""Approving, building and exporting the devShell of the project flake."""
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .util import confirm, env_flag, log, read_text

NIX = [
    "nix",
    "--extra-experimental-features", "nix-command flakes",
    "--option", "allow-import-from-derivation", "false",
    "--option", "accept-flake-config", "false",
]


def flake_hash(workdir):
    inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=workdir,
                            capture_output=True, text=True)

    if inside.returncode == 0 and inside.stdout.strip() == "true":
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.nix", "flake.lock"],
            cwd=workdir, capture_output=True,
        ).stdout
        files = sorted({name for name in listed.split(b"\0") if name})
    else:
        files = [b"flake.nix", b"flake.lock"]

    digest = hashlib.sha256()
    for name in files:
        try:
            content = (Path(workdir) / os.fsdecode(name)).read_bytes()
        except OSError:
            continue
        digest.update(hashlib.sha256(content).hexdigest().encode() + b"  " + name + b"\n")
    return digest.hexdigest()


def approve_flake(vm):
    """The project dir is writable from the guest, so its Nix files must be approved before the host evaluates them."""
    digest = flake_hash(vm.workdir)
    approved = vm.state / "devshell.approved"
    if digest == read_text(approved):
        return True

    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        log("the Nix files of this project are not approved, skipping the devShell "
            "(run bub-vm interactively to approve)")
        return False

    log("the Nix files of this project changed since they were last approved (or were never approved).")
    log("loading the devShell evaluates and builds them on the host, outside of the VM.")
    if not confirm(f"load the devShell of {vm.workdir}?"):
        return False

    approved.write_text(f"{digest}\n")
    return True


def prepare_devshell(vm):
    devenv = vm.state.meta / "devenv.sh"
    if not build_devshell(vm, devenv):
        devenv.unlink(missing_ok=True)


def build_devshell(vm, devenv):
    if not env_flag("BUB_DEVSHELL", vm.config["devShell"]) or not (Path(vm.workdir) / "flake.nix").is_file():
        return False

    if shutil.which("nix") is None:
        log("nix not found on the host, skipping the devShell")
        return False

    if not approve_flake(vm):
        return False

    system = vm.config["guestSystem"]
    evaluated = subprocess.run(
        [*NIX, "eval", "--raw", f"{vm.workdir}#devShells.{system}", "--apply",
         'shells: if shells ? bub then "bub" else if shells ? default then "default" else ""'],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    attr = evaluated.stdout if evaluated.returncode == 0 else ""
    if not attr:
        log(f"no devShells.{system}.{{bub,default}} found (or the flake failed to evaluate), skipping the devShell")
        return False

    installable = f"{vm.workdir}#devShells.{system}.{attr}"
    log(f"loading {installable}")

    tmp = vm.state.meta / "devenv.sh.tmp"
    with open(tmp, "wb") as out:
        built = subprocess.run([*NIX, "print-dev-env", "--profile", str(vm.state / "devshell"), installable],
                               stdout=out)
    if built.returncode != 0:
        log("building the devShell failed, continuing without it")
        tmp.unlink(missing_ok=True)
        return False

    tmp.replace(devenv)
    return True
