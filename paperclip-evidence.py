"""Exact-source Paperclip native qualification and provider retention receipts."""

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

SOURCE_PATHS = {
    "pkgs/by-name/pa/paperclip/package.nix",
    "nixos/tests/all-tests.nix",
    "nixos/tests/paperclip.nix",
}
PARENT = "921ffc6b6ef3e56a58751007ad1c7dfa598f2e1b"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def api(path):
    request = urllib.request.Request(
        f"https://api.github.com/{path}",
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_identity(head, parent, review):
    require(
        re.fullmatch(r"[0-9a-f]{40}", head) and head != parent,
        "Select an immutable ARM source successor",
    )
    require(parent == PARENT, "Source parent is outside the three-path P2 grant")
    require(
        re.fullmatch(r"[0-9a-f]{64}", review),
        "Signed-head/current-parent review receipt binding is missing",
    )
    pr = api("repos/NixOS/nixpkgs/pulls/567242")
    require(
        pr["head"]["sha"] == head and not pr["merged"],
        "Nixpkgs PR head advanced or merged",
    )
    commit = api(f"repos/NixOS/nixpkgs/commits/{head}")
    require(
        len(commit["parents"]) == 1 and commit["parents"][0]["sha"] == parent,
        "Source parent changed",
    )
    require(
        commit["commit"]["verification"]["verified"],
        "Source successor is not verified as signed",
    )
    paths = {member["filename"] for member in commit["files"]}
    require(
        paths and paths <= SOURCE_PATHS, "Source successor exceeds the three-path grant"
    )
    return {
        "pr": 567242,
        "source_head": head,
        "source_parent": parent,
        "signed_review_sha256": review,
        "source_paths": sorted(paths),
    }


def initialize(directory):
    require(
        os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch",
        "Heavy qualification requires an explicit hosted dispatch",
    )
    require(
        os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted",
        "Only supported hosted execution qualifies",
    )
    require(int(os.environ["GITHUB_RUN_ATTEMPT"]) == 1, "Retry evidence cannot qualify")
    require(
        int(os.environ["GITHUB_RETENTION_DAYS"]) >= 31,
        "Provider retention must permit 31 days",
    )
    source = source_identity(
        os.environ["SOURCE_HEAD"],
        os.environ["SOURCE_PARENT"],
        os.environ["SOURCE_REVIEW_SHA256"],
    )
    actual = subprocess.check_output(
        ["git", "-C", "nixpkgs", "rev-parse", "HEAD"], text=True
    ).strip()
    require(
        actual == source["source_head"],
        "Nixpkgs checkout is not the selected exact head",
    )
    source.update(
        {
            "workflow_repository": os.environ["GITHUB_REPOSITORY"],
            "workflow_sha": os.environ["GITHUB_SHA"],
            "workflow_ref": os.environ["GITHUB_WORKFLOW_REF"],
            "run_id": os.environ["GITHUB_RUN_ID"],
            "attempt": 1,
            "system": os.environ["SYSTEM"],
            "receipt_tool_sha256": digest(__file__),
            "source_members_sha256": {
                path: digest(Path("nixpkgs") / path) for path in SOURCE_PATHS
            },
        }
    )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "source.json").write_text(json.dumps(source, indent=2) + "\n")


def seal(directory, outcome):
    source = json.loads((directory / "source.json").read_text())
    receipt = {
        "schema": "paperclip-native-hosted.v1",
        **source,
        "qualified": False,
        "outcome": outcome,
        "members_sha256": {
            str(path.relative_to(directory)): digest(path)
            for path in sorted(directory.rglob("*"))
            if path.is_file() and path.name != "receipt.json"
        },
    }
    (directory / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    if outcome == "success":
        source_identity(
            source["source_head"],
            source["source_parent"],
            source["signed_review_sha256"],
        )
        results = json.loads((directory / "result.json").read_text())
        require(
            len(results) == 2 and all(item.get("outputs") for item in results),
            "Package/full-P2-VM outputs are missing",
        )
        require(
            (directory / "cache-readback.json").is_file(),
            "Native cache qualification is missing",
        )
        require(
            (directory / "derivations.json").is_file()
            and (directory / "closure.json").is_file(),
            "Installed/source closure binding is missing",
        )

        def paths(document):
            return (
                document
                if isinstance(document, dict)
                else {item["path"]: item for item in document}
            )

        local = paths(json.loads((directory / "closure.json").read_text()))
        remote = paths(json.loads((directory / "cache-readback.json").read_text()))
        outputs = {path for item in results for path in item["outputs"].values()}
        require(
            outputs <= remote.keys() and outputs <= local.keys(),
            "Native cache readback omitted a required output",
        )
        require(
            all(
                local[path]["narHash"] == remote[path]["narHash"]
                and local[path]["narSize"] == remote[path]["narSize"]
                for path in outputs
            ),
            "Native cache content does not match the built outputs",
        )
        require(
            (directory / "cache-verify.log").is_file(),
            "Signed cache verification is missing",
        )


def audit(destination):
    repository = os.environ["GITHUB_REPOSITORY"]
    require(
        api(f"repos/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}")[
            "head_sha"
        ]
        == os.environ["GITHUB_SHA"],
        "Workflow source identity changed",
    )
    retained = []
    for page in range(1, 20):
        artifacts = api(
            f"repos/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}/artifacts?per_page=100&page={page}"
        )["artifacts"]
        for artifact in artifacts:
            if not artifact["name"].startswith("paperclip-native-proof-"):
                continue
            lifetime = (
                dt.datetime.fromisoformat(artifact["expires_at"].replace("Z", "+00:00"))
                - dt.datetime.fromisoformat(
                    artifact["created_at"].replace("Z", "+00:00")
                )
            ).total_seconds()
            require(
                not artifact["expired"] and lifetime >= 2592000,
                "Required artifact lifetime is below thirty days",
            )
            require(
                re.fullmatch(r"sha256:[0-9a-f]{64}", artifact.get("digest", "")),
                "Provider artifact digest is missing",
            )
            retained.append(
                {
                    "id": artifact["id"],
                    "name": artifact["name"],
                    "sha256": artifact["digest"],
                    "retention_seconds": lifetime,
                    "expires_at": artifact["expires_at"],
                }
            )
        if len(artifacts) < 100:
            break
    require(
        len(retained) == 3,
        "Readiness and both native architectures must retain evidence",
    )
    Path(destination).write_text(
        json.dumps(
            {
                "schema": "paperclip-native-retention.v1",
                "qualified": False,
                "workflow_sha": os.environ["GITHUB_SHA"],
                "artifacts": retained,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    if sys.argv[1] == "initialize":
        initialize(Path(sys.argv[2]))
    elif sys.argv[1] == "seal":
        seal(Path(sys.argv[2]), sys.argv[3])
    elif sys.argv[1] == "audit":
        audit(sys.argv[2])
