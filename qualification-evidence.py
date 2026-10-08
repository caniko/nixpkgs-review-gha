#!/usr/bin/env python3
"""Exact-head hosted qualification receipts and provider-retention readback."""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import urllib.request
import xml.etree.ElementTree as ET

MIN_RETENTION_SECONDS = 2592000


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def api(path):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/{path}",
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def event_identity():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    require(
        os.environ["GITHUB_EVENT_NAME"] == "pull_request",
        "Qualification requires a PR event",
    )
    pr = event["pull_request"]
    return {
        "repository": os.environ["GITHUB_REPOSITORY"],
        "pr": pr["number"],
        "head": pr["head"]["sha"],
        "base": pr["base"]["sha"],
        "workflow_ref": os.environ["GITHUB_WORKFLOW_REF"],
        "workflow_sha": os.environ["GITHUB_WORKFLOW_SHA"],
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
    }


def identity():
    source = event_identity()
    require(
        api(f"pulls/{source['pr']}")["head"]["sha"] == source["head"],
        "PR head advanced; evidence is historical",
    )
    require(
        source["run_attempt"] == 1,
        "Retries cannot qualify; publish a source successor",
    )
    return source


def initialize(directory):
    require(
        os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted",
        "Only GitHub-hosted runners qualify",
    )
    require(
        int(os.environ["GITHUB_RETENTION_DAYS"]) >= 31,
        "Repository/organization retention must allow 31 days",
    )
    source = identity()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    require(revision == source["head"], "Checkout is not the exact PR head")
    source.update(
        {
            "tested_source": revision,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "receipt_tool_sha256": sha256(__file__),
            "locks": {
                str(path): sha256(path)
                for path in (
                    Path("pnpm-lock.yaml"),
                    Path("flake.lock"),
                    Path("Cargo.lock"),
                    Path("uv.lock"),
                    Path("packages/paperclip-runner/runner/Cargo.lock"),
                )
                if path.is_file()
            },
        }
    )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "source.json").write_text(json.dumps(source, indent=2) + "\n")


def seal(directory, outcome, strict_reports):
    directory.mkdir(parents=True, exist_ok=True)
    source = {
        "repository": os.environ.get("GITHUB_REPOSITORY"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF"),
        "workflow_sha": os.environ.get("GITHUB_WORKFLOW_SHA"),
    }
    reports = []
    rejected = []
    try:
        document = json.loads((directory / "source.json").read_text())
        require(isinstance(document, dict), "Source receipt is not an object")
        source.update(document)
    except Exception as error:
        rejected.append(str(error))
        try:
            source.update(event_identity())
        except Exception as identity_error:
            rejected.append(str(identity_error))
    for path in sorted(directory.rglob("*.xml")):
        root = ET.parse(path).getroot()
        cases = root.findall(".//testcase")
        require(cases, f"Empty JUnit report: {path}")
        identities = [(case.get("classname"), case.get("name")) for case in cases]
        failures = root.findall(".//failure") + root.findall(".//error")
        skipped = root.findall(".//skipped")
        retried = [
            node
            for node in root.iter()
            if "retry" in node.tag.lower() or "flaky" in node.tag.lower()
        ]
        if outcome == "success":
            if len(identities) != len(set(identities)):
                rejected.append(f"Duplicate/retried JUnit cases: {path}")
            if failures or retried:
                rejected.append(f"Failed or retried test: {path}")
            if strict_reports and skipped:
                rejected.append(f"Mandatory test skipped: {path}")
        reports.append(
            {
                "path": str(path.relative_to(directory)),
                "cases": len(cases),
                "failures": len(failures),
                "skipped": len(skipped),
                "retries": len(retried),
            }
        )
    if strict_reports:
        if not reports:
            rejected.append("Mandatory JUnit evidence is missing")
    receipt = {
        "schema": "hosted-qualification.v1",
        **source,
        "outcome": "failure" if rejected else outcome,
        "original_outcome": outcome,
        "qualified": False,
        "reports": reports,
        "rejected": rejected,
        "members": {
            str(path.relative_to(directory)): sha256(path)
            for path in sorted(directory.rglob("*"))
            if path.is_file() and path.name != "receipt.json"
        },
    }
    (directory / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    require(not rejected, "; ".join(rejected))


def expected_red(directory):
    root = ET.parse(directory / "regression.xml").getroot()
    cases = root.findall(".//testcase")
    require(
        cases and not root.findall(".//skipped") and not root.findall(".//error"),
        "Expected RED requires completed assertion failures, without errors or skips",
    )
    failed = [
        case.get("name", "") for case in cases if case.find("failure") is not None
    ]
    oracles = [
        "binds a delayed ID-less parent poll",
        "rejects a delayed parent SSE",
        "holds live and recovery ownership",
        "stops and settles before returning",
    ]
    require(
        all(any(oracle in name for name in failed) for oracle in oracles),
        f"Missing baseline regression failures: {failed}",
    )
    source_path = directory / "source.json"
    source = json.loads(source_path.read_text())
    source.update(
        {
            "baseline": "f6451f242d5cbb803e3c265e90f05b642e573203",
            "regression_fixture_sha256": sha256(
                "packages/adapters/hermes/src/gateway/server/lineage-observation.test.ts"
            ),
            "expected_assertion_failures": failed,
        }
    )
    source_path.write_text(json.dumps(source, indent=2) + "\n")
    seal(directory, "expected-red", True)


def artifact_metadata(artifact, source):
    created = dt.datetime.fromisoformat(artifact["created_at"].replace("Z", "+00:00"))
    expiry = dt.datetime.fromisoformat(artifact["expires_at"].replace("Z", "+00:00"))
    lifetime = (expiry - created).total_seconds()
    require(
        not artifact["expired"] and lifetime >= MIN_RETENTION_SECONDS,
        f"Artifact {artifact['id']} lifetime is only {lifetime} seconds",
    )
    digest = artifact.get("digest", "")
    require(
        re.fullmatch(r"sha256:[0-9a-f]{64}", digest),
        "Provider artifact SHA-256 is missing",
    )
    require(
        artifact["workflow_run"]["head_sha"] == source["head"]
        and artifact["workflow_run"]["id"] == source["run_id"],
        "Artifact workflow source mismatch",
    )
    return {
        "id": artifact["id"],
        "name": artifact["name"],
        "sha256": digest,
        "created_at": artifact["created_at"],
        "expires_at": artifact["expires_at"],
        "retention_seconds": lifetime,
    }


def retained_artifacts(prefix, count):
    source = identity()
    retained = []
    page = 1
    while True:
        response = api(
            f"actions/runs/{source['run_id']}/artifacts?per_page=100&page={page}"
        )
        for artifact in response["artifacts"]:
            if not artifact["name"].startswith(prefix):
                continue
            retained.append(artifact_metadata(artifact, source))
        if len(response["artifacts"]) < 100:
            break
        page += 1
    require(
        len(retained) == count,
        f"Expected {count} required artifacts, found {len(retained)}",
    )
    return {
        "schema": "hosted-retention.v1",
        **source,
        "qualified": False,
        "artifacts": retained,
    }


def artifacts(prefix, count, destination):
    try:
        receipt = retained_artifacts(prefix, count)
    except Exception as error:
        receipt = {
            "schema": "hosted-retention.v1",
            "qualified": False,
            "outcome": "failure",
            "repository": os.environ.get("GITHUB_REPOSITORY"),
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "prefix": prefix,
            "rejected": [str(error)],
        }
        Path(destination).write_text(json.dumps(receipt, indent=2) + "\n")
        raise
    Path(destination).write_text(json.dumps(receipt, indent=2) + "\n")


def artifact(artifact_id, destination, expected_digest=None):
    receipt = {
        "schema": "hosted-final-retention.v1",
        "repository": os.environ.get("GITHUB_REPOSITORY"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "artifact_id": artifact_id,
        "qualified": False,
        "artifacts": [],
    }
    try:
        source = identity()
        receipt.update(source)
        selected = api(f"actions/artifacts/{artifact_id}")
        require(selected["id"] == artifact_id, "Provider artifact ID mismatch")
        require(
            selected["name"] == f"workflow-retention-audit-{source['head']}",
            "Final retention artifact name mismatch",
        )
        receipt["artifacts"].append(artifact_metadata(selected, source))
        if expected_digest is not None:
            require(
                re.fullmatch(r"[0-9a-f]{64}", expected_digest)
                and selected["digest"] == "sha256:" + expected_digest,
                "Final retention artifact differs from the uploaded digest",
            )
        receipt.update(outcome="success", rejected=[])
    except Exception as error:
        receipt.update(outcome="failure", rejected=[str(error)])
        raise
    finally:
        payload = json.dumps(receipt, indent=2) + "\n"
        Path(destination).write_text(payload)
        print(payload, end="")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("initialize")
    init.add_argument("directory", type=Path)
    finish = sub.add_parser("seal")
    finish.add_argument("directory", type=Path)
    finish.add_argument("outcome")
    finish.add_argument("--strict-reports", action="store_true")
    red = sub.add_parser("expected-red")
    red.add_argument("directory", type=Path)
    audit = sub.add_parser("artifacts")
    audit.add_argument("prefix")
    audit.add_argument("count", type=int)
    audit.add_argument("destination")
    final = sub.add_parser("artifact")
    final.add_argument("artifact_id", type=int)
    final.add_argument("destination", type=Path)
    final.add_argument("--sha256", required=True)
    args = parser.parse_args()
    if args.command == "initialize":
        initialize(args.directory)
    elif args.command == "seal":
        seal(args.directory, args.outcome, args.strict_reports)
    elif args.command == "expected-red":
        expected_red(args.directory)
    elif args.command == "artifacts":
        artifacts(args.prefix, args.count, args.destination)
    else:
        artifact(args.artifact_id, args.destination, args.sha256)


if __name__ == "__main__":
    main()
