import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
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
                self.assertEqual(receipt["workflow_repository"], env["GITHUB_REPOSITORY"])
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
        for kind in ["valid", "expired", "short", "digest", "foreign-run", "foreign-id"]:
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
            with (
                self.subTest(kind=kind),
                tempfile.TemporaryDirectory() as temporary,
                patch.object(qualification, "identity", return_value=source),
                patch.object(qualification, "api", return_value=artifact),
            ):
                destination = Path(temporary) / "final-retention.json"
                if kind == "valid":
                    qualification.artifact(13, destination)
                else:
                    with self.assertRaises(RuntimeError):
                        qualification.artifact(13, destination)
                receipt = json.loads(destination.read_text())
                self.assertEqual(receipt["head"], source["head"])
                self.assertEqual(receipt["run_id"], source["run_id"])
                self.assertEqual(
                    receipt["outcome"], "success" if kind == "valid" else "failure"
                )
                if kind == "valid":
                    self.assertEqual(receipt["artifacts"][0]["sha256"], original["digest"])
                    self.assertGreaterEqual(
                        receipt["artifacts"][0]["retention_seconds"], 2592000
                    )
                else:
                    self.assertTrue(receipt["rejected"])
                self.assertFalse(receipt["qualified"])

    def native_artifacts(self):
        return [
            {
                "id": index,
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


if __name__ == "__main__":
    unittest.main()
