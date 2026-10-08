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
REVIEW_DIRECTORY = Path(__file__).resolve().parent / "source-reviews" / "paperclip"


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


def controller_identity():
    require(
        os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch"
        and os.environ["GITHUB_REF"] == "refs/heads/main",
        "Qualification requires the reviewed default-branch controller",
    )
    repository = os.environ["GITHUB_REPOSITORY"]
    require(
        api(f"repos/{repository}/git/ref/heads/main")["object"]["sha"]
        == os.environ["GITHUB_SHA"],
        "Default-branch controller advanced; qualify the current reviewed source",
    )


def dispatch_identity(head, review):
    controller_identity()
    require(int(os.environ["GITHUB_RUN_ATTEMPT"]) == 1, "Retry evidence cannot qualify")
    repository = os.environ["GITHUB_REPOSITORY"]
    current = api(f"repos/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    title = f"paperclip-native:{head}:{review}"
    require(
        current["display_title"] == title, "Dispatch source/review identity mismatch"
    )
    for page in range(1, 101):
        runs = api(
            f"repos/{repository}/actions/workflows/{current['workflow_id']}/runs?per_page=100&page={page}"
        )["workflow_runs"]
        for run in runs:
            require(
                run["display_title"] != title
                or run["run_number"] >= current["run_number"],
                f"Source/review already has a previous dispatch: {run['id']}",
            )
        if len(runs) < 100:
            return
    raise RuntimeError("Dispatch history exceeds the bounded admission scan")


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
    receipt_path = REVIEW_DIRECTORY / (review + ".json")
    require(receipt_path.is_file(), "Accepted source-review receipt is missing")
    require(digest(receipt_path) == review, "Source-review receipt digest mismatch")
    receipt = json.loads(receipt_path.read_text())
    require(
        receipt.get("schema") == "paperclip-source-review.v1"
        and receipt.get("decision") == "accepted"
        and receipt.get("repository") == "NixOS/nixpkgs"
        and receipt.get("pr") == 567242
        and receipt.get("source_head") == head
        and receipt.get("source_parent") == parent
        and receipt.get("source_paths") == sorted(SOURCE_PATHS),
        "Source-review receipt does not accept this exact head, parent, and grant",
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
    paths.update(
        member["previous_filename"]
        for member in commit["files"]
        if "previous_filename" in member
    )
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
    controller_identity()
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
    (directory / "source-review.json").write_bytes(
        (REVIEW_DIRECTORY / (source["signed_review_sha256"] + ".json")).read_bytes()
    )
    (directory / "source.json").write_text(json.dumps(source, indent=2) + "\n")


def validate_success(directory, source):
    source_identity(
        source["source_head"],
        source["source_parent"],
        source["signed_review_sha256"],
    )
    require(
        digest(directory / "source-review.json") == source["signed_review_sha256"],
        "Retained source-review receipt digest mismatch",
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


def seal(directory, outcome):
    source = {}
    rejected = []
    try:
        document = json.loads((directory / "source.json").read_text())
        require(isinstance(document, dict), "Native source receipt is not an object")
        source = document
        if outcome == "success":
            validate_success(directory, source)
    except Exception as error:
        rejected.append(str(error))
    receipt = {
        "schema": "paperclip-native-hosted.v1",
        **source,
        "qualified": False,
        "outcome": "failure" if rejected else outcome,
        "original_outcome": outcome,
        "rejected": rejected,
        "members_sha256": {
            str(path.relative_to(directory)): digest(path)
            for path in sorted(directory.rglob("*"))
            if path.is_file() and path.name != "receipt.json"
        },
    }
    (directory / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    require(not rejected, "; ".join(rejected))


def validate_retention(retained):
    repository = os.environ["GITHUB_REPOSITORY"]
    require(
        api(f"repos/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}")[
            "head_sha"
        ]
        == os.environ["GITHUB_SHA"],
        "Workflow source identity changed",
    )
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


def audit(destination):
    receipt = {
        "schema": "paperclip-native-retention.v1",
        "qualified": False,
        "workflow_sha": os.environ.get("GITHUB_SHA"),
        "repository": os.environ.get("GITHUB_REPOSITORY"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "source_head": os.environ.get("SOURCE_HEAD", ""),
        "source_parent": os.environ.get("SOURCE_PARENT", ""),
        "signed_review_sha256": os.environ.get("SOURCE_REVIEW_SHA256", ""),
        "artifacts": [],
    }
    try:
        validate_retention(receipt["artifacts"])
        controller_identity()
        source_identity(
            receipt["source_head"],
            receipt["source_parent"],
            receipt["signed_review_sha256"],
        )
    except Exception as error:
        receipt.update(outcome="failure", rejected=[str(error)])
        Path(destination).write_text(json.dumps(receipt, indent=2) + "\n")
        raise
    receipt.update(outcome="success", rejected=[])
    Path(destination).write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    if sys.argv[1] == "initialize":
        initialize(Path(sys.argv[2]))
    elif sys.argv[1] == "seal":
        seal(Path(sys.argv[2]), sys.argv[3])
    elif sys.argv[1] == "audit":
        audit(sys.argv[2])
