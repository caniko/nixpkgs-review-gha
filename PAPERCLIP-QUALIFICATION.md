# Paperclip native qualification

The existing `review.yml` dispatch route has an explicit
`paperclip-qualification` mode. It invokes the exact-source package and unchanged
full `nixosTests.paperclip` VM on real x86-64 and ARM hosted runners. This mode
does not invoke review approval, merge, retirement, or consumer advancement.

Execution requires a separately reviewed signed successor on the P2 parent
`921ffc6b6ef3e56a58751007ad1c7dfa598f2e1b`, a SHA-256 binding to its accepted
signed-head/current-parent review, and the current head of Nixpkgs PR #567242.
The source change may touch only the three authorized package/test paths.
The current P2 parent is not an ARM successor and cannot qualify itself.

The independent accepted source review must be committed in the reviewed
controller checkout at `source-reviews/paperclip/<sha256>.json`. The dispatch
digest identifies the exact file bytes, not just a hexadecimal-shaped claim.
The receipt uses schema `paperclip-source-review.v1`, `decision: "accepted"`,
`repository: "NixOS/nixpkgs"`, `pr: 567242`, the exact `source_head` and
`source_parent`, and the sorted three-path grant in `source_paths`. Receipt
publication requires the existing independent review authority. No acceptance
receipt is supplied by this workflow preparation; missing, changed, unaccepted,
or foreign-source receipts block scheduling. Native evidence retains the same
receipt bytes and revalidates the source before sealing success.

Set `QUALIFICATION_X86_LARGER_RUNNER` and `QUALIFICATION_ARM_LARGER_RUNNER` to
supported organization-owned hosted runner names with at least 64 GiB.
`HOSTED_RUNNER_READ_TOKEN` supplies supported organization runner-read access.
Readiness validates provider allocation and repository access before scheduling.
The native job checks the actual architecture and KVM; no emulation, denied-call
wrapper, outer evaluation flock, capacity override, or unsupported-system bypass
is supplied. The personal-account fork currently lacks larger-runner readiness.

The package and full P2 VM run once on attempt one, with substitution disabled,
the existing assertions, and deadlines. Results bind source members, workflow
source, derivations, native
store closure, build logs, signed cache verification, and cache NAR hashes.
Attic and Cachix retain their supported owner-controlled cache transports and
configure Nix read credentials through `attic use` or token-backed `cachix use`.
Every required artifact and its audit require an actual provider lifetime of at
least 2,592,000 seconds; uploads request 31 days.

The native VM covers packaged runner architecture, sharp, embedded PostgreSQL,
and the full P2 lifecycle. Its assertions and installed ELF/source bindings need
independent review before acceptance. No x86 receipt can substitute for ARM.

The local ARM projection remains held at its existing literal target. Its source,
custody, tool, light, and executor gates remain independent. Ordinary wait remains
zero, ARM budget remains one, and original reconciliation budget remains zero.
This workflow preparation allocates no reconciliation successor attempt and does
not dispatch the held ARM command. Publication still requires its separate
signed-head/current-parent review and authorized non-force fast-forward.
