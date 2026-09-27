# Exact browserless package selected by the Canix thething overlay.
# Source pin: Canix trunk bf0777d25, nixpkgs-omniroute = github:NixOS/nixpkgs/9765ec03e84a23d2ee24c9bb7399d0920a41e5fd.
# Build this with build.yml on aarch64-linux with push-to-cache.
let
  packageSet =
    (builtins.getFlake "github:NixOS/nixpkgs/9765ec03e84a23d2ee24c9bb7399d0920a41e5fd")
    .legacyPackages.aarch64-linux;
  package =
    (packageSet.omniroute.override {
      withBrowser = false;
    }).overrideAttrs (old: {
      patches = (old.patches or []) ++ [ ./omniroute-effort-policy.patch ./omniroute-byte-admission-headroom.patch ];
    });
in
assert package.drvPath == "/nix/store/phhjxirrgsd77jbhvpp0q0slb52ccngn-omniroute-3.8.50.drv";
assert package.outPath == "/nix/store/p01wly8nh2hqlflbzyq0zkpx1dqsswqa-omniroute-3.8.50";
package
