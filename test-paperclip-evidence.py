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
                patch.object(evidence, "source_identity", side_effect=RuntimeError(reason)),
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
        closure = {path: {"narHash": "sha256:content", "narSize": 42} for path in outputs}
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
