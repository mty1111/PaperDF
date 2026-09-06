"""Filesystem safety tests. Fixtures are fake PDFs in temporary directories."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import paperdf_workflow as workflow


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.journals = self.root / "journals"

    def pdf(self, name, content=b"%PDF-1.7\nfixture"):
        path = self.root / name
        path.write_bytes(content)
        return path

    def plan(self, source, name):
        return workflow.plan_renames([(str(source), name)])[0]

    def apply(self, plans, **kwargs):
        return workflow.apply_batch(plans, self.journals, **kwargs)

    def undo(self, **kwargs):
        return workflow.undo_last_batch(self.journals, **kwargs)

    def read_journal(self):
        path = sorted(self.journals.glob("*.json"))[-1]
        return path, json.loads(path.read_text(encoding="utf-8"))

    def set_state(self, state):
        path, data = self.read_journal()
        data["operations"][0]["state"] = state
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_planning_is_dry_run_and_edit_changes_only_plan(self):
        source = self.pdf("old.pdf")
        old = self.plan(source, "Author 2020.pdf")
        edited = self.plan(source, "Author 2021.pdf")
        self.assertEqual(old.status, "ready")
        self.assertEqual(Path(edited.destination).name, "Author 2021.pdf")
        self.assertEqual(list(self.root.iterdir()), [source])

    def test_cancel_planning_does_not_hash_remaining_files(self):
        sources = [self.pdf(f"{index}.pdf", str(index).encode()) for index in range(3)]
        hashed = []
        fingerprint = workflow.fingerprint_file

        def record_hash(path):
            hashed.append(path)
            return fingerprint(path)

        with mock.patch.object(workflow, "fingerprint_file", side_effect=record_hash):
            plans = workflow.plan_renames(
                [(str(source), f"new{index}.pdf") for index, source in enumerate(sources)],
                should_stop=lambda: bool(hashed),
            )
        self.assertEqual(hashed, [str(sources[0])])
        self.assertEqual([p.status for p in plans], ["ready", "cancelled", "cancelled"])
        self.assertTrue(all(not p.fingerprint and not p.destination for p in plans[1:]))
        self.assertTrue(all(source.exists() for source in sources))

    def test_same_name_is_skipped(self):
        source = self.pdf("old.pdf")
        plan = self.plan(source, "old.pdf")
        self.assertEqual(plan.status, "same")
        result = self.apply([plan])
        self.assertIsNone(result.batch_id)
        self.assertEqual(result.results[0].status, "skipped")
        self.assertFalse(self.journals.exists())

    def test_existing_duplicate_preserves_both_files(self):
        source, destination = self.pdf("old.pdf"), self.pdf("new.pdf")
        plan = self.plan(source, destination.name)
        self.assertEqual(plan.status, "duplicate")
        self.apply([plan])
        self.assertTrue(source.exists())
        self.assertTrue(destination.exists())

    def test_conflict_gets_reviewable_hash_suffix(self):
        source = self.pdf("old.pdf", b"first")
        occupied = self.pdf("new.pdf", b"second")
        plan = self.plan(source, occupied.name)
        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.proposed_name, "new.pdf")
        self.assertEqual(Path(plan.destination).name, f"new_{plan.fingerprint[:8]}.pdf")
        self.apply([plan])
        self.assertEqual(occupied.read_bytes(), b"second")
        self.assertEqual(Path(plan.destination).read_bytes(), b"first")

    def test_collisions_include_earlier_plans(self):
        first = self.pdf("one.pdf", b"first")
        second = self.pdf("two.pdf", b"second")
        duplicate = self.pdf("three.pdf", b"first")
        plans = workflow.plan_renames([(str(p), "new.pdf") for p in (first, second, duplicate)])
        self.assertEqual([p.status for p in plans], ["ready", "ready", "duplicate"])
        self.assertNotEqual(plans[0].destination, plans[1].destination)
        self.assertEqual(plans[0].destination, plans[2].destination)

    def test_suffix_collision_increments_without_overwriting(self):
        source = self.pdf("old.pdf", b"source")
        self.pdf("new.pdf", b"occupied")
        digest = workflow.fingerprint_file(source)
        self.pdf(f"new_{digest[:8]}.pdf", b"also occupied")
        self.assertEqual(Path(self.plan(source, "new.pdf").destination).name, f"new_{digest[:8]}_2.pdf")

    def test_invalid_names_and_extension_fail_closed(self):
        source = self.pdf("old.pdf")
        for name in ("../escape.pdf", "nested/a.pdf", r"nested\a.pdf", "file.txt", "NUL.pdf", "con.extra.pdf", "x.pdf ", "bad\x00.pdf", "a" * 252 + ".pdf", "中" * 84 + ".pdf"):
            with self.subTest(name=name):
                plan = self.plan(source, name)
                self.assertEqual(plan.status, "error")
                self.assertEqual(self.apply([plan]).results[0].status, "failed")
        self.assertEqual(list(self.root.iterdir()), [source])

    def test_suffix_keeps_unicode_filename_within_byte_limit(self):
        source = self.pdf("old.pdf", b"source")
        name = "中" * 83 + ".pdf"
        self.pdf(name, b"occupied")
        plan = self.plan(source, name)
        self.assertEqual(plan.status, "ready")
        self.assertLessEqual(len(Path(plan.destination).name.encode("utf-8")), 255)

    def test_non_pdf_and_repeated_source_are_rejected(self):
        source = self.pdf("old.txt")
        self.assertEqual(self.plan(source, "new.pdf").status, "error")
        source = self.pdf("old.pdf")
        plans = workflow.plan_renames([(str(source), "one.pdf"), (str(source), "two.pdf")])
        self.assertEqual([p.status for p in plans], ["ready", "error"])

    def test_source_changed_since_analysis_is_rejected(self):
        source = self.pdf("old.pdf")
        expected = {str(source): workflow.fingerprint_file(source)}
        source.write_bytes(b"changed")
        plan = workflow.plan_renames([(str(source), "new.pdf")], expected)[0]
        self.assertEqual(plan.status, "error")
        self.assertIn("after analysis", plan.message)

    def test_source_changed_since_review_is_not_renamed(self):
        source = self.pdf("old.pdf")
        plan = self.plan(source, "new.pdf")
        source.write_bytes(b"changed")
        result = self.apply([plan])
        self.assertEqual(result.results[0].status, "failed")
        self.assertTrue(source.exists())
        self.assertFalse(Path(plan.destination).exists())

    def test_source_rechecked_after_pending_journal_save(self):
        source = self.pdf("old.pdf")
        plan = self.plan(source, "new.pdf")
        save = workflow._save_journal

        def change_after_save(path, data):
            save(path, data)
            if data["operations"][0]["state"] == "pending":
                source.write_bytes(b"changed during persistence")

        with mock.patch.object(workflow, "_save_journal", side_effect=change_after_save):
            self.assertEqual(self.apply([plan]).results[0].status, "failed")
        self.assertTrue(source.exists())
        self.assertFalse(Path(plan.destination).exists())

    def test_destination_created_since_review_is_not_replanned(self):
        source = self.pdf("old.pdf")
        plan = self.plan(source, "new.pdf")
        destination = self.pdf("new.pdf", b"new occupant")
        self.assertEqual(self.apply([plan]).results[0].status, "failed")
        self.assertTrue(source.exists())
        self.assertEqual(destination.read_bytes(), b"new occupant")
        self.assertFalse(any(self.root.glob("new_*.pdf")))

    def test_no_clobber_primitive_rejects_racing_destination(self):
        source = self.pdf("old.pdf")
        plan = self.plan(source, "new.pdf")
        move = workflow._move_no_clobber

        def racing_move(src, dest):
            Path(dest).write_bytes(b"racing occupant")
            return move(src, dest)

        with mock.patch.object(workflow, "_move_no_clobber", side_effect=racing_move):
            self.assertEqual(self.apply([plan]).results[0].status, "failed")
        self.assertEqual(Path(plan.destination).read_bytes(), b"racing occupant")
        self.assertTrue(source.exists())

    def test_only_selected_subset_is_applied_and_undone(self):
        first, second = self.pdf("one.pdf"), self.pdf("two.pdf")
        plans = [self.plan(first, "first.pdf"), self.plan(second, "second.pdf")]
        result = self.apply(plans[1:])
        self.assertEqual(len(result.results), 1)
        self.assertTrue(first.exists())
        self.assertFalse(second.exists())
        undo = self.undo()
        self.assertEqual(undo.batch_id, result.batch_id)
        self.assertEqual(undo.results[0].status, "restored")
        self.assertTrue(second.exists())

    def test_cancel_before_apply_makes_no_journal(self):
        source = self.pdf("old.pdf")
        result = self.apply([self.plan(source, "new.pdf")], should_stop=lambda: True)
        self.assertIsNone(result.batch_id)
        self.assertEqual(result.results[0].status, "skipped")
        self.assertTrue(source.exists())
        self.assertFalse(self.journals.exists())

    def test_cancel_between_files_keeps_partial_batch_undoable(self):
        sources = [self.pdf(f"{n}.pdf", str(n).encode()) for n in range(3)]
        plans = [self.plan(source, f"new{n}.pdf") for n, source in enumerate(sources)]
        emitted = []
        result = self.apply(plans, should_stop=lambda: bool(emitted), on_result=emitted.append)
        self.assertEqual([r.status for r in result.results], ["renamed", "skipped", "skipped"])
        self.assertEqual(emitted, result.results)
        self.assertEqual([p.exists() for p in sources], [False, True, True])
        self.assertEqual(self.undo().results[0].status, "restored")

    def test_apply_and_undo_use_persistent_journal_only(self):
        source = self.pdf("old.pdf", b"original")
        applied = self.apply([self.plan(source, "new.pdf")])
        path, journal = self.read_journal()
        self.assertEqual(journal["operations"][0]["state"], "renamed")
        self.assertEqual(path.stem, applied.batch_id)
        # There is no retained in-memory batch/plan state in this call.
        undone = workflow.undo_last_batch(str(self.journals))
        self.assertEqual(undone.batch_id, applied.batch_id)
        self.assertEqual(undone.results[0].status, "restored")
        self.assertEqual(source.read_bytes(), b"original")
        self.assertIsNone(self.undo().batch_id)

    def test_undo_after_process_restart(self):
        source = self.pdf("old.pdf", b"original")
        applied = self.apply([self.plan(source, "new.pdf")])
        completed = subprocess.run(
            [sys.executable, "-c", "import sys; from paperdf_workflow import undo_last_batch; "
             "r = undo_last_batch(sys.argv[1]); print(r.batch_id); print(r.results[0].status)",
             str(self.journals)],
            cwd=Path(workflow.__file__).parent, capture_output=True, text=True, check=True,
        )
        self.assertEqual(completed.stdout.splitlines(), [applied.batch_id, "restored"])
        self.assertEqual(source.read_bytes(), b"original")

    def test_undo_does_not_clobber_occupied_original(self):
        source = self.pdf("old.pdf", b"original")
        self.apply([self.plan(source, "new.pdf")])
        source.write_bytes(b"occupied")
        result = self.undo()
        self.assertEqual(result.results[0].status, "failed")
        self.assertEqual(source.read_bytes(), b"occupied")
        self.assertEqual((self.root / "new.pdf").read_bytes(), b"original")
        source.unlink()
        self.assertEqual(self.undo().results[0].status, "restored")

    def test_changed_renamed_file_blocks_undo_until_restored(self):
        source = self.pdf("old.pdf", b"original")
        self.apply([self.plan(source, "new.pdf")])
        renamed = self.root / "new.pdf"
        renamed.write_bytes(b"edited")
        self.assertEqual(self.undo().results[0].status, "failed")
        self.assertFalse(source.exists())
        renamed.write_bytes(b"original")
        self.assertEqual(self.undo().results[0].status, "restored")

    def test_partial_undo_retries_only_unfinished_operations(self):
        first, second = self.pdf("one.pdf", b"first"), self.pdf("two.pdf", b"second")
        self.apply([self.plan(first, "first.pdf"), self.plan(second, "second.pdf")])
        first.write_bytes(b"occupied")
        result = self.undo()
        self.assertEqual([r.status for r in result.results], ["restored", "failed"])
        self.assertTrue(second.exists())
        first.unlink()
        retry = self.undo()
        self.assertEqual(len(retry.results), 1)
        self.assertEqual(retry.results[0].status, "restored")
        self.assertEqual(first.read_bytes(), b"first")

    def test_cancelled_undo_is_retryable(self):
        source = self.pdf("old.pdf")
        self.apply([self.plan(source, "new.pdf")])
        self.assertEqual(self.undo(should_stop=lambda: True).results[0].status, "skipped")
        self.assertFalse(source.exists())
        self.assertEqual(self.undo().results[0].status, "restored")

    def test_newest_unfinished_batch_is_undone_first(self):
        first = self.pdf("one.pdf", b"first")
        second = self.pdf("two.pdf", b"second")
        batch1 = self.apply([self.plan(first, "first.pdf")])
        batch2 = self.apply([self.plan(second, "second.pdf")])
        self.assertEqual(self.undo().batch_id, batch2.batch_id)
        self.assertFalse(first.exists())
        self.assertEqual(self.undo().batch_id, batch1.batch_id)

    def test_corrupt_newest_journal_does_not_undo_older_batch(self):
        source = self.pdf("old.pdf")
        self.apply([self.plan(source, "new.pdf")])
        (self.journals / "zz_corrupt.json").write_text("{truncated", encoding="utf-8")
        with self.assertRaises(workflow.WorkflowError):
            self.undo()
        self.assertFalse(source.exists())

    def test_invalid_journal_paths_fail_closed(self):
        source = self.pdf("old.pdf")
        self.apply([self.plan(source, "new.pdf")])
        path, data = self.read_journal()
        data["operations"][0]["source"] = str(self.root.parent / "outside.pdf")
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(workflow.WorkflowError):
            self.undo()

    def test_initial_journal_failure_prevents_all_moves(self):
        source = self.pdf("old.pdf")
        plan = self.plan(source, "new.pdf")
        with mock.patch.object(workflow, "_save_journal", side_effect=workflow.WorkflowError("disk full")):
            with self.assertRaises(workflow.WorkflowError):
                self.apply([plan])
        self.assertTrue(source.exists())
        self.assertFalse(Path(plan.destination).exists())

    def test_pending_journal_failure_prevents_the_move(self):
        source = self.pdf("old.pdf")
        plan = self.plan(source, "new.pdf")
        save = workflow._save_journal

        def fail_before_move(path, data):
            if data["operations"][0]["state"] == "pending":
                raise workflow.WorkflowError("disk full before rename")
            save(path, data)

        with mock.patch.object(workflow, "_save_journal", side_effect=fail_before_move):
            with self.assertRaises(workflow.WorkflowError):
                self.apply([plan])
        self.assertTrue(source.exists())
        self.assertFalse(Path(plan.destination).exists())
        self.assertIsNone(self.undo().batch_id)

    def test_undo_pending_journal_failure_prevents_the_move(self):
        source = self.pdf("old.pdf")
        self.apply([self.plan(source, "new.pdf")])
        save = workflow._save_journal

        def fail_before_undo(path, data):
            if data["operations"][0]["state"] == "undo_pending":
                raise workflow.WorkflowError("disk full before undo")
            save(path, data)

        with mock.patch.object(workflow, "_save_journal", side_effect=fail_before_undo):
            with self.assertRaises(workflow.WorkflowError):
                self.undo()
        self.assertFalse(source.exists())
        self.assertTrue((self.root / "new.pdf").exists())
        self.assertEqual(self.undo().results[0].status, "restored")

    def test_journal_write_failure_after_move_is_recoverable(self):
        source = self.pdf("old.pdf")
        plan = self.plan(source, "new.pdf")
        save = workflow._save_journal

        def fail_after_move(path, data):
            if data["operations"][0]["state"] == "renamed":
                raise workflow.WorkflowError("disk full after rename")
            save(path, data)

        with mock.patch.object(workflow, "_save_journal", side_effect=fail_after_move):
            with self.assertRaises(workflow.WorkflowError):
                self.apply([plan])
        self.assertFalse(source.exists())
        self.assertEqual(self.read_journal()[1]["operations"][0]["state"], "pending")
        self.assertEqual(self.undo().results[0].status, "restored")
        self.assertTrue(source.exists())

    def test_undo_journal_failure_after_move_is_recoverable(self):
        source = self.pdf("old.pdf")
        self.apply([self.plan(source, "new.pdf")])
        save = workflow._save_journal

        def fail_after_undo(path, data):
            if data["operations"][0]["state"] == "restored":
                raise workflow.WorkflowError("disk full after undo")
            save(path, data)

        with mock.patch.object(workflow, "_save_journal", side_effect=fail_after_undo):
            with self.assertRaises(workflow.WorkflowError):
                self.undo()
        self.assertTrue(source.exists())
        self.assertEqual(self.read_journal()[1]["operations"][0]["state"], "undo_pending")
        self.assertEqual(self.undo().results[0].status, "restored")

    def test_pending_without_a_move_is_safely_skipped(self):
        source = self.pdf("old.pdf")
        self.apply([self.plan(source, "new.pdf")])
        os.rename(self.root / "new.pdf", source)
        self.set_state("pending")
        self.assertEqual(self.undo().results[0].status, "skipped")
        self.assertTrue(source.exists())
        self.assertIsNone(self.undo().batch_id)

    def test_interrupted_hardlink_move_preserves_original(self):
        source = self.pdf("old.pdf")
        self.apply([self.plan(source, "new.pdf")])
        os.link(self.root / "new.pdf", source)
        self.set_state("pending")
        self.assertEqual(self.undo().results[0].status, "restored")
        self.assertTrue(source.exists())
        self.assertFalse((self.root / "new.pdf").exists())

    def test_atomic_journal_replace_failure_retains_valid_old_journal(self):
        source = self.pdf("old.pdf")
        self.apply([self.plan(source, "new.pdf")])
        path, data = self.read_journal()
        data["operations"][0]["state"] = "restored"
        with mock.patch.object(workflow.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(workflow.WorkflowError):
                workflow._save_journal(path, data)
        self.assertEqual(self.read_journal()[1]["operations"][0]["state"], "renamed")
        self.assertEqual(list(self.journals.glob("*.tmp")), [])

    def test_symlinks_are_rejected(self):
        source = self.pdf("old.pdf")
        link = self.root / "link.pdf"
        try:
            link.symlink_to(source)
        except OSError:
            self.skipTest("Creating symbolic links requires privileges on this Windows host.")
        self.assertEqual(self.plan(link, "new.pdf").status, "error")
        self.assertEqual(self.plan(source, link.name).status, "error")


if __name__ == "__main__":
    unittest.main()
