"""Releases of the gateway configuration: validation, atomic switching, rollback and the rehearsal."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "gateway"))

import release  # noqa: E402
from model_gateway import load_gateway_policy  # noqa: E402
from release import (  # noqa: E402
    ReleaseError,
    active_release_id,
    compare_pins,
    load_release,
    rollback_release,
    switch_release,
    validate_release,
)
from release_rehearsal import run_rehearsal  # noqa: E402

RELEASES = LAB / "releases"
INSTALLED = LAB / "tests" / "fixtures" / "ollama-tags-20261023.json"
DIGEST = "a" * 64


def manifest(release_id="r9", models=None):
    return {
        "release": release_id,
        "note": "test",
        "models": models if models is not None else [{"name": "qwen3:4b", "digest": DIGEST}],
    }


class ValidateReleaseTest(unittest.TestCase):
    def test_a_well_formed_release_has_no_problems(self):
        self.assertEqual(validate_release(manifest(), "r9"), [])

    def test_cloud_models_cannot_be_released(self):
        problems = validate_release(manifest(models=[{"name": "gpt-oss:120b-cloud", "digest": DIGEST}]), "r9")
        self.assertIn("cloud model in release: gpt-oss:120b-cloud", problems)

    def test_digest_must_be_64_hex_characters(self):
        problems = validate_release(manifest(models=[{"name": "qwen3:4b", "digest": "latest"}]), "r9")
        self.assertIn("bad digest for qwen3:4b", problems)

    def test_the_same_model_cannot_be_listed_twice(self):
        models = [{"name": "bge-m3", "digest": DIGEST}, {"name": "bge-m3:latest", "digest": DIGEST}]
        self.assertIn("model listed twice: bge-m3:latest", validate_release(manifest(models=models), "r9"))

    def test_release_id_must_match_its_file_name(self):
        self.assertIn("release id r9 does not match file r8", validate_release(manifest(), "r8"))

    def test_a_release_must_list_at_least_one_model(self):
        self.assertIn("no models", validate_release(manifest(models=[]), "r9"))


class SwitchReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for release_id in ("r1", "r2"):
            (self.dir / f"{release_id}.json").write_text(json.dumps(manifest(release_id)), encoding="utf-8")
        (self.dir / "ACTIVE").write_text("r1\n", encoding="utf-8")
        self.audit = self.dir / "audit.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def records(self):
        return [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]

    def test_switch_moves_the_pointer_and_appends_one_record(self):
        record = switch_release(self.dir, "r2", self.audit, action="activate", reason="test")
        self.assertEqual(active_release_id(self.dir), "r2")
        self.assertEqual((self.dir / "ACTIVE").stat().st_mode & 0o777, 0o644)
        self.assertEqual((record["action"], record["from"], record["to"]), ("activate", "r1", "r2"))
        self.assertEqual(len(record["release_sha256"]), 64)
        self.assertEqual(self.records(), [record])

    def test_an_invalid_release_is_refused_and_nothing_changes(self):
        (self.dir / "r3.json").write_text(json.dumps(manifest("r3", models=[])), encoding="utf-8")
        with self.assertRaises(ReleaseError):
            switch_release(self.dir, "r3", self.audit, action="activate", reason="test")
        self.assertEqual(active_release_id(self.dir), "r1")
        self.assertFalse(self.audit.exists())

    def test_a_missing_release_is_refused(self):
        with self.assertRaises(ReleaseError):
            switch_release(self.dir, "r7", self.audit, action="activate", reason="test")
        self.assertEqual(active_release_id(self.dir), "r1")

    def test_an_interrupted_switch_leaves_the_old_pointer_whole(self):
        with mock.patch.object(release.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                switch_release(self.dir, "r2", self.audit, action="activate", reason="test")
        self.assertEqual(active_release_id(self.dir), "r1")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["ACTIVE", "r1.json", "r2.json"])
        self.assertFalse(self.audit.exists())

    def test_rollback_returns_to_the_release_that_was_active_before(self):
        switch_release(self.dir, "r2", self.audit, action="activate", reason="test")
        record = rollback_release(self.dir, self.audit, reason="checks failed")
        self.assertEqual(active_release_id(self.dir), "r1")
        self.assertEqual((record["action"], record["from"], record["to"]), ("rollback", "r2", "r1"))
        self.assertEqual([r["action"] for r in self.records()], ["activate", "rollback"])

    def test_rollback_without_history_is_refused(self):
        with self.assertRaises(ReleaseError):
            rollback_release(self.dir, self.audit, reason="nothing to undo")
        self.assertEqual(active_release_id(self.dir), "r1")

    def test_pointer_must_name_a_release(self):
        (self.dir / "ACTIVE").write_text("../r1\n", encoding="utf-8")
        with self.assertRaises(ReleaseError):
            active_release_id(self.dir)


class ComparePinsTest(unittest.TestCase):
    def setUp(self):
        self.installed = json.loads(INSTALLED.read_text(encoding="utf-8"))

    def test_the_repository_release_matches_the_recorded_host_weights(self):
        self.assertEqual(compare_pins(load_release(RELEASES, "r1"), self.installed), [])

    def test_a_different_installed_digest_is_reported(self):
        changed = manifest(models=[{"name": "qwen3:4b", "digest": DIGEST}])
        problems = compare_pins(changed, self.installed)
        self.assertEqual([p["model"] for p in problems], ["qwen3:4b"])
        self.assertEqual(problems[0]["problem"], "digest_mismatch")

    def test_a_model_that_is_not_installed_is_reported(self):
        missing = manifest(models=[{"name": "qwen3:8b", "digest": DIGEST}])
        self.assertEqual(compare_pins(missing, self.installed), [{"model": "qwen3:8b", "problem": "not_installed", "pinned": DIGEST, "installed": None}])


class GatewayPolicyTest(unittest.TestCase):
    def test_release_mode_takes_the_allowlist_from_the_active_release(self):
        release_id, allowed = load_gateway_policy({"RELEASE_DIR": str(RELEASES)})
        self.assertEqual(release_id, "r1")
        self.assertEqual(allowed, frozenset({"qwen3:4b", "gemma3:4b", "bge-m3:latest"}))

    def test_release_mode_fails_closed_without_a_valid_release(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(ReleaseError):
                load_gateway_policy({"RELEASE_DIR": empty, "LAB_MODELS": "qwen3:4b"})

    def test_without_releases_the_environment_allowlist_still_works(self):
        self.assertEqual(load_gateway_policy({"LAB_MODELS": "qwen3:4b"}), (None, frozenset({"qwen3:4b"})))


class RepositoryReleaseTest(unittest.TestCase):
    def test_the_active_release_is_the_lab_configuration(self):
        current = load_release(RELEASES, active_release_id(RELEASES))
        names = {m["name"] for m in current["models"]}
        self.assertEqual(names, {"qwen3:4b", "gemma3:4b", "bge-m3:latest"})

    def test_compose_runs_the_gateway_from_the_release_directory_read_only(self):
        compose = (LAB / "compose.yaml").read_text(encoding="utf-8")
        gateway = compose.split("  model-gateway:", 1)[1].split("\n  egress-check:", 1)[0]
        self.assertIn("RELEASE_DIR: /releases", gateway)
        self.assertIn("./releases:/releases:ro", gateway)
        self.assertNotIn("LAB_MODELS", gateway)


def snapshot(directory: Path) -> dict:
    return {p.name: (p.read_bytes(), p.stat().st_mode & 0o777) for p in sorted(directory.iterdir())}


class RehearsalTest(unittest.TestCase):
    def test_bad_release_fails_its_checks_and_rollback_restores_the_good_one(self):
        before = snapshot(RELEASES)
        with tempfile.TemporaryDirectory() as evidence_dir:
            evidence = run_rehearsal(RELEASES, INSTALLED, evidence_dir)
            steps = {s["step"]: s for s in evidence["steps"]}
            self.assertTrue(steps["baseline"]["checks_passed"])
            self.assertFalse(steps["bad_release"]["checks_passed"])
            self.assertEqual(steps["bad_release"]["failed_checks"], ["embed:bge-m3:latest"])
            self.assertTrue(steps["after_rollback"]["checks_passed"])
            self.assertEqual([s["serving_release"] for s in evidence["steps"]], ["r1", "r2", "r1"])
            self.assertEqual([r["action"] for r in evidence["switches"]], ["activate", "rollback"])
            self.assertEqual(evidence["final_active_release"], "r1")
            self.assertTrue(evidence["passed"])

            written = json.loads((Path(evidence_dir) / evidence["evidence_file"]).read_text(encoding="utf-8"))
            self.assertEqual(written["switch_log_sha256"], evidence["switch_log_sha256"])
            self.assertTrue((Path(evidence_dir) / evidence["switch_log"]).is_file())
        self.assertEqual(snapshot(RELEASES), before)


if __name__ == "__main__":
    unittest.main()
