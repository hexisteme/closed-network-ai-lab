"""Releases of the gateway configuration: validation, atomic switching, rollback and the rehearsal."""

import contextlib
import fcntl
import io
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "gateway"))

import release  # noqa: E402
from model_gateway import load_gateway_policy  # noqa: E402
from release import (  # noqa: E402
    ReleaseError,
    ArchiveIncomplete,
    SwitchIncomplete,
    archive_switch_log,
    active_release_id,
    compare_pins,
    load_release,
    rollback_release,
    switch_release,
    validate_release,
)
from release_rehearsal import audit_matches, run_rehearsal  # noqa: E402

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
        # The log is opened before the pointer moves, so it exists, but nothing was recorded and no temporary file is left.
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["ACTIVE", "audit.jsonl", "r1.json", "r2.json"])
        self.assertEqual(self.audit.read_text(encoding="utf-8"), "")

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

    def test_a_log_that_cannot_be_opened_stops_the_switch_before_the_pointer_moves(self):
        with self.assertRaises(OSError):
            switch_release(self.dir, "r2", self.dir / "missing" / "audit.jsonl", action="activate", reason="test")
        self.assertEqual(active_release_id(self.dir), "r1")

    def test_a_record_lost_after_the_switch_is_reported_as_unlogged_not_refused(self):
        self.audit.write_text(json.dumps({"action": "activate", "from": "r2", "to": "r1"}) + "\n", encoding="utf-8")
        with mock.patch.object(release, "_write_line", side_effect=OSError("disk full")):
            with self.assertRaises(SwitchIncomplete) as caught:
                switch_release(self.dir, "r2", self.audit, action="activate", reason="test")
        self.assertEqual((caught.exception.previous, caught.exception.target), ("r1", "r2"))
        self.assertFalse(caught.exception.logged)
        self.assertEqual(active_release_id(self.dir), "r2")
        with self.assertRaisesRegex(ReleaseError, "ends at r1"):
            rollback_release(self.dir, self.audit, reason="the log does not know r2")
        self.assertEqual(active_release_id(self.dir), "r2")

    def test_the_command_line_separates_an_unlogged_switch_from_a_refusal(self):
        err = io.StringIO()
        with mock.patch.object(release, "_write_line", side_effect=OSError("disk full")), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = release.main(["--releases", str(self.dir), "--log", str(self.audit), "activate", "r2", "--reason", "test"])
        self.assertEqual(code, 3)
        self.assertIn("switched r1 -> r2", err.getvalue())
        self.assertNotIn("refused", err.getvalue())

    def test_activating_the_active_release_changes_nothing(self):
        switch_release(self.dir, "r2", self.audit, action="activate", reason="test")
        with self.assertRaisesRegex(ReleaseError, "already active"):
            switch_release(self.dir, "r2", self.audit, action="activate", reason="again")
        self.assertEqual(len(self.records()), 1)
        rollback_release(self.dir, self.audit, reason="back")
        self.assertEqual(active_release_id(self.dir), "r1")

    def test_rollback_uses_only_the_newest_record_and_it_must_match_the_pointer(self):
        rows = [{"action": "activate", "from": "r1", "to": "r2"}, {"action": "activate", "from": "r2", "to": "r3"}]
        self.audit.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        (self.dir / "ACTIVE").write_text("r2\n", encoding="utf-8")
        with self.assertRaisesRegex(ReleaseError, "ends at r3"):
            rollback_release(self.dir, self.audit, reason="contradictory tail")
        self.assertEqual(active_release_id(self.dir), "r2")

    def test_a_corrupt_switch_log_is_refused_by_name_before_anything_changes(self):
        good = json.dumps({"action": "activate", "from": "r1", "to": "r2"})
        cases = {
            "truncated line": good + "\n" + good[:-5] + "\n",
            "blank line": good + "\n\n",
            "not an object": good + "\n[]\n",
            "missing from": good + "\n" + json.dumps({"action": "activate", "to": "r2"}) + "\n",
            "switch to itself": json.dumps({"action": "activate", "from": "r2", "to": "r2"}) + "\n",
            "bad id": json.dumps({"action": "activate", "from": "../r1", "to": "r2"}) + "\n",
        }
        (self.dir / "ACTIVE").write_text("r2\n", encoding="utf-8")
        for name, text in cases.items():
            with self.subTest(name):
                self.audit.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(ReleaseError, "switch log line"):
                    rollback_release(self.dir, self.audit, reason=name)
                self.assertEqual(active_release_id(self.dir), "r2")
                self.assertEqual(self.audit.read_text(encoding="utf-8"), text)

    def test_a_switch_waits_for_one_already_in_progress(self):
        holder = os.open(self.dir, os.O_RDONLY)
        fcntl.flock(holder, fcntl.LOCK_EX)
        worker = threading.Thread(target=switch_release, args=(self.dir, "r2", self.audit),
                                  kwargs={"action": "activate", "reason": "queued"})
        try:
            worker.start()
            time.sleep(0.3)
            self.assertEqual(active_release_id(self.dir), "r1")
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            os.close(holder)
        worker.join(timeout=5)
        self.assertEqual(active_release_id(self.dir), "r2")

    def test_the_pointer_and_the_record_are_synced_before_the_switch_returns(self):
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            events.append("fsync-dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync-file")
            real_fsync(fd)

        def replace(src, dst):
            events.append("replace")
            real_replace(src, dst)

        with mock.patch.object(release.os, "fsync", side_effect=fsync), \
                mock.patch.object(release.os, "replace", side_effect=replace):
            switch_release(self.dir, "r2", self.audit, action="activate", reason="test")
        # temporary pointer, rename, releases directory, log line, then the log's directory and its parent
        self.assertEqual(events, ["fsync-file", "replace", "fsync-dir", "fsync-file", "fsync-dir", "fsync-dir"])
        events.clear()
        with mock.patch.object(release.os, "fsync", side_effect=fsync), \
                mock.patch.object(release.os, "replace", side_effect=replace):
            switch_release(self.dir, "r1", self.audit, action="activate", reason="the log exists now")
        # A log that already exists still has its directories synced, so a retry after a failed sync is covered.
        self.assertEqual(events, ["fsync-file", "replace", "fsync-dir", "fsync-file", "fsync-dir", "fsync-dir"])

    def test_a_directory_sync_failure_after_the_rename_reports_the_switch(self):
        with mock.patch.object(release, "_sync_directory", side_effect=OSError("I/O error")):
            with self.assertRaises(SwitchIncomplete) as caught:
                switch_release(self.dir, "r2", self.audit, action="activate", reason="test")
        self.assertIn("releases directory", str(caught.exception))
        self.assertFalse(caught.exception.logged)
        self.assertEqual(active_release_id(self.dir), "r2")

    def test_a_line_written_but_not_synced_is_reported_as_written(self):
        real_fsync, calls = os.fsync, []

        def fsync(fd):
            calls.append(fd)
            if len(calls) == 3:  # temporary pointer, releases directory, then the log line
                raise OSError("I/O error")
            real_fsync(fd)

        err = io.StringIO()
        with mock.patch.object(release.os, "fsync", side_effect=fsync), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = release.main(["--releases", str(self.dir), "--log", str(self.audit), "activate", "r2", "--reason", "test"])
        self.assertEqual(code, 3)
        self.assertIn("the log line was written", err.getvalue())
        self.assertEqual([r["to"] for r in self.records()], ["r2"])
        rollback_release(self.dir, self.audit, reason="the line is there")
        self.assertEqual(active_release_id(self.dir), "r1")

    def test_text_that_is_not_utf8_is_a_named_refusal(self):
        self.audit.write_bytes(b"\xff\xfe\n")
        with self.assertRaisesRegex(ReleaseError, "UTF-8"):
            rollback_release(self.dir, self.audit, reason="bad bytes")
        (self.dir / "r2.json").write_bytes(b"\xff")
        with self.assertRaises(ReleaseError):
            load_release(self.dir, "r2")
        (self.dir / "ACTIVE").write_bytes(b"\xff\n")
        with self.assertRaises(ReleaseError):
            active_release_id(self.dir)

    def test_archiving_a_damaged_log_lets_a_new_history_start(self):
        damaged = json.dumps({"action": "activate", "from": "r1", "to": "r2"})[:-4] + "\n"
        self.audit.write_text(damaged, encoding="utf-8")
        archived = archive_switch_log(self.dir, self.audit)
        self.assertEqual(archived.read_text(encoding="utf-8"), damaged)
        self.assertFalse(self.audit.exists())
        with self.assertRaises(ReleaseError):
            rollback_release(self.dir, self.audit, reason="no history yet")
        switch_release(self.dir, "r2", self.audit, action="activate", reason="new history")
        rollback_release(self.dir, self.audit, reason="back")
        self.assertEqual(active_release_id(self.dir), "r1")

    def test_the_command_line_creates_one_missing_log_directory(self):
        log = self.dir / "logs" / "switches.jsonl"
        with contextlib.redirect_stdout(io.StringIO()):
            code = release.main(["--releases", str(self.dir), "--log", str(log), "activate", "r2", "--reason", "test"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(log.read_text(encoding="utf-8"))["to"], "r2")

    def test_a_log_directory_whose_parent_is_missing_is_refused_before_the_switch(self):
        log = self.dir / "new" / "logs" / "switches.jsonl"
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = release.main(["--releases", str(self.dir), "--log", str(log), "activate", "r2", "--reason", "test"])
        self.assertEqual(code, 2)
        self.assertIn("refused", err.getvalue())
        self.assertEqual(active_release_id(self.dir), "r1")

    def test_a_log_directory_created_meanwhile_is_not_an_error(self):
        log = self.dir / "logs" / "switches.jsonl"
        real_mkdir = Path.mkdir

        def mkdir(path, *args, **kwargs):
            real_mkdir(path)  # another process creates it first
            return real_mkdir(path, *args, **kwargs)

        with mock.patch.object(Path, "mkdir", mkdir), contextlib.redirect_stdout(io.StringIO()):
            code = release.main(["--releases", str(self.dir), "--log", str(log), "activate", "r2", "--reason", "test"])
        self.assertEqual(code, 0)

    def test_an_archive_whose_sync_fails_is_reported_as_archived(self):
        self.audit.write_text(json.dumps({"action": "activate", "from": "r1", "to": "r2"}) + "\n", encoding="utf-8")
        with mock.patch.object(release, "_sync_directory", side_effect=OSError("I/O error")):
            with self.assertRaises(ArchiveIncomplete) as caught:
                archive_switch_log(self.dir, self.audit)
        self.assertTrue(caught.exception.target.is_file())
        self.assertFalse(self.audit.exists())
        err = io.StringIO()
        self.audit.write_text(json.dumps({"action": "activate", "from": "r1", "to": "r2"}) + "\n", encoding="utf-8")
        with mock.patch.object(release, "_sync_directory", side_effect=OSError("I/O error")), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = release.main(["--releases", str(self.dir), "--log", str(self.audit), "archive-log"])
        self.assertEqual(code, 3)
        self.assertIn("archived to", err.getvalue())
        self.assertNotIn("refused", err.getvalue())


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

    def test_an_empty_release_dir_is_refused_rather_than_ignored(self):
        for value in ("", "  "):
            with self.subTest(repr(value)):
                with self.assertRaises(ReleaseError):
                    load_gateway_policy({"RELEASE_DIR": value, "LAB_MODELS": "llama3:8b"})

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
            for step in evidence["steps"]:
                self.assertEqual(step["gateway_audit_releases"], [step["serving_release"]])
                self.assertTrue(step["audit_matches"], step["step"])
            self.assertTrue(evidence["passed"])

            written = json.loads((Path(evidence_dir) / evidence["evidence_file"]).read_text(encoding="utf-8"))
            self.assertEqual(written["switch_log_sha256"], evidence["switch_log_sha256"])
            self.assertTrue((Path(evidence_dir) / evidence["switch_log"]).is_file())
        self.assertEqual(snapshot(RELEASES), before)


class AuditMatchesTest(unittest.TestCase):
    def lines(self, *releases):
        return [json.dumps({"release": r, "path": "/api/chat"}) for r in releases]

    def test_every_request_must_name_the_serving_release(self):
        self.assertTrue(audit_matches("r1", self.lines("r1", "r1"), expected_requests=2))
        self.assertFalse(audit_matches("r1", self.lines("r1", "r2"), expected_requests=2))

    def test_the_request_count_must_match(self):
        self.assertFalse(audit_matches("r1", self.lines("r1"), expected_requests=2))
        self.assertFalse(audit_matches("r1", [], expected_requests=0))


if __name__ == "__main__":
    unittest.main()
