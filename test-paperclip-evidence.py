import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "evidence", Path(__file__).with_name("paperclip-evidence.py")
)
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)

spec = importlib.util.spec_from_file_location(
    "readiness", Path(__file__).with_name("runner-readiness.py")
)
readiness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readiness)

spec = importlib.util.spec_from_file_location(
    "qualification", Path(__file__).with_name("qualification-evidence.py")
)
qualification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualification)


class SourceBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.receipt = {
            "schema": "paperclip-source-review.v1",
            "decision": "accepted",
            "repository": "NixOS/nixpkgs",
            "pr": 567242,
            "source_head": "a" * 40,
            "source_parent": evidence.PARENT,
            "source_paths": sorted(evidence.SOURCE_PATHS),
        }
        self.review = self.write_review(self.receipt)
        self.receipt_patch = patch.object(
            evidence, "REVIEW_DIRECTORY", self.directory, create=True
        )
        self.receipt_patch.start()
        self.addCleanup(self.receipt_patch.stop)

    def write_review(self, receipt):
        payload = (json.dumps(receipt, sort_keys=True) + "\n").encode()
        review = hashlib.sha256(payload).hexdigest()
        (self.directory / (review + ".json")).write_bytes(payload)
        return review

    def records(self):
        return [
            {"head": {"sha": "a" * 40}, "merged": False},
            {
                "parents": [{"sha": evidence.PARENT}],
                "commit": {"verification": {"verified": True}},
                "files": [{"filename": path} for path in sorted(evidence.SOURCE_PATHS)],
            },
        ]

    def test_exact_signed_head_parent_and_paths_are_bound(self):
        with patch.object(evidence, "api", side_effect=self.records()):
            source = evidence.source_identity("a" * 40, evidence.PARENT, self.review)
        self.assertEqual(source["source_head"], "a" * 40)
        self.assertEqual(source["source_parent"], evidence.PARENT)
        self.assertEqual(set(source["source_paths"]), evidence.SOURCE_PATHS)

    def test_stale_unsigned_foreign_parent_and_out_of_scope_source_fail_closed(self):
        for kind in ["stale", "unsigned", "foreign-parent", "foreign-path", "rename"]:
            records = self.records()
            if kind == "stale":
                records[0]["head"]["sha"] = "c" * 40
            if kind == "unsigned":
                records[1]["commit"]["verification"]["verified"] = False
            if kind == "foreign-parent":
                records[1]["parents"][0]["sha"] = "c" * 40
            if kind == "foreign-path":
                records[1]["files"].append({"filename": "flake.lock"})
            if kind == "rename":
                records[1]["files"][0]["previous_filename"] = "outside-the-grant.nix"
            with (
                self.subTest(kind=kind),
                patch.object(evidence, "api", side_effect=records),
            ):
                with self.assertRaises(RuntimeError):
                    evidence.source_identity("a" * 40, evidence.PARENT, self.review)

    def test_fabricated_digest_and_changed_receipt_bytes_fail_closed(self):
        for kind in ["missing", "changed-bytes"]:
            review = "b" * 64
            if kind == "changed-bytes":
                review = self.review
                (self.directory / (review + ".json")).write_text("{}\n")
            with (
                self.subTest(kind=kind),
                patch.object(evidence, "api", side_effect=self.records()),
            ):
                with self.assertRaises(RuntimeError):
                    evidence.source_identity("a" * 40, evidence.PARENT, review)

    def test_unaccepted_or_foreign_review_receipts_fail_closed(self):
        for field, value in [
            ("schema", "unrelated.v1"),
            ("decision", "pending"),
            ("repository", "someone/nixpkgs"),
            ("pr", 1),
            ("source_head", "c" * 40),
            ("source_parent", "c" * 40),
            ("source_paths", ["flake.lock"]),
        ]:
            review = self.write_review({**self.receipt, field: value})
            with (
                self.subTest(field=field),
                patch.object(evidence, "api", side_effect=self.records()),
            ):
                with self.assertRaises(RuntimeError):
                    evidence.source_identity("a" * 40, evidence.PARENT, review)

    def test_no_review_binding_or_unselected_source_cannot_schedule(self):
        for head, parent, review in [
            ("main", evidence.PARENT, "b" * 64),
            (evidence.PARENT, evidence.PARENT, "b" * 64),
            ("a" * 40, "c" * 40, "b" * 64),
            ("a" * 40, evidence.PARENT, ""),
        ]:
            with self.subTest(head=head, parent=parent, review=review):
                with self.assertRaises(RuntimeError):
                    evidence.source_identity(head, parent, review)

    def test_seal_rejection_retains_failure_instead_of_success(self):
        source = {
            "source_head": "a" * 40,
            "source_parent": evidence.PARENT,
            "signed_review_sha256": self.review,
        }
        (self.directory / "source.json").write_text(json.dumps(source))
        for reason in ["PR head advanced", "Native cache content mismatch"]:
            with (
                self.subTest(reason=reason),
                patch.object(
                    evidence, "source_identity", side_effect=RuntimeError(reason)
                ),
            ):
                with self.assertRaises(RuntimeError):
                    evidence.seal(self.directory, "success")
            receipt = json.loads((self.directory / "receipt.json").read_text())
            self.assertEqual(receipt["outcome"], "failure")
            self.assertEqual(receipt["original_outcome"], "success")
            self.assertEqual(receipt["rejected"], [reason])
            self.assertFalse(receipt["qualified"])

    def test_success_requires_matching_outputs_and_cache_content(self):
        source = {
            "source_head": "a" * 40,
            "source_parent": evidence.PARENT,
            "signed_review_sha256": self.review,
        }
        outputs = ["/nix/store/package", "/nix/store/full-p2-vm"]
        closure = {
            path: {"narHash": "sha256:content", "narSize": 42} for path in outputs
        }
        (self.directory / "source.json").write_text(json.dumps(source))
        (self.directory / "source-review.json").write_bytes(
            (self.directory / (self.review + ".json")).read_bytes()
        )
        (self.directory / "result.json").write_text(
            json.dumps([{"outputs": {"out": path}} for path in outputs])
        )
        (self.directory / "derivations.json").write_text("{}")
        (self.directory / "closure.json").write_text(json.dumps(closure))
        (self.directory / "cache-readback.json").write_text(json.dumps(closure))
        (self.directory / "cache-verify.log").write_text("Signed cache verified")
        with patch.object(evidence, "api", side_effect=self.records()):
            evidence.seal(self.directory, "success")
        receipt = json.loads((self.directory / "receipt.json").read_text())
        self.assertEqual(receipt["outcome"], "success")
        self.assertEqual(receipt["rejected"], [])
        self.assertFalse(receipt["qualified"])
        closure[outputs[1]]["narHash"] = "sha256:wrong-content"
        (self.directory / "cache-readback.json").write_text(json.dumps(closure))
        with patch.object(evidence, "api", side_effect=self.records()):
            with self.assertRaisesRegex(RuntimeError, "Native cache content"):
                evidence.seal(self.directory, "success")
        receipt = json.loads((self.directory / "receipt.json").read_text())
        self.assertEqual(receipt["outcome"], "failure")
        self.assertIn("Native cache content", receipt["rejected"][0])

    def test_final_retention_rechecks_selected_head_and_retains_rejection(self):
        env = {
            "SOURCE_HEAD": "a" * 40,
            "SOURCE_PARENT": evidence.PARENT,
            "SOURCE_REVIEW_SHA256": self.review,
        }
        for kind in ["current", "advanced", "merged"]:
            records = self.records()
            if kind == "advanced":
                records[0]["head"]["sha"] = "c" * 40
            if kind == "merged":
                records[0]["merged"] = True
            with (
                self.subTest(kind=kind),
                patch.dict(os.environ, env),
                patch.object(evidence, "controller_identity"),
                patch.object(evidence, "api", side_effect=records),
                patch.object(evidence, "validate_retention"),
            ):
                destination = self.directory / "retention.json"
                if kind == "current":
                    evidence.audit(destination)
                else:
                    with self.assertRaisesRegex(
                        RuntimeError, "head advanced or merged"
                    ):
                        evidence.audit(destination)
                receipt = json.loads(destination.read_text())
                self.assertEqual(receipt["source_head"], env["SOURCE_HEAD"])
                self.assertEqual(
                    receipt["outcome"], "success" if kind == "current" else "failure"
                )


class DispatchAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "GITHUB_REPOSITORY": "org/controller",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_SHA": "a" * 40,
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_RUN_ID": "12",
            "GITHUB_RUN_ATTEMPT": "1",
        }

    def test_controller_rejects_branch_dispatch_and_stale_default_sha(self):
        for ref, sha in [
            ("refs/heads/topic", "a" * 40),
            ("refs/heads/main", "b" * 40),
        ]:
            with (
                self.subTest(ref=ref, sha=sha),
                patch.dict(os.environ, {**self.env, "GITHUB_REF": ref}),
                patch.object(evidence, "api", return_value={"object": {"sha": sha}}),
            ):
                with self.assertRaises(RuntimeError):
                    evidence.controller_identity()
        with (
            patch.dict(os.environ, self.env),
            patch.object(evidence, "api", return_value={"object": {"sha": "a" * 40}}),
        ):
            evidence.controller_identity()

    def test_repeat_dispatch_rejects_prior_attempts_across_pages_and_outcomes(self):
        title = "paperclip-native:" + "b" * 40 + ":" + "c" * 64
        current = {
            "id": 12,
            "workflow_id": 7,
            "run_number": 10,
            "display_title": title,
        }
        for outcome in ["success", "failure", "cancelled", None]:
            prior = {**current, "id": 11, "run_number": 9, "conclusion": outcome}
            unrelated = [{**prior, "display_title": "unrelated"} for _ in range(100)]
            with (
                self.subTest(outcome=outcome),
                patch.dict(os.environ, self.env),
                patch.object(evidence, "controller_identity"),
                patch.object(
                    evidence,
                    "api",
                    side_effect=[
                        current,
                        {"workflow_runs": unrelated},
                        {"workflow_runs": [prior]},
                    ],
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "previous dispatch"):
                    evidence.dispatch_identity("b" * 40, "c" * 64)
        with (
            patch.dict(os.environ, self.env),
            patch.object(evidence, "controller_identity"),
            patch.object(
                evidence,
                "api",
                side_effect=[current, {"workflow_runs": [current]}],
            ),
        ):
            evidence.dispatch_identity("b" * 40, "c" * 64)

    def test_prior_attempt_after_a_thousand_dispatches_is_still_rejected(self):
        title = "paperclip-native:" + "b" * 40 + ":" + "c" * 64
        current = {
            "id": 12,
            "workflow_id": 7,
            "run_number": 2000,
            "display_title": title,
        }

        def provider(path):
            if path.endswith("/runs/12"):
                return current
            page = int(path.rsplit("page=", 1)[1])
            if page <= 10:
                return {
                    "workflow_runs": [{**current, "display_title": "ordinary"}] * 100
                }
            # GitHub caps filtered run searches at 1,000 records.
            return {
                "workflow_runs": (
                    []
                    if "event=" in path
                    else [{**current, "id": 11, "run_number": 999}]
                )
            }

        with (
            patch.dict(os.environ, self.env),
            patch.object(evidence, "controller_identity"),
            patch.object(evidence, "api", side_effect=provider),
        ):
            with self.assertRaisesRegex(RuntimeError, "previous dispatch: 11"):
                evidence.dispatch_identity("b" * 40, "c" * 64)


class RunnerReadinessTests(unittest.TestCase):
    def test_documented_machine_size_and_repository_access(self):
        records = [
            {"id": 7, "owner": {"type": "Organization"}, "private": False},
            {
                "runners": [
                    {
                        "name": "native-arm",
                        "status": "Ready",
                        "maximum_runners": 1,
                        "machine_size_details": {"memory_gb": 64},
                        "runner_group_id": 5,
                    }
                ]
            },
            {
                "id": 5,
                "visibility": "selected",
                "allows_public_repositories": True,
            },
            {"repositories": [{"id": 7}]},
        ]
        env = {"GITHUB_REPOSITORY": "org/controller", "GITHUB_RUN_ID": "12"}
        with (
            patch.dict(os.environ, env),
            patch.object(readiness, "api", side_effect=records),
        ):
            receipt = readiness.configured("native-arm", 64)
        self.assertEqual(receipt["runner"]["name"], "native-arm")
        self.assertEqual(receipt["minimum_memory_gb"], 64)

    def test_insufficient_memory_fails_closed(self):
        records = [
            {"id": 7, "owner": {"type": "Organization"}, "private": False},
            {
                "runners": [
                    {
                        "name": "small-arm",
                        "status": "Ready",
                        "maximum_runners": 1,
                        "machine_size_details": {"memory_gb": 32},
                        "runner_group_id": 5,
                    }
                ]
            },
        ]
        with (
            patch.dict(os.environ, {"GITHUB_REPOSITORY": "org/controller"}),
            patch.object(readiness, "api", side_effect=records),
        ):
            with self.assertRaisesRegex(RuntimeError, "memory"):
                readiness.configured("small-arm", 64)


class RetentionFailureTests(unittest.TestCase):
    def test_native_rejected_initialization_keeps_dispatch_identity(self):
        env = {
            "GITHUB_REPOSITORY": "org/controller",
            "GITHUB_SHA": "a" * 40,
            "GITHUB_WORKFLOW_REF": "org/controller/workflow@refs/heads/main",
            "GITHUB_RUN_ID": "12",
            "GITHUB_RUN_ATTEMPT": "1",
            "SYSTEM": "aarch64-linux",
            "SOURCE_HEAD": "b" * 40,
            "SOURCE_PARENT": evidence.PARENT,
            "SOURCE_REVIEW_SHA256": "c" * 64,
        }
        for kind in ["missing", "invalid-json", "invalid-type"]:
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "native"
                if kind != "missing":
                    directory.mkdir()
                    (directory / "source.json").write_text(
                        "{" if kind == "invalid-json" else "[]"
                    )
                with self.subTest(kind=kind), patch.dict(os.environ, env):
                    with self.assertRaises(Exception):
                        evidence.seal(directory, "failure")
                receipt = json.loads((directory / "receipt.json").read_text())
                self.assertEqual(
                    receipt["workflow_repository"], env["GITHUB_REPOSITORY"]
                )
                self.assertEqual(receipt["workflow_sha"], env["GITHUB_SHA"])
                self.assertEqual(receipt["run_id"], env["GITHUB_RUN_ID"])
                self.assertEqual(receipt["attempt"], env["GITHUB_RUN_ATTEMPT"])
                self.assertEqual(receipt["system"], env["SYSTEM"])
                self.assertEqual(receipt["source_head"], env["SOURCE_HEAD"])
                self.assertEqual(receipt["source_parent"], env["SOURCE_PARENT"])
                self.assertEqual(
                    receipt["signed_review_sha256"], env["SOURCE_REVIEW_SHA256"]
                )
                self.assertEqual(receipt["outcome"], "failure")
                self.assertTrue(receipt["rejected"])
                self.assertFalse(receipt["qualified"])

    def test_final_artifact_readback_rejects_provider_drift_and_keeps_identity(self):
        source = {"repository": "org/controller", "head": "a" * 40, "run_id": 12}
        original = {
            "id": 13,
            "name": "workflow-retention-audit-" + source["head"],
            "expired": False,
            "created_at": "2026-10-08T00:00:00Z",
            "expires_at": "2026-11-08T00:00:00Z",
            "digest": "sha256:" + "b" * 64,
            "workflow_run": {"id": 12, "head_sha": source["head"]},
        }
        for kind in [
            "valid",
            "expired",
            "short",
            "digest",
            "foreign-run",
            "foreign-id",
            "foreign-name",
            "uploaded-digest",
        ]:
            artifact = {**original, "workflow_run": dict(original["workflow_run"])}
            if kind == "expired":
                artifact["expired"] = True
            if kind == "short":
                artifact["expires_at"] = "2026-10-09T00:00:00Z"
            if kind == "digest":
                artifact["digest"] = "sha256:" + "z" * 64
            if kind == "foreign-run":
                artifact["workflow_run"]["id"] = 99
            if kind == "foreign-id":
                artifact["id"] = 99
            if kind == "foreign-name":
                artifact["name"] = "unrelated"
            uploaded_digest = "c" * 64 if kind == "uploaded-digest" else "b" * 64
            with (
                self.subTest(kind=kind),
                tempfile.TemporaryDirectory() as temporary,
                patch.object(qualification, "identity", return_value=source),
                patch.object(qualification, "api", return_value=artifact),
            ):
                destination = Path(temporary) / "final-retention.json"
                if kind == "valid":
                    qualification.artifact(13, destination, uploaded_digest)
                else:
                    with self.assertRaises(RuntimeError):
                        qualification.artifact(13, destination, uploaded_digest)
                receipt = json.loads(destination.read_text())
                self.assertEqual(receipt["head"], source["head"])
                self.assertEqual(receipt["run_id"], source["run_id"])
                self.assertEqual(
                    receipt["outcome"], "success" if kind == "valid" else "failure"
                )
                if kind == "valid":
                    self.assertEqual(
                        receipt["artifacts"][0]["sha256"], original["digest"]
                    )
                    self.assertGreaterEqual(
                        receipt["artifacts"][0]["retention_seconds"], 2592000
                    )
                else:
                    self.assertTrue(receipt["rejected"])
                self.assertFalse(receipt["qualified"])

    def test_native_final_upload_rejects_digest_drift(self):
        workflow = (
            Path(__file__).parent / ".github/workflows/paperclip-qualification.yml"
        ).read_text()
        script = textwrap.dedent(
            workflow.split("python3 - <<'PY'")[-1].rsplit("\n          PY", 1)[0]
        )
        cases = [
            ("b" * 64, "sha256:" + "b" * 64, True),
            ("b" * 64, "sha256:" + "c" * 64, False),
            ("", "sha256:" + "b" * 64, False),
            ("z" * 64, "sha256:" + "z" * 64, False),
            ("b" * 64, "sha256:", False),
        ]
        for uploaded, stored, accepted in cases:
            artifact = {
                "expired": False,
                "created_at": "2026-10-08T00:00:00Z",
                "expires_at": "2026-11-08T00:00:00Z",
                "digest": stored,
            }
            env = {
                "GITHUB_REPOSITORY": "org/controller",
                "GH_TOKEN": "test-only",
                "ARTIFACT_ID": "13",
                "ARTIFACT_SHA256": uploaded,
            }
            with (
                self.subTest(uploaded=uploaded, stored=stored),
                patch.dict(os.environ, env),
                patch(
                    "urllib.request.urlopen",
                    return_value=io.BytesIO(json.dumps(artifact).encode()),
                ),
            ):
                if accepted:
                    exec(script, {})
                else:
                    with self.assertRaises(AssertionError):
                        exec(script, {})

    def native_artifacts(self):
        return [
            {
                "id": index + 1,
                "name": "paperclip-native-proof-" + system,
                "expired": False,
                "created_at": "2026-10-08T00:00:00Z",
                "expires_at": "2026-11-08T00:00:00Z",
                "digest": "sha256:" + "a" * 64,
            }
            for index, system in enumerate(
                ["readiness", "x86_64-linux", "aarch64-linux"]
            )
        ]

    def test_native_audit_rejections_retain_diagnostics(self):
        env = {
            "GITHUB_REPOSITORY": "org/controller",
            "GITHUB_RUN_ID": "12",
            "GITHUB_SHA": "a" * 40,
            **self.native_upload_env(),
        }
        for kind in ["source", "missing", "lifetime", "digest"]:
            artifacts = self.native_artifacts()
            head = env["GITHUB_SHA"]
            if kind == "source":
                head = "b" * 40
            if kind == "missing":
                artifacts.pop()
            if kind == "lifetime":
                artifacts[2]["expires_at"] = "2026-10-09T00:00:00Z"
            if kind == "digest":
                artifacts[2]["digest"] = ""
            with (
                self.subTest(kind=kind),
                tempfile.TemporaryDirectory() as temporary,
                patch.dict(os.environ, env),
                patch.object(
                    evidence,
                    "api",
                    side_effect=[{"head_sha": head}, {"artifacts": artifacts}],
                ),
            ):
                destination = Path(temporary) / "retention.json"
                with self.assertRaises(RuntimeError):
                    evidence.audit(destination)
                self.assertTrue(destination.is_file(), "Rejection receipt was lost")
                receipt = json.loads(destination.read_text())
                self.assertEqual(receipt["outcome"], "failure")
                self.assertTrue(receipt["rejected"])
                self.assertEqual(receipt["workflow_sha"], env["GITHUB_SHA"])
                self.assertFalse(receipt["qualified"])
                if kind != "source":
                    self.assertEqual(len(receipt["artifacts"]), 2)

    def test_native_audit_api_failure_retains_run_identity(self):
        env = {
            "GITHUB_REPOSITORY": "org/controller",
            "GITHUB_RUN_ID": "12",
            "GITHUB_SHA": "a" * 40,
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, env),
            patch.object(evidence, "api", side_effect=RuntimeError("API unavailable")),
        ):
            destination = Path(temporary) / "retention.json"
            with self.assertRaisesRegex(RuntimeError, "API unavailable"):
                evidence.audit(destination)
            receipt = json.loads(destination.read_text())
            self.assertEqual(receipt["repository"], env["GITHUB_REPOSITORY"])
            self.assertEqual(receipt["run_id"], env["GITHUB_RUN_ID"])
            self.assertEqual(receipt["outcome"], "failure")
            self.assertEqual(receipt["rejected"], ["API unavailable"])
            self.assertFalse(receipt["qualified"])

    def test_native_audit_keeps_all_three_valid_artifacts(self):
        env = {
            "GITHUB_REPOSITORY": "org/controller",
            "GITHUB_RUN_ID": "12",
            "GITHUB_SHA": "a" * 40,
            **self.native_upload_env(),
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, env),
            patch.object(evidence, "controller_identity"),
            patch.object(evidence, "source_identity", return_value={}),
            patch.object(
                evidence,
                "api",
                side_effect=[
                    {"head_sha": env["GITHUB_SHA"]},
                    {"artifacts": self.native_artifacts()},
                ],
            ),
        ):
            destination = Path(temporary) / "retention.json"
            evidence.audit(destination)
            receipt = json.loads(destination.read_text())
            self.assertEqual(len(receipt["artifacts"]), 3)
            self.assertEqual(receipt["outcome"], "success")
            self.assertEqual(receipt["rejected"], [])
            self.assertFalse(receipt["qualified"])

    def native_upload_env(self):
        env = {}
        for artifact in self.native_artifacts():
            system = artifact["name"].removeprefix("paperclip-native-proof-")
            prefix = system.upper().replace("-", "_") + "_ARTIFACT_"
            env[prefix + "ID"] = str(artifact["id"])
            env[prefix + "SHA256"] = artifact["digest"].removeprefix("sha256:")
        return env

    def test_native_audit_binds_each_proof_to_its_upload_identity(self):
        systems = ["readiness", "x86_64-linux", "aarch64-linux"]
        for index, system in enumerate(systems):
            for kind in [
                "valid",
                "provider-drift",
                "missing-upload-digest",
                "malformed-upload-digest",
                "different-upload-id",
                "missing-upload-id",
                "different-proof-name",
            ]:
                artifacts = self.native_artifacts()
                env = {
                    "GITHUB_REPOSITORY": "org/controller",
                    "GITHUB_RUN_ID": "12",
                    "GITHUB_SHA": "a" * 40,
                }
                for proof_system, artifact in zip(systems, artifacts):
                    prefix = proof_system.upper().replace("-", "_") + "_ARTIFACT_"
                    env[prefix + "ID"] = str(artifact["id"])
                    env[prefix + "SHA256"] = artifact["digest"].removeprefix("sha256:")
                prefix = system.upper().replace("-", "_") + "_ARTIFACT_"
                if kind == "provider-drift":
                    artifacts[index]["digest"] = "sha256:" + "b" * 64
                elif kind == "missing-upload-digest":
                    env[prefix + "SHA256"] = ""
                elif kind == "malformed-upload-digest":
                    env[prefix + "SHA256"] = "z" * 64
                elif kind == "different-upload-id":
                    env[prefix + "ID"] = "99"
                elif kind == "missing-upload-id":
                    env[prefix + "ID"] = ""
                elif kind == "different-proof-name":
                    artifacts[index]["name"] = "paperclip-native-proof-unexpected"
                with (
                    self.subTest(system=system, kind=kind),
                    tempfile.TemporaryDirectory() as temporary,
                    patch.dict(os.environ, env),
                    patch.object(evidence, "controller_identity"),
                    patch.object(evidence, "source_identity", return_value={}),
                    patch.object(
                        evidence,
                        "api",
                        side_effect=[
                            {"head_sha": env["GITHUB_SHA"]},
                            {"artifacts": artifacts},
                        ],
                    ),
                ):
                    destination = Path(temporary) / "retention.json"
                    if kind == "valid":
                        evidence.audit(destination)
                    else:
                        with self.assertRaises(RuntimeError):
                            evidence.audit(destination)
                    receipt = json.loads(destination.read_text())
                    self.assertEqual(
                        receipt["outcome"], "success" if kind == "valid" else "failure"
                    )
                    self.assertFalse(receipt["qualified"])
                    if kind == "valid":
                        self.assertEqual(len(receipt["artifacts"]), 3)
                    else:
                        self.assertTrue(receipt["rejected"])

    def test_final_audit_rejects_live_controller_movement(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(evidence, "validate_retention"),
            patch.object(
                evidence, "controller_identity", side_effect=RuntimeError("main moved")
            ),
        ):
            destination = Path(temporary) / "retention.json"
            with self.assertRaisesRegex(RuntimeError, "main moved"):
                evidence.audit(destination)
            receipt = json.loads(destination.read_text())
            self.assertEqual(receipt["outcome"], "failure")
            self.assertEqual(receipt["rejected"], ["main moved"])

    def test_rejected_source_initialization_still_seals_run_identity(self):
        for kind in ["missing", "invalid-json", "invalid-type"]:
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "evidence"
                directory.mkdir()
                event = Path(temporary) / "event.json"
                event.write_text(
                    json.dumps(
                        {
                            "pull_request": {
                                "number": 2,
                                "head": {"sha": "a" * 40},
                                "base": {"sha": "b" * 40},
                            }
                        }
                    )
                )
                env = {
                    "GITHUB_REPOSITORY": "org/controller",
                    "GITHUB_EVENT_PATH": str(event),
                    "GITHUB_EVENT_NAME": "pull_request",
                    "GITHUB_WORKFLOW_REF": "org/controller/workflow@refs/pull/2/merge",
                    "GITHUB_WORKFLOW_SHA": "c" * 40,
                    "GITHUB_RUN_ID": "12",
                    "GITHUB_RUN_ATTEMPT": "2",
                }
                if kind == "invalid-json":
                    (directory / "source.json").write_text("{")
                if kind == "invalid-type":
                    (directory / "source.json").write_text("[]")
                with self.subTest(kind=kind), patch.dict(os.environ, env):
                    with self.assertRaises(Exception):
                        qualification.seal(directory, "failure", False)
                receipt = json.loads((directory / "receipt.json").read_text())
                self.assertEqual(receipt["outcome"], "failure")
                self.assertEqual(receipt["head"], "a" * 40)
                self.assertEqual(receipt["run_id"], 12)
                self.assertEqual(receipt["run_attempt"], 2)
                self.assertTrue(receipt["rejected"])
                self.assertFalse(receipt["qualified"])

    def test_rejected_retention_audit_keeps_its_failure_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "retention-audit.json"
            with patch.object(
                qualification, "identity", side_effect=RuntimeError("PR head advanced")
            ):
                with self.assertRaisesRegex(RuntimeError, "PR head advanced"):
                    qualification.artifacts("workflow-retention-", 1, destination)
            receipt = json.loads(destination.read_text())
            self.assertEqual(receipt["outcome"], "failure")
            self.assertEqual(receipt["rejected"], ["PR head advanced"])
            self.assertFalse(receipt["qualified"])


class NativeWorkflowFailureTests(unittest.TestCase):
    def job(self, name):
        workflow = (
            Path(__file__).parent / ".github/workflows/paperclip-qualification.yml"
        ).read_text()
        body = workflow.split(f"  {name}:\n", 1)[1]
        body = re.split(r"(?m)^  [a-z]+:\n", body, maxsplit=1)[0]
        header, steps = body.split("    steps:\n", 1)
        return header, re.split(r"(?m)^      - ", steps)[1:]

    def run_script(self, step):
        match = re.search(r"(?:^|\n        )run: (.*)", step)
        if match is None:
            return None
        if match[1] == "|":
            return textwrap.dedent(step[match.end() + 1 :])
        return match[1]

    def test_proof_upload_matrix_exports_preserve_both_architectures(self):
        header, steps = self.job("native")
        _, retention_steps = self.job("retention")
        exporter = next(step for step in steps if "id: proof_identity" in step)
        self.assertIn("if: always() && steps.proof.outcome == 'success'", exporter)
        self.assertIn("ARTIFACT_ID: ${{ steps.proof.outputs.artifact-id }}", exporter)
        self.assertIn(
            "ARTIFACT_SHA256: ${{ steps.proof.outputs.artifact-digest }}", exporter
        )
        combined = {}
        for index, system in enumerate(["x86_64-linux", "aarch64-linux"], start=1):
            with self.subTest(system=system), tempfile.TemporaryDirectory() as temporary:
                destination = Path(temporary) / "github-output"
                env = {
                    **os.environ,
                    "SYSTEM": system,
                    "ARTIFACT_ID": str(index),
                    "ARTIFACT_SHA256": str(index) * 64,
                    "GITHUB_OUTPUT": str(destination),
                }
                completed = subprocess.run(
                    ["bash", "-eo", "pipefail", "-c", self.run_script(exporter)],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                outputs = dict(
                    line.split("=", 1) for line in destination.read_text().splitlines()
                )
                prefix = system.replace("-", "_") + "_artifact_"
                self.assertEqual(
                    outputs,
                    {prefix + "id": str(index), prefix + "sha256": str(index) * 64},
                )
                self.assertTrue(combined.keys().isdisjoint(outputs))
                for key in outputs:
                    self.assertIn(
                        key + ": ${{ steps.proof_identity.outputs." + key + " }}",
                        header,
                    )
                combined.update(outputs)
        self.assertEqual(len(combined), 4)
        retention_header, _ = self.job("retention")
        for key in combined:
            self.assertIn(
                key.upper() + ": ${{ needs.native.outputs." + key + " }}",
                retention_header,
            )
        self.assertTrue(
            any("paperclip-evidence.py audit" in step for step in retention_steps)
        )

    def test_failed_source_checkout_retains_receipt_at_the_upload_path(self):
        header, steps = self.job("native")
        for system in ["x86_64-linux", "aarch64-linux"]:
            with (
                self.subTest(system=system),
                tempfile.TemporaryDirectory() as temporary,
            ):
                controller = Path(temporary) / "controller"
                controller.mkdir()
                runner = Path(temporary) / "runner temp"
                runner.mkdir()
                (controller / "paperclip-evidence.py").write_bytes(
                    Path(__file__).with_name("paperclip-evidence.py").read_bytes()
                )
                env = {
                    **os.environ,
                    "RUNNER_TEMP": str(runner),
                    "GITHUB_ENV": str(runner / "github-env"),
                    "GITHUB_REPOSITORY": "org/controller",
                    "GITHUB_SHA": "a" * 40,
                    "GITHUB_RUN_ID": "12",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "SYSTEM": system,
                    "SOURCE_HEAD": "b" * 40,
                    "SOURCE_PARENT": evidence.PARENT,
                    "SOURCE_REVIEW_SHA256": "c" * 64,
                }
                env.pop("EVIDENCE", None)
                declared = re.search(r"(?m)^      EVIDENCE: (.+)$", header)
                if declared:
                    env["EVIDENCE"] = (
                        declared[1]
                        .strip("'\"")
                        .replace("${{ runner.temp }}", str(runner))
                    )
                for step in steps:
                    if "repository: NixOS/nixpkgs" in step:
                        break  # The external source checkout fails here.
                    script = self.run_script(step)
                    if script:
                        completed = subprocess.run(
                            ["bash", "-eo", "pipefail", "-c", script],
                            cwd=controller,
                            env=env,
                            capture_output=True,
                            text=True,
                        )
                        self.assertEqual(completed.returncode, 0, completed.stderr)
                        exports = Path(env["GITHUB_ENV"])
                        if exports.is_file():
                            for line in exports.read_text().splitlines():
                                key, value = line.split("=", 1)
                                env[key] = value
                else:
                    self.fail("The native source checkout is missing")
                sealer = next(
                    step for step in steps if "paperclip-evidence.py seal" in step
                )
                script = self.run_script(sealer).replace("${{ job.status }}", "failure")
                completed = subprocess.run(
                    ["bash", "-eo", "pipefail", "-c", script],
                    cwd=controller,
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(completed.returncode, 0)
                uploader = next(
                    step for step in steps if "uses: actions/upload-artifact@" in step
                )
                upload_path = re.search(r"(?m)^          path: (.+)$", uploader)[1]
                directory = Path(upload_path.replace("${{ runner.temp }}", str(runner)))
                self.assertTrue(
                    (directory / "receipt.json").is_file(), completed.stderr
                )
                self.assertFalse((controller / "receipt.json").exists())
                receipt = json.loads((directory / "receipt.json").read_text())
                self.assertEqual(receipt["source_head"], env["SOURCE_HEAD"])
                self.assertEqual(receipt["system"], system)
                self.assertEqual(receipt["outcome"], "failure")
                self.assertTrue(receipt["rejected"])
                self.assertFalse(receipt["qualified"])

    def test_final_readback_runs_on_rejection_before_prerequisite_result_gates(self):
        _, steps = self.job("retention")
        step = next(step for step in steps if "ARTIFACT_ID:" in step)
        condition = re.search(r"(?m)^        if: (.+)$", step)
        cases = [
            ("success", "success", False, False),
            ("success", "success", True, False),
            ("failure", "skipped", True, False),
            ("success", "failure", True, False),
            ("success", "cancelled", True, False),
            ("success", "success", True, True),
        ]
        for ready, native, audit_failed, drift in cases:
            with (
                self.subTest(
                    ready=ready, native=native, audit_failed=audit_failed, drift=drift
                ),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = Path(temporary)
                marker = directory / "provider-readback"
                shim = directory / "python3"
                shim.write_text(
                    f"#!{sys.executable}\n"
                    "import io, json, os, sys, urllib.request\n"
                    "from pathlib import Path\n"
                    "def provider(request, timeout):\n"
                    "    Path(os.environ['READBACK_MARKER']).write_text(request.full_url)\n"
                    "    return io.BytesIO(os.environ['PROVIDER_BYTES'].encode())\n"
                    "urllib.request.urlopen = provider\n"
                    "exec(sys.stdin.read(), {})\n"
                )
                shim.chmod(0o700)
                env = {
                    **os.environ,
                    "PATH": str(directory) + os.pathsep + os.environ["PATH"],
                    "GITHUB_REPOSITORY": "org/controller",
                    "GH_TOKEN": "test-only",
                    "ARTIFACT_ID": "13",
                    "ARTIFACT_SHA256": "b" * 64,
                    "READBACK_MARKER": str(marker),
                    "PROVIDER_BYTES": json.dumps(
                        {
                            "expired": False,
                            "created_at": "2026-10-08T00:00:00Z",
                            "expires_at": "2026-11-08T00:00:00Z",
                            "digest": "sha256:" + ("c" if drift else "b") * 64,
                        }
                    ),
                }
                completed = None
                # Actions adds success() when no status-check function is present.
                if not audit_failed or (condition and "always()" in condition[1]):
                    script = self.run_script(step)
                    script = script.replace("${{ needs.readiness.result }}", ready)
                    script = script.replace("${{ needs.native.result }}", native)
                    completed = subprocess.run(
                        ["bash", "-eo", "pipefail", "-c", script],
                        cwd=directory,
                        env=env,
                        capture_output=True,
                        text=True,
                    )
                self.assertTrue(
                    marker.is_file(), "Final uploaded rejection was not read back"
                )
                self.assertEqual(
                    marker.read_text(),
                    "https://api.github.com/repos/org/controller/actions/artifacts/13",
                )
                accepted = ready == native == "success" and not drift
                self.assertEqual(completed.returncode == 0, accepted, completed.stderr)


if __name__ == "__main__":
    unittest.main()
