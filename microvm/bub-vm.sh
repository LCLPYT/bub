log() { printf '[bub-vm] %s\n' "$*" >&2; }
die() {
  log "error: $*"
  exit 1
}

usage() {
  cat >&2 <<EOF
Usage: bub-vm [program [args...]]  run a program (default: bash) in the VM of the current directory
       bub-vm --stop               stop the VM of the current directory
       bub-vm --destroy            stop the VM and delete its disk and state
       bub-vm --log                print the console log of the VM

Environment:
  BUB_DEVSHELL=0       don't load the devShell of the project flake
  BUB_KEEP_RUNNING=1   keep the VM running after the last session ends
EOF
}

NIX=(nix --extra-experimental-features 'nix-command flakes' --option allow-import-from-derivation false)

WORKDIR=$(pwd -P)
STATE_ROOT=${XDG_STATE_HOME:-$HOME/.local/state}/bub-vm
HASH=$(printf '%s' "$WORKDIR" | sha256sum | cut -c1-12)
STATE=$STATE_ROOT/$HASH
META=$STATE/meta
KEY=$STATE/id_ed25519
CID=$((16#${HASH:0:7} + 3))
MAC="02:${HASH:0:2}:${HASH:2:2}:${HASH:4:2}:${HASH:6:2}:${HASH:8:2}"
SHARES=()

check_path() {
  case "$2" in
    *[[:space:]]* | *,* | *=* | *\'* | *\"* | *\\* | *\$* | *\`* | *\** | *\?* | *\[* | *\]*)
      die "$1 contains characters that can't be passed to the hypervisor: $2"
      ;;
  esac
}

validate() {
  [ "$(id -u)" = "$BUB_GUEST_UID" ] ||
    die "host uid $(id -u) differs from the VM uid $BUB_GUEST_UID, set bub.uid in your configuration"

  case "$WORKDIR" in
    / | "$HOME" | "$HOME"/.ssh* | "$HOME"/.gnupg* | "$HOME"/.config | "$HOME"/.local | "$HOME"/.local/share | "$STATE_ROOT"*)
      die "refusing to share '$WORKDIR' with the VM"
      ;;
    /bin* | /boot* | /dev* | /etc* | /lib* | /nix* | /proc* | /root* | /run* | /sbin* | /sys* | /tmp | /usr* | /var | /var/lib* | /var/log*)
      die "'$WORKDIR' would shadow a system directory inside the VM"
      ;;
  esac

  check_path "project path" "$WORKDIR"
  check_path "state path" "$STATE"
  [ "${#STATE}" -lt 80 ] || die "state path is too long for unix sockets: $STATE"

  if [ "$BUB_HYPERVISOR" = qemu ]; then
    [ -w /dev/kvm ] || die "/dev/kvm is not accessible"
    [ -w /dev/vhost-vsock ] || die "/dev/vhost-vsock is not accessible, try 'modprobe vhost_vsock'"
  fi
}

write_gitconfig() {
  local name email
  name=$(git config --global user.name 2>/dev/null || true)
  email=$(git config --global user.email 2>/dev/null || true)
  {
    [ -z "$name$email" ] || echo "[user]"
    [ -z "$name" ] || printf '\tname = %s\n' "$name"
    [ -z "$email" ] || printf '\temail = %s\n' "$email"
  } >"$META/gitconfig"
}

setup_state() {
  mkdir -p "$META"
  chmod 700 "$STATE" "$META"
  printf '%s\n' "$WORKDIR" >"$STATE/workdir"
  printf '%s\n' "$WORKDIR" >"$META/workdir"
  [ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N '' -C bub-vm -f "$KEY"
  install -m 600 "$KEY.pub" "$META/authorized_keys"
  write_gitconfig
}

collect_shares() {
  local tag src
  SHARES=("work"$'\t'"$WORKDIR" "meta"$'\t'"$META")
  while IFS=$'\t' read -r tag src; do
    [ -n "$tag" ] || continue
    case "$src" in
      "~") src=$HOME ;;
      "~"/*) src=$HOME/${src#"~/"} ;;
    esac
    mkdir -p "$src"
    src=$(cd "$src" && pwd -P)
    check_path "share $tag" "$src"
    SHARES+=("$tag"$'\t'"$src")
  done <<<"$BUB_HOME_SHARES"
}

flake_hash() {
  (
    cd "$WORKDIR"
    if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      git ls-files -z --cached --others --exclude-standard -- '*.nix' 'flake.lock' |
        sort -z | { xargs -0 -r sha256sum 2>/dev/null || true; }
    else
      sha256sum flake.nix flake.lock 2>/dev/null || true
    fi
  ) | sha256sum | cut -d' ' -f1
}

approve_flake() {
  local hash approved answer
  hash=$(flake_hash)
  approved=$(cat "$STATE/devshell.approved" 2>/dev/null || true)
  [ "$hash" != "$approved" ] || return 0

  if [ ! -t 0 ] || [ ! -t 2 ]; then
    log "the Nix files of this project are not approved, skipping the devShell (run bub-vm interactively to approve)"
    return 1
  fi
  log "the Nix files of this project changed since they were last approved (or were never approved)."
  log "loading the devShell evaluates and builds them on the host, outside of the VM."
  read -r -p "[bub-vm] load the devShell of $WORKDIR? [y/N] " answer || answer=""
  case "$answer" in
    y | Y | yes) printf '%s\n' "$hash" >"$STATE/devshell.approved" ;;
    *) return 1 ;;
  esac
}

prepare_devshell() {
  local attr installable
  if [ "${BUB_DEVSHELL:-1}" != 1 ] || [ ! -f "$WORKDIR/flake.nix" ]; then
    rm -f "$META/devenv.sh"
    return 0
  fi
  if ! command -v nix >/dev/null; then
    log "nix not found on the host, skipping the devShell"
    rm -f "$META/devenv.sh"
    return 0
  fi
  if ! approve_flake; then
    rm -f "$META/devenv.sh"
    return 0
  fi

  attr=$("${NIX[@]}" eval --raw "$WORKDIR#devShells.$BUB_GUEST_SYSTEM" \
    --apply 'shells: if shells ? bub then "bub" else if shells ? default then "default" else ""' \
    2>/dev/null || true)
  if [ -z "$attr" ]; then
    log "no devShells.$BUB_GUEST_SYSTEM.{bub,default} found (or the flake failed to evaluate), skipping the devShell"
    rm -f "$META/devenv.sh"
    return 0
  fi

  installable="$WORKDIR#devShells.$BUB_GUEST_SYSTEM.$attr"
  log "loading $installable"
  if "${NIX[@]}" print-dev-env --profile "$STATE/devshell" "$installable" >"$META/devenv.sh.tmp"; then
    mv "$META/devenv.sh.tmp" "$META/devenv.sh"
  else
    log "building the devShell failed, continuing without it"
    rm -f "$META/devenv.sh.tmp" "$META/devenv.sh"
  fi
}

# Starts a command in a new session, so terminal signals (Ctrl-C, hangup) don't reach it,
# and prints its pid. The command runs in the state dir, which microvm.nix expects for relative paths.
detach() {
  local logfile=$1
  shift
  (
    cd "$STATE"
    perl -MPOSIX -e 'POSIX::setsid(); exec { $ARGV[0] } @ARGV or die "exec $ARGV[0]: $!\n"' "$@" \
      </dev/null >>"$logfile" 2>&1 &
    echo $!
  ) 8>&- 9>&-
}

vm_running() {
  local pid
  pid=$(cat "$STATE/vm.pid" 2>/dev/null) || return 1
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

start_virtiofsd() {
  local tag=$1 src=$2
  shift 2
  detach virtiofsd.log virtiofsd "--socket-path=virtiofs-$tag.sock" "--shared-dir=$src" \
    --sandbox=none --cache=auto "$@" >>"$STATE/virtiofsd.pids"
}

start_vm() {
  local entry tag src i missing
  local -a args=() sockets=()

  log "starting VM for $WORKDIR"
  rm -f "$STATE"/*.sock "$STATE/serial.log" "$STATE/virtiofsd.pids"
  : >"$STATE/vm.log"

  case "$BUB_HYPERVISOR" in
    qemu)
      start_virtiofsd ro-store /nix/store --readonly
      sockets+=(virtiofs-ro-store.sock)
      args+=(
        -netdev "user,id=bubnet"
        -device "virtio-net-pci,netdev=bubnet,mac=$MAC"
        -device "vhost-vsock-pci,guest-cid=$CID"
      )
      for entry in "${SHARES[@]}"; do
        IFS=$'\t' read -r tag src <<<"$entry"
        if [ "$tag" = meta ]; then
          start_virtiofsd "$tag" "$src" --readonly
        else
          start_virtiofsd "$tag" "$src"
        fi
        sockets+=("virtiofs-$tag.sock")
        args+=(
          -chardev "socket,id=bub-$tag,path=virtiofs-$tag.sock"
          -device "vhost-user-fs-pci,chardev=bub-$tag,tag=$tag"
        )
      done

      for ((i = 0; i < 100; i++)); do
        missing=0
        for entry in "${sockets[@]}"; do
          [ -S "$STATE/$entry" ] || missing=1
        done
        [ "$missing" = 1 ] || break
        sleep 0.05
      done
      [ "$missing" = 0 ] || die "virtiofsd did not start, see $STATE/virtiofsd.log"
      ;;
    vfkit)
      args+=(
        --device "virtio-net,nat,mac=$MAC"
        --device "virtio-vsock,port=22,socketURL=$STATE/ssh.sock,connect"
      )
      for entry in "${SHARES[@]}"; do
        IFS=$'\t' read -r tag src <<<"$entry"
        args+=(--device "virtio-fs,sharedDir=$src,mountTag=$tag")
      done
      ;;
    *)
      die "unsupported hypervisor $BUB_HYPERVISOR"
      ;;
  esac

  export BUB_HYPERVISOR_ARGS="${args[*]}"
  detach vm.log "$BUB_RUNNER/bin/microvm-run" >"$STATE/vm.pid"
  printf '%s\n' "$BUB_RUNNER" >"$STATE/vm.runner"
}

ssh_proxy() {
  case "$BUB_HYPERVISOR" in
    qemu) echo "exec socat - VSOCK-CONNECT:$CID:22" ;;
    vfkit) echo "exec socat - UNIX-CONNECT:$STATE/ssh.sock" ;;
  esac
}

vm_ssh() {
  env -u SSH_AUTH_SOCK SHELL="$BASH" ssh \
    -F /dev/null \
    -i "$KEY" \
    -l "$BUB_GUEST_USER" \
    -o IdentitiesOnly=yes \
    -o IdentityAgent=none \
    -o ForwardAgent=no \
    -o ForwardX11=no \
    -o ClearAllForwardings=yes \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR \
    -o "ProxyCommand=$(ssh_proxy)" \
    "$@"
}

wait_ssh() {
  local i
  for ((i = 0; i < 300; i++)); do
    if vm_ssh -T -o ConnectTimeout=2 bub-vm true </dev/null >/dev/null 2>&1; then
      return 0
    fi
    if ! vm_running; then
      log "the VM exited, last console output:"
      tail -n 40 "$STATE/vm.log" "$STATE/serial.log" >&2 2>/dev/null || true
      exit 1
    fi
    sleep 0.3
  done
  die "the VM did not become reachable, see 'bub-vm --log'"
}

stop_vm() {
  local pid i
  vm_running || return 0
  pid=$(cat "$STATE/vm.pid")

  log "stopping VM"
  vm_ssh -T -o ConnectTimeout=5 bub-vm /run/wrappers/bin/sudo /run/current-system/sw/bin/systemctl poweroff \
    </dev/null >/dev/null 2>&1 || true
  for ((i = 0; i < 100; i++)); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.3
  done
  if kill -0 "$pid" 2>/dev/null; then
    log "the VM did not shut down, terminating it"
    kill "$pid" 2>/dev/null || true
    sleep 2
    kill -9 "$pid" 2>/dev/null || true
  fi

  if [ -f "$STATE/virtiofsd.pids" ]; then
    xargs kill <"$STATE/virtiofsd.pids" 2>/dev/null || true
  fi
  rm -f "$STATE/vm.pid" "$STATE/virtiofsd.pids" "$STATE"/*.sock
}

lock_start() {
  exec 8>"$STATE/start.lock"
  flock 8
}

# shellcheck disable=SC2329
finish() {
  trap - EXIT
  flock -u 9 2>/dev/null || true
  [ "${BUB_KEEP_RUNNING:-0}" != 1 ] || return 0
  flock 8
  if flock -xn 9; then
    stop_vm
  fi
  flock -u 8 || true
}

case "${1:-}" in
  -h | --help)
    usage
    exit 0
    ;;
  --stop)
    [ -d "$STATE" ] || exit 0
    lock_start
    stop_vm
    exit 0
    ;;
  --destroy)
    [ -d "$STATE" ] || die "there is no VM for $WORKDIR"
    if [ -t 0 ]; then
      read -r -p "[bub-vm] delete the VM disk and state of $WORKDIR? [y/N] " answer || answer=""
      case "$answer" in
        y | Y | yes) ;;
        *) exit 1 ;;
      esac
    fi
    lock_start
    stop_vm
    rm -rf "$STATE"
    exit 0
    ;;
  --log)
    cat "$STATE/vm.log" "$STATE/serial.log" 2>/dev/null || die "there is no log for $WORKDIR"
    exit 0
    ;;
  --)
    shift
    ;;
  -*)
    usage
    exit 1
    ;;
esac

validate
setup_state
collect_shares
prepare_devshell

lock_start
if vm_running; then
  [ "$(cat "$STATE/vm.runner" 2>/dev/null)" = "$BUB_RUNNER" ] ||
    log "the running VM uses an older build of bub-vm, run 'bub-vm --stop' to restart it"
else
  start_vm
  log "waiting for the VM to boot"
fi
exec 9>"$STATE/sessions.lock"
flock -s 9
flock -u 8

trap finish EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

wait_ssh

remote=/run/current-system/sw/bin/bub-session
if [ $# -gt 0 ]; then
  remote+=" $(printf '%q ' "$@")"
fi
tty_flag=-T
if [ -t 0 ] && [ -t 1 ]; then
  tty_flag=-t
fi

set +e
vm_ssh "$tty_flag" -o "SetEnv=COLORTERM=${COLORTERM:-}" bub-vm "$remote"
rc=$?
set -e
exit "$rc"
