# Exact browserless package selected by Canix rollout/omniroute-12952.
# Build this with build.yml on aarch64-linux and opt in to cache publication.
let
  packageSet =
    (builtins.getFlake "github:NixOS/nixpkgs/9765ec03e84a23d2ee24c9bb7399d0920a41e5fd")
    .legacyPackages.aarch64-linux;
  source = builtins.fetchTree {
    type = "github";
    owner = "caniko";
    repo = "OmniRoute";
    rev = "6c6a4a8f269e342e233810a0937e8c188db20535";
    narHash = "sha256-Wx4+8aby0RzcdLYqB1VTVtlgurnsWAsr8HSBv0rehyw=";
  };
  package = packageSet.omniroute.override {
    withBrowser = false;
    buildNpmPackage =
      args:
      packageSet.buildNpmPackage (
        finalAttrs:
        let
          original = args finalAttrs;
        in
        builtins.removeAttrs original [
          "npmDepsHash"
          "npmDepsFetcherVersion"
        ]
        // {
          version = (builtins.fromJSON (builtins.readFile "${source}/package.json")).version;
          src = source;
          npmDeps = packageSet.importNpmLock { npmRoot = source; };
          npmConfigHook = packageSet.importNpmLock.npmConfigHook;
          env = original.env // {
            NEXT_DIST_DIR = ".build/next";
          };
          postPatch = original.postPatch + ''
            # build:cli must compile this source, not reuse a standalone tree.
            rm -rf .build/next .next dist
          '';
        }
      );
  };
in
assert package.outPath == "/nix/store/6pcz4j4bchykvwpqj5zqnydni3xkw4k1-omniroute-3.8.51";
package
