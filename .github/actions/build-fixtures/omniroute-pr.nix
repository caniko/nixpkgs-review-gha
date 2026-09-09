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
          sourcePackage = builtins.fromJSON (builtins.readFile "${source}/package.json");
        in
        original
        // {
          version = sourcePackage.version;
          src = source;
          npmDepsHash = "sha256-FMXIpnQKXivZ42SH45QFn/NPg5uwh+MJD5BhnOBtjWo=";
          npmDepsFetcherVersion = 2;
          env = original.env // {
            NEXT_DIST_DIR = ".build/next";
            OMNIROUTE_USE_TURBOPACK = "0";
          };
          postPatch = original.postPatch + ''
            # build:cli must compile this source, not reuse a standalone tree.
            rm -rf .build/next .next dist
            substituteInPlace src/lib/providers/validation/specialtyInline.ts \
              --replace-fail '    const res = await directHttpsRequest(
      chatUrl,
      {
        method: "POST",
        headers: buildBearerHeaders(apiKey, providerSpecificData),
        body: JSON.stringify({
          model: modelId,
          messages: [{ role: "user", content: "test" }],
          max_tokens: 1,
        }),
      },
      20000
    );' '    const res = await validationWrite(
      chatUrl,
      {
        method: "POST",
        headers: buildBearerHeaders(apiKey, providerSpecificData),
        body: JSON.stringify({
          model: modelId,
          messages: [{ role: "user", content: "test" }],
          max_tokens: 1,
        }),
      },
      false
    );'
          '';
        }
      );
  };
in
package
