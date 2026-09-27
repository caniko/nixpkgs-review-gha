# Hash-preparation fixture. The deliberate mismatch reports the cache NAR hash;
# this is not a successful application build and must not be published.
let
  package = import ./omniroute-pr.nix;
  pkgs =
    (builtins.getFlake "github:NixOS/nixpkgs/9765ec03e84a23d2ee24c9bb7399d0920a41e5fd")
    .legacyPackages.aarch64-linux;
in
pkgs.fetchNpmDeps {
  name = "omniroute-3.8.51-npm-deps";
  src = package.src;
  fetcherVersion = 2;
  hash = pkgs.lib.fakeHash;
}
