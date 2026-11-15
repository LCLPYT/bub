import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

USAGE = """\
Usage: bub-vm [program [args...]]  run a program (default: bash) in the VM of the current directory
       bub-vm --stop               stop the VM of the current directory
       bub-vm --destroy            stop the VM and delete its disk and state
       bub-vm --log                print the console log of the VM

Environment:
  BUB_DEVSHELL=0       don't load the devShell of the project flake
  BUB_KEEP_RUNNING=1   keep the VM running after the last session ends
"""

NIX = [
    "nix",
    "--extra-experimental-features", "nix-command flakes",
    "--option", "allow-import-from-derivation", "false",
]
SESSION = "/run/current-system/sw/bin/bub-session"
POWEROFF = "/run/wrappers/bin/sudo /run/current-system/sw/bin/systemctl poweroff"
SYSTEM_DIRS = [
    "/bin*", "/boot*", "/dev*", "/etc*", "/lib*", "/nix*", "/proc*", "/root*",
    "/run*", "/sbin*", "/sys*", "/tmp", "/usr*", "/var", "/var/lib*", "/var/log*",
]
UNSAFE_CHARS = re.compile(r"[\s,='\"\\$`*?\[\]]")
DEVNULL = subprocess.DEVNULL


def log(message):
    print(f"[bub-vm] {message}", file=sys.stderr, flush=True)


def die(message):
    log(f"error: {message}")
    sys.exit(1)


def confirm(question):
    sys.stderr.write(f"[bub-vm] {question} [y/N] ")
    sys.stderr.flush()
    return sys.stdin.readline().strip() in ("y", "Y", "yes")


def matches(path, patterns):
    """Matches like a shell `case`, where a trailing `*` matches any suffix."""
    for pattern in patterns:
        if pattern.endswith("*") and path.startswith(pattern[:-1]):
            return True
        if path == pattern:
            return True
    return False


def check_path(what, path):
    if UNSAFE_CHARS.search(path):
        die(f"{what} contains characters that can't be passed to the hypervisor: {path}")


def wait_until(predicate, attempts, delay):
    for _ in range(attempts):
        if predicate():
            return True
        time.sleep(delay)
    return predicate()


def read_text(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def tail(path, count):
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return
    print(f"==> {path} <==", file=sys.stderr)
    for line in lines[-count:]:
        print(line, file=sys.stderr)


def kill(pid, signum):
    try:
        os.kill(pid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def open_lock(path, operation):
    lock = open(path, "a")
    fcntl.flock(lock, operation)
    return lock


class Vm:
    def __init__(self, config):
        self.config = config
        self.hypervisor = config["hypervisor"]
        self.runner = config["runner"]
        self.home = os.environ["HOME"]
        self.workdir = os.getcwd()

        state_home = os.environ.get("XDG_STATE_HOME") or f"{self.home}/.local/state"
        self.state_root = Path(state_home) / "bub-vm"

        digest = hashlib.sha256(self.workdir.encode()).hexdigest()[:12]
        self.state = self.state_root / digest
        self.meta = self.state / "meta"
        self.key = self.state / "id_ed25519"
        self.cid = int(digest[:7], 16) + 3
        self.mac = "02:" + ":".join(digest[i:i + 2] for i in range(0, 10, 2))

        # Processes started by this invocation. Once they exit they stay zombies until reaped,
        # so a plain kill(pid, 0) would still report them as running.
        self.children = {}

    def validate(self):
        uid = self.config["guestUid"]
        if os.getuid() != uid:
            die(f"host uid {os.getuid()} differs from the VM uid {uid}, set bub.uid in your configuration")

        home = self.home
        sensitive = [
            "/", home, f"{home}/.ssh*", f"{home}/.gnupg*", f"{home}/.config", f"{home}/.local",
            f"{home}/.local/share", f"{self.state_root}*",
        ]
        if matches(self.workdir, sensitive):
            die(f"refusing to share '{self.workdir}' with the VM")
        if matches(self.workdir, SYSTEM_DIRS):
            die(f"'{self.workdir}' would shadow a system directory inside the VM")

        check_path("project path", self.workdir)
        check_path("state path", str(self.state))
        if len(str(self.state)) >= 80:
            die(f"state path is too long for unix sockets: {self.state}")

        if self.hypervisor == "qemu":
            if not os.access("/dev/kvm", os.W_OK):
                die("/dev/kvm is not accessible")
            if not os.access("/dev/vhost-vsock", os.W_OK):
                die("/dev/vhost-vsock is not accessible, try 'modprobe vhost_vsock'")

    def setup_state(self):
        self.meta.mkdir(parents=True, exist_ok=True)
        self.state.chmod(0o700)
        self.meta.chmod(0o700)
        (self.state / "workdir").write_text(f"{self.workdir}\n")
        (self.meta / "workdir").write_text(f"{self.workdir}\n")

        if not self.key.exists():
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "bub-vm", "-f", str(self.key)],
                           check=True)

        authorized_keys = self.meta / "authorized_keys"
        shutil.copyfile(f"{self.key}.pub", authorized_keys)
        authorized_keys.chmod(0o600)

    def collect_shares(self):
        shares = [("work", self.workdir), ("meta", str(self.meta))]

        for share in self.config["homeShares"]:
            tag, source = share["tag"], share["source"]
            if source == "~" or source.startswith("~/"):
                source = self.home + source[1:]

            os.makedirs(source, exist_ok=True)
            source = os.path.realpath(source)
            check_path(f"share {tag}", source)
            shares.append((tag, source))

        return shares

    def flake_hash(self):
        inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=self.workdir,
                                capture_output=True, text=True)

        if inside.returncode == 0 and inside.stdout.strip() == "true":
            listed = subprocess.run(
                ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.nix", "flake.lock"],
                cwd=self.workdir, capture_output=True,
            ).stdout
            files = sorted({name for name in listed.split(b"\0") if name})
        else:
            files = [b"flake.nix", b"flake.lock"]

        digest = hashlib.sha256()
        for name in files:
            try:
                content = (Path(self.workdir) / os.fsdecode(name)).read_bytes()
            except OSError:
                continue
            digest.update(hashlib.sha256(content).hexdigest().encode() + b"  " + name + b"\n")
        return digest.hexdigest()

    def approve_flake(self):
        """The project dir is writable from the guest, so its Nix files must be approved before the host evaluates them."""
        digest = self.flake_hash()
        approved = self.state / "devshell.approved"
        if digest == read_text(approved):
            return True

        if not (sys.stdin.isatty() and sys.stderr.isatty()):
            log("the Nix files of this project are not approved, skipping the devShell "
                "(run bub-vm interactively to approve)")
            return False

        log("the Nix files of this project changed since they were last approved (or were never approved).")
        log("loading the devShell evaluates and builds them on the host, outside of the VM.")
        if not confirm(f"load the devShell of {self.workdir}?"):
            return False

        approved.write_text(f"{digest}\n")
        return True

    def prepare_devshell(self):
        devenv = self.meta / "devenv.sh"
        if not self.build_devshell(devenv):
            devenv.unlink(missing_ok=True)

    def build_devshell(self, devenv):
        if os.environ.get("BUB_DEVSHELL", "1") != "1" or not (Path(self.workdir) / "flake.nix").is_file():
            return False

        if shutil.which("nix") is None:
            log("nix not found on the host, skipping the devShell")
            return False

        if not self.approve_flake():
            return False

        system = self.config["guestSystem"]
        evaluated = subprocess.run(
            [*NIX, "eval", "--raw", f"{self.workdir}#devShells.{system}", "--apply",
             'shells: if shells ? bub then "bub" else if shells ? default then "default" else ""'],
            stdout=subprocess.PIPE, stderr=DEVNULL, text=True,
        )
        attr = evaluated.stdout if evaluated.returncode == 0 else ""
        if not attr:
            log(f"no devShells.{system}.{{bub,default}} found (or the flake failed to evaluate), skipping the devShell")
            return False

        installable = f"{self.workdir}#devShells.{system}.{attr}"
        log(f"loading {installable}")

        tmp = self.meta / "devenv.sh.tmp"
        with open(tmp, "wb") as out:
            built = subprocess.run([*NIX, "print-dev-env", "--profile", str(self.state / "devshell"), installable],
                                   stdout=out)
        if built.returncode != 0:
            log("building the devShell failed, continuing without it")
            tmp.unlink(missing_ok=True)
            return False

        tmp.replace(devenv)
        return True

    def detach(self, logname, command, env=None):
        """Starts a command in a new session, so terminal signals (Ctrl-C, hangup) don't reach it.

        The command runs in the state dir, which microvm.nix expects for relative paths.
        """
        with open(self.state / logname, "ab") as logfile:
            try:
                process = subprocess.Popen(command, cwd=self.state, env=env, stdin=DEVNULL, stdout=logfile,
                                           stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as error:
                die(f"failed to start {command[0]}: {error}")
        self.children[process.pid] = process
        return process.pid

    def alive(self, pid):
        child = self.children.get(pid)
        if child is not None:
            return child.poll() is None
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def vm_pid(self):
        try:
            pid = int(read_text(self.state / "vm.pid"))
        except ValueError:
            return None
        return pid if self.alive(pid) else None

    def start_virtiofsd(self, tag, source, readonly=False):
        command = ["virtiofsd", f"--socket-path=virtiofs-{tag}.sock", f"--shared-dir={source}",
                   "--sandbox=none", "--cache=auto"]
        if readonly:
            command.append("--readonly")

        pid = self.detach("virtiofsd.log", command)
        with open(self.state / "virtiofsd.pids", "a") as pids:
            pids.write(f"{pid}\n")

    def qemu_args(self, shares):
        self.start_virtiofsd("ro-store", "/nix/store", readonly=True)
        sockets = [self.state / "virtiofs-ro-store.sock"]
        args = [
            "-netdev", "user,id=bubnet",
            "-device", f"virtio-net-pci,netdev=bubnet,mac={self.mac}",
            "-device", f"vhost-vsock-pci,guest-cid={self.cid}",
        ]

        for tag, source in shares:
            self.start_virtiofsd(tag, source, readonly=tag == "meta")
            sockets.append(self.state / f"virtiofs-{tag}.sock")
            args += [
                "-chardev", f"socket,id=bub-{tag},path=virtiofs-{tag}.sock",
                "-device", f"vhost-user-fs-pci,chardev=bub-{tag},tag={tag}",
            ]

        if not wait_until(lambda: all(socket.is_socket() for socket in sockets), 100, 0.05):
            die(f"virtiofsd did not start, see {self.state}/virtiofsd.log")
        return args

    def vfkit_args(self, shares):
        args = [
            "--device", f"virtio-net,nat,mac={self.mac}",
            "--device", f"virtio-vsock,port=22,socketURL={self.state}/ssh.sock,connect",
        ]
        for tag, source in shares:
            args += ["--device", f"virtio-fs,sharedDir={source},mountTag={tag}"]
        return args

    def start_vm(self, shares):
        log(f"starting VM for {self.workdir}")
        for socket in self.state.glob("*.sock"):
            socket.unlink(missing_ok=True)
        (self.state / "serial.log").unlink(missing_ok=True)
        (self.state / "virtiofsd.pids").unlink(missing_ok=True)
        (self.state / "vm.log").write_bytes(b"")

        if self.hypervisor == "qemu":
            args = self.qemu_args(shares)
        elif self.hypervisor == "vfkit":
            args = self.vfkit_args(shares)
        else:
            die(f"unsupported hypervisor {self.hypervisor}")

        # microvm.nix word-splits the output of extraArgsScript, which prints this variable.
        env = dict(os.environ, BUB_HYPERVISOR_ARGS=" ".join(args))
        pid = self.detach("vm.log", [f"{self.runner}/bin/microvm-run"], env=env)
        (self.state / "vm.pid").write_text(f"{pid}\n")
        (self.state / "vm.runner").write_text(f"{self.runner}\n")

    def ssh_command(self, *args):
        if self.hypervisor == "qemu":
            proxy = f"exec socat - VSOCK-CONNECT:{self.cid}:22"
        else:
            proxy = f"exec socat - UNIX-CONNECT:{self.state}/ssh.sock"

        return [
            "ssh",
            "-F", "/dev/null",
            "-i", str(self.key),
            "-l", self.config["guestUser"],
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

    def ssh_env(self):
        env = dict(os.environ)
        env.pop("SSH_AUTH_SOCK", None)
        # ssh runs the ProxyCommand with $SHELL, which may not be a POSIX shell.
        env["SHELL"] = "/bin/sh"
        return env

    def ssh_quiet(self, *args):
        return subprocess.run(self.ssh_command("-T", *args), env=self.ssh_env(),
                              stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL).returncode

    def wait_ssh(self):
        for _ in range(300):
            if self.ssh_quiet("-o", "ConnectTimeout=2", "bub-vm", "true") == 0:
                return

            if self.vm_pid() is None:
                log("the VM exited, last console output:")
                tail(self.state / "vm.log", 40)
                tail(self.state / "serial.log", 40)
                sys.exit(1)

            time.sleep(0.3)

        die("the VM did not become reachable, see 'bub-vm --log'")

    def stop_vm(self):
        pid = self.vm_pid()
        if pid is None:
            return

        log("stopping VM")
        self.ssh_quiet("-o", "ConnectTimeout=5", "bub-vm", POWEROFF)

        if not wait_until(lambda: not self.alive(pid), 100, 0.3):
            log("the VM did not shut down, terminating it")
            kill(pid, signal.SIGTERM)
            time.sleep(2)
            if self.alive(pid):
                kill(pid, signal.SIGKILL)

        for line in read_text(self.state / "virtiofsd.pids").split():
            if line.isdigit():
                kill(int(line), signal.SIGTERM)

        for name in ("vm.pid", "virtiofsd.pids"):
            (self.state / name).unlink(missing_ok=True)
        for socket in self.state.glob("*.sock"):
            socket.unlink(missing_ok=True)

    def interactive(self, command):
        remote = SESSION
        if command:
            remote += " " + shlex.join(command)
        tty = "-t" if sys.stdin.isatty() and sys.stdout.isatty() else "-T"

        process = subprocess.Popen(
            self.ssh_command(tty, "-o", f"SetEnv=COLORTERM={os.environ.get('COLORTERM', '')}", "bub-vm", remote),
            env=self.ssh_env(),
        )

        def forward(signum, _frame):
            process.send_signal(signum)

        handled = (signal.SIGINT, signal.SIGHUP, signal.SIGTERM)
        previous = {signum: signal.signal(signum, forward) for signum in handled}
        try:
            code = process.wait()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)

        return 128 - code if code < 0 else code


def exit_on_signal(signum, _frame):
    sys.exit(128 + signum)


def finish(vm, start_lock, sessions_lock):
    """Stops the VM if this was the last session, i.e. no other session holds a shared lock."""
    fcntl.flock(sessions_lock, fcntl.LOCK_UN)
    if os.environ.get("BUB_KEEP_RUNNING", "0") == "1":
        return

    fcntl.flock(start_lock, fcntl.LOCK_EX)
    try:
        fcntl.flock(sessions_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    else:
        vm.stop_vm()
    fcntl.flock(start_lock, fcntl.LOCK_UN)


def run_session(vm, command):
    vm.validate()
    vm.setup_state()
    shares = vm.collect_shares()
    vm.prepare_devshell()

    start_lock = open_lock(vm.state / "start.lock", fcntl.LOCK_EX)
    if vm.vm_pid() is None:
        vm.start_vm(shares)
        log("waiting for the VM to boot")
    elif read_text(vm.state / "vm.runner") != vm.runner:
        log("the running VM uses an older build of bub-vm, run 'bub-vm --stop' to restart it")

    sessions_lock = open_lock(vm.state / "sessions.lock", fcntl.LOCK_SH)
    fcntl.flock(start_lock, fcntl.LOCK_UN)

    signal.signal(signal.SIGHUP, exit_on_signal)
    signal.signal(signal.SIGTERM, exit_on_signal)
    try:
        vm.wait_ssh()
        return vm.interactive(command)
    finally:
        finish(vm, start_lock, sessions_lock)


def main(args):
    config_path = os.environ.get("BUB_VM_CONFIG")
    if not config_path:
        die("BUB_VM_CONFIG is not set")
    vm = Vm(json.loads(Path(config_path).read_text()))

    first = args[0] if args else ""
    if first in ("-h", "--help"):
        sys.stderr.write(USAGE)
        return 0

    if first == "--stop":
        if vm.state.is_dir():
            _lock = open_lock(vm.state / "start.lock", fcntl.LOCK_EX)
            vm.stop_vm()
        return 0

    if first == "--destroy":
        if not vm.state.is_dir():
            die(f"there is no VM for {vm.workdir}")
        if sys.stdin.isatty() and not confirm(f"delete the VM disk and state of {vm.workdir}?"):
            return 1
        _lock = open_lock(vm.state / "start.lock", fcntl.LOCK_EX)
        vm.stop_vm()
        shutil.rmtree(vm.state)
        return 0

    if first == "--log":
        logs = [vm.state / "vm.log", vm.state / "serial.log"]
        if not any(path.is_file() for path in logs):
            die(f"there is no log for {vm.workdir}")
        for path in logs:
            if path.is_file():
                sys.stdout.buffer.write(path.read_bytes())
        return 0

    if first == "--":
        args = args[1:]
    elif first.startswith("-"):
        sys.stderr.write(USAGE)
        return 1

    return run_session(vm, args)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
