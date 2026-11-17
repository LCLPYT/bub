"""Validation of the project path and the list of virtio-fs shares mounted in the guest."""
import os

from .util import check_path, die, matches


def validate(vm):
    config = vm.config
    uid = config["guestUid"]

    if os.getuid() != uid:
        die(f"host uid {os.getuid()} differs from the VM uid {uid}, set bub.uid in your configuration")

    home = vm.home

    sensitive = [
        "/", home, f"{home}/.ssh*", f"{home}/.gnupg*", f"{home}/.config", f"{home}/.local",
        f"{home}/.local/share", f"{vm.state.root}*",
    ]

    if matches(vm.workdir, sensitive):
        die(f"refusing to share '{vm.workdir}' with the VM")

    if matches(vm.workdir, config["systemDirs"]):
        die(f"'{vm.workdir}' would shadow a system directory inside the VM")

    guest_home = config["guestHome"]

    if matches(vm.workdir, [guest_home, f"{guest_home}/*"]):
        die(f"'{vm.workdir}' would shadow the home directory of the VM user")

    check_path("project path", vm.workdir)
    check_path("state path", str(vm.state.path))

    if len(str(vm.state.path)) >= 80:
        die(f"state path is too long for unix sockets: {vm.state.path}")

    if vm.hypervisor == "qemu":
        if not os.access("/dev/kvm", os.W_OK):
            die("/dev/kvm is not accessible")
        if not os.access("/dev/vhost-vsock", os.W_OK):
            die("/dev/vhost-vsock is not accessible, try 'modprobe vhost_vsock'")


def collect_shares(vm):
    """Returns (tag, source, readonly, target) tuples. The meta share is mounted by the guest itself."""
    shares = [("work", vm.workdir, False, vm.workdir), ("meta", str(vm.state.meta), True, None)]

    for share in vm.config["shares"]:
        tag, source = share["tag"], expand_home(vm, share["source"])

        if not share_applies(vm, share.get("when")):
            continue

        os.makedirs(source, exist_ok=True)

        source = os.path.realpath(source)

        check_path(f"share {tag}", source)

        target = share_target(vm.config, tag, share["target"], source)

        if source == vm.workdir and target == vm.workdir:
            continue

        shares.append((tag, source, share["readOnly"], target))

    targets = [target for *_, target in shares if target is not None]

    for target in sorted(set(targets)):
        if targets.count(target) > 1:
            die(f"more than one share is mounted at {target}")

    return shares


def expand_home(vm, path):
    if path == "~" or path.startswith("~/"):
        return vm.home + path[1:]
    return path


def share_applies(vm, when):
    """`when` lists project path patterns, where a trailing `*` matches any suffix. None means always."""
    if when is None:
        return True

    patterns = []

    for pattern in when:
        pattern = expand_home(vm, pattern)
        wildcard = pattern.endswith("*")
        base = pattern.rstrip("*")

        if base.endswith("/") or not wildcard:
            base = os.path.realpath(base) + ("/" if base.endswith("/") else "")

        patterns.append(base + ("*" if wildcard else ""))

    return matches(vm.workdir, patterns)


def share_target(config, tag, target, source):
    home = config["guestHome"]

    if target is None:
        path = source
    elif target.startswith("/"):
        path = os.path.normpath(target)
    else:
        path = os.path.normpath(f"{home}/{target}")

        if not path.startswith(f"{home}/"):
            die(f"share {tag}: relative target '{target}' must stay inside {home}")

    check_path(f"share {tag} target", path)

    if matches(path, ["/", home, *config["systemDirs"]]):
        die(f"share {tag}: can't mount at '{path}', it would shadow a system or the home directory inside the VM")

    return path


def write_mounts(vm, shares):
    """The guest mounts these in order, so parents (which sort first) are mounted before nested shares."""
    mounts = sorted((target, tag, "ro" if readonly else "rw") for tag, _, readonly, target in shares if target)
    text = "".join(f"{tag} {mode} {target}\n" for target, tag, mode in mounts)
    (vm.state.meta / "mounts").write_text(text)
