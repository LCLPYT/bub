{
  lib,
  stdenv,
  runCommand,
  makeWrapper,
  writeText,
  python3,
  gitMinimal,
  openssh,
  socat,
  virtiofsd,
  vm,
}:

let
  cfg = vm.config.bub;
  mcfg = vm.config.microvm;

  config = writeText "bub-vm-config.json" (
    builtins.toJSON {
      runner = "${mcfg.declaredRunner}";
      inherit (mcfg) hypervisor;
      guestSystem = vm.pkgs.stdenv.hostPlatform.system;
      guestUser = cfg.user;
      guestUid = cfg.uid;
      guestHome = vm.config.users.users.${cfg.user}.home;
      inherit (cfg) systemDirs;
      shares = map (share: {
        inherit (share)
          tag
          source
          target
          readOnly
          when
          ;
      }) (lib.attrValues cfg.shares);
      network = cfg.network.enable;
      keepRunning = cfg.keepRunning;
      devShell = cfg.devShell.enable;
      inherit (cfg) forwardEnv;
    }
  );

  runtimeInputs = [
    gitMinimal
    openssh
    socat
  ]
  ++ lib.optional stdenv.hostPlatform.isLinux virtiofsd;
in
runCommand "bub-vm"
  {
    nativeBuildInputs = [ makeWrapper ];
    meta.mainProgram = "bub-vm";
  }
  ''
    mkdir -p $out/libexec
    cp -r ${./bub_vm} $out/libexec/bub_vm
    chmod -R u+w $out/libexec/bub_vm
    ${python3.interpreter} -m compileall -q $out/libexec/bub_vm
    makeWrapper ${python3.interpreter} $out/bin/bub-vm \
      --add-flags "-I $out/libexec/bub_vm/__main__.py" \
      --prefix PATH : ${lib.makeBinPath runtimeInputs} \
      --set BUB_VM_CONFIG ${config}
  ''
