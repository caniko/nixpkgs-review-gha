# nixpkgs-review-gha vision

A fork of `Defelo/nixpkgs-review-gha` for running `nixpkgs-review` on GitHub
Actions. The upstream review flow is kept — build on `x86_64-linux`,
`aarch64-linux`, `x86_64-darwin`, and `aarch64-darwin`, post results on the
reviewed pull request, optionally push to Attic or Cachix, use remote builders,
and offer the pull-request shortcut userscript. This fork adds Canix-owned
qualification tooling: Paperclip exact-source native qualification, a
qualification-workflow source CI lane, and larger-runner readiness probing.

## Problem

Reviewing an nixpkgs pull request needs a native build platform and enough
memory. A review workflow that silently emulates, oversubscribes the host, or
reports unattempted work as a pass is worse than no review at all, and its
receipts cannot be reused as evidence.

## Principles

- **Native, not emulated.** The Paperclip qualification job checks the actual
  architecture and KVM and supplies no emulation, denied-call wrapper,
  unsupported-system bypass, or outer evaluation flock.
- **Evidence is bound, not summarized.** Results bind source members, workflow
  source, derivations, the native store closure, build logs, signed cache
  verification, and cache NAR hashes; artifacts require an actual provider
  lifetime of at least 2,592,000 seconds (30 days); uploads request 31 days.
- **Honest blocked states.** The ARM projection stays held at its literal
  target, ordinary wait stays zero, and the personal-account fork states that it
  lacks larger-runner readiness instead of scheduling anyway.
- **Readiness before scheduling.** Provider allocation and repository access are
  validated first, on explicitly named organization-owned hosted runners.
- **One attempt with real deadlines.** The package and full VM run once on
  attempt one under the existing assertions and deadlines.
- **Fork discipline.** Upstream setup documentation and workflows remain usable;
  local additions live in separate workflows and scripts.

## Scope

The review, self-update, check, build, and flake-update workflows (`review.yml`
carries the added qualification mode); `paperclip-qualification.yml` and
`qualification-source-ci.yml`;
`qualification-evidence.py`, `paperclip-evidence.py`, `runner-readiness.py`, and
the `test-paperclip-evidence.py` behaviour test; and the repository's treefmt
configuration.

## Non-goals

- It is not a general CI service and does not invoke review approval, merge,
  retirement, or consumer advancement from the qualification mode.
- The current candidate cannot qualify itself; a separately reviewed successor
  on the declared parent is required.
- No x86 receipt substitutes for an ARM result, and no capacity override or
  emulation route is offered.

## Direction

The head commits prepare exact-source Paperclip native qualification, repair
action metadata and retained-source diagnostics, and describe composite inputs.
The ARM projection remains held pending its own source, custody, tool, light,
and executor gates.

## Place in the fleet

canix documents the workflow in `docs/src/operations/agent-nix-workflow.md`,
`docs/src/operations/paperclip.md`, `docs/src/guides/upstream-pr-verification.md`,
and the Attic cache operations page; Atlas's update automation tracks the
repository.
