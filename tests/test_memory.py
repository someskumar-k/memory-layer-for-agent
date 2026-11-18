import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from unittest import mock

from membuddy.api import execute
from membuddy.cli import main
from membuddy.config import MODEL_ID, Config
from membuddy.embeddings import Embeddings, install_model, is_installed
from membuddy.history import list_candidates, scan
from membuddy.privacy import normalize_remote, project_scope, redact
from membuddy.server import identify
from membuddy.setup import SKILLS, configure, setup
from membuddy.store import Store, canonical, prune_backups, validate_note


class FakeEmbeddings:
    identity = "test-deterministic-16"

    def encode(self, texts):
        return [
            [b - 127.5 for b in hashlib.sha256(t.split("\nApplies when:")[0].encode()).digest()[:16]]
            for t in texts
        ]


class SameEmbeddings:
    identity = "test-constant-3"

    def encode(self, texts):
        return [[1.0, 0.0, 0.0] for _ in texts]


def note(text="Use spaces for indentation.", **kwargs):
    return {"kind": "preference", "text": text, "origin": "user", **kwargs}


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(self.root / "data", self.root / "model")
        self.store = Store(self.config, FakeEmbeddings())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def save(self, text="Use spaces for indentation.", source="test:session-1", **kw):
        result = self.store.remember(note(text, **kw), source)
        self.assertEqual(result["status"], "stored", result)
        return result["id"]

    def commit(self, operation, payload):
        preview = self.store.preview(operation, payload)
        self.assertEqual(preview["status"], "confirmation_required", preview)
        return self.store.commit(preview["proposal_id"], True)

    def test_persistence_across_sessions(self):
        nid = self.save()
        self.store.close()
        self.store = Store(self.config, FakeEmbeddings())
        self.assertEqual(self.store.list_notes()["notes"][0]["id"], nid)
        self.assertEqual(self.store.recall("Use spaces for indentation.")["notes"][0]["id"], nid)

    def test_exact_repeat_is_idempotent(self):
        self.assertEqual(self.save(), self.save())
        self.assertEqual(len(self.store.list_notes()["notes"]), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM contributions").fetchone()[0], 1)

    def test_archived_note_can_be_reinforced(self):
        nid = self.save()
        self.commit("archive", {"ids": [nid]})
        self.assertEqual(self.save(), nid)
        self.assertEqual(self.store.list_notes()["notes"][0]["state"], "active")

    def test_scope_and_cross_project_context(self):
        self.save(scope="repo:example.com/one", reusable=False)
        found = self.store.recall("Use spaces for indentation.", scope="repo:example.com/two")["notes"]
        self.assertTrue(found[0]["cross_project"])
        self.assertEqual(found[0]["scope"], "repo:example.com/one")

    def test_reusable_exact_lesson_promotes_without_losing_provenance(self):
        nid = self.save(source="test:a", scope="repo:example.com/one", reusable=True)
        self.assertEqual(nid, self.save(source="test:b", scope="repo:example.com/two", reusable=True))
        self.assertEqual(self.store.list_notes()["notes"][0]["scope"], "global")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM contributions").fetchone()[0], 2)

    def test_repo_conventions_do_not_promote(self):
        self.save(source="test:a", scope="repo:example.com/one")
        self.save(source="test:b", scope="repo:example.com/two")
        self.assertEqual(len(self.store.list_notes()["notes"]), 2)
        self.assertTrue(all(n["scope"] != "global" for n in self.store.list_notes()["notes"]))

    def test_user_correction_outranks_self(self):
        nid = self.save(origin="self")
        self.save(source="test:b", origin="correction")
        self.assertEqual(self.store.list_notes()["notes"][0]["origin"], "correction")
        self.assertEqual(nid, self.store.list_notes()["notes"][0]["id"])

    def test_preview_cancel_is_not_mutation(self):
        nid = self.save()
        p = self.store.preview("conversation", {"source": "test:session-1", "notes": [note("Use tabs.")]})
        self.assertEqual(p["status"], "confirmation_required")
        self.assertEqual(self.store.list_notes()["notes"][0]["id"], nid)
        with self.assertRaisesRegex(ValueError, "confirmation"):
            self.store.commit(p["proposal_id"])
        self.assertEqual(self.store.list_notes()["notes"][0]["id"], nid)

    def test_repeat_chat_replaces_own_contribution(self):
        old = self.save()
        result = self.commit("conversation", {"source": "test:session-1", "notes": [note("Use tabs.")]})
        self.assertNotEqual(result["ids"][0], old)
        self.assertEqual([n["text"] for n in self.store.list_notes()["notes"]], ["Use tabs."])
        self.assertTrue(self.store.history(old)["history"])
        self.assertTrue(Path(result["backup"]).is_file())

    def test_repeat_chat_preserves_other_sources(self):
        nid = self.save(source="test:a")
        self.save(source="test:b")
        self.commit("conversation", {"source": "test:a", "notes": [note("Use tabs.")]})
        self.assertEqual(len(self.store.list_notes()["notes"]), 2)
        sources = list(self.store.db.execute("SELECT source FROM contributions WHERE note_id=?", (nid,)))
        self.assertEqual([r[0] for r in sources], ["test:b"])

    def test_removed_source_no_longer_supplies_correction_priority(self):
        self.save(source="test:a", origin="correction")
        self.save(source="test:b", origin="self")
        self.commit("conversation", {"source": "test:a", "notes": [note("Use tabs.")]})
        old = next(n for n in self.store.list_notes()["notes"] if n["text"] == "Use spaces for indentation.")
        self.assertEqual(old["origin"], "self")

    def test_stale_preview_rejected(self):
        nid = self.save()
        p = self.store.preview("remove", {"ids": [nid]})
        self.save("Run fast tests first.")
        with self.assertRaisesRegex(ValueError, "changed since preview"):
            self.store.commit(p["proposal_id"], True)
        self.assertEqual(len(self.store.list_notes()["notes"]), 2)

    def test_remove_stops_recall_keeps_history_and_backup(self):
        nid = self.save()
        self.commit("remove", {"ids": [nid]})
        self.assertEqual(self.store.recall("Use spaces for indentation.")["notes"], [])
        self.assertEqual(len(self.store.list_notes("retired")["notes"]), 1)
        self.assertTrue(self.store.history(nid)["history"])
        self.assertTrue(list((self.config.data_dir / "backups").glob("*.db")))

    def test_scope_move(self):
        nid = self.save(scope="repo:example.com/test")
        self.commit("scope", {"ids": [nid], "scope": "global"})
        self.assertEqual(self.store.list_notes()["notes"][0]["scope"], "global")

    def test_automatic_backups_are_pruned_but_manual_ones_are_kept(self):
        for i in range(13):
            nid = self.save(f"Note {i}.", source=f"test:{i}")
            self.commit("archive", {"ids": [nid]})
        manual = Path(self.store.backup())
        backups = self.config.data_dir / "backups"
        self.assertEqual(len(list(backups.glob("auto-*.db"))), 10)
        self.assertTrue(manual.is_file())

    def test_prune_keeps_newest_per_recent_day_and_drops_older(self):
        directory = self.root / "backups"
        directory.mkdir()
        today = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)

        def make(day, hour, tag):
            path = directory / f"auto-{day}T{hour:02d}0000000000-{tag}.db"
            path.write_bytes(b"x")
            return path

        recent = [make("20261030", h, f"{h:06x}") for h in range(10)]
        older_same_day = [make("20261029", h, f"a{h:05x}") for h in (1, 2, 3)]
        ancient = make("20261001", 1, "bbbbbb")
        manual = directory / "memory-20260101T000000000000-cccccc.db"
        manual.write_bytes(b"x")
        prune_backups(directory, today=today)
        self.assertTrue(all(p.exists() for p in recent))
        self.assertEqual([p.exists() for p in older_same_day], [False, False, True])
        self.assertFalse(ancient.exists())
        self.assertTrue(manual.exists())

    def test_backup_is_portable_and_complete(self):
        nid = self.save()
        backup = self.store.backup()
        db = sqlite3.connect(backup)
        try:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT id FROM notes").fetchone()[0], nid)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM contributions").fetchone()[0], 1)
        finally:
            db.close()

    def test_feedback_only_successful_unique_events_count(self):
        nid = self.save()
        self.store.recall("Use spaces for indentation.")
        self.assertEqual(self.store.list_notes()["notes"][0]["successes"], 0)
        self.store.feedback(nid, "action:1", True)
        self.store.feedback(nid, "action:1", True)
        self.store.feedback(nid, "action:2", False)
        self.assertEqual(self.store.list_notes()["notes"][0]["successes"], 1)

    def test_budget_is_measured_not_estimated(self):
        self.save()
        result = self.store.recall("Use spaces for indentation.", budget_chars=256)
        self.assertLessEqual(len(canonical(result["notes"])), 256)
        with self.assertRaises(ValueError):
            self.store.recall("x", budget_chars=50000)

    def test_usage_feedback_does_not_invalidate_a_pending_preview(self):
        a = self.save("Use spaces for indentation.", source="test:a")
        b = self.save("Run fast tests first.", source="test:b")
        preview = self.store.preview("remove", {"ids": [a]})
        self.store.feedback(b, "action:1", True)
        self.assertEqual(self.store.commit(preview["proposal_id"], True)["status"], "committed")

    def test_startup_reserves_slots_for_relevant_lessons(self):
        for i in range(7):
            self.save(f"Preference number {i}.", source=f"test:p{i}")
        lesson = "Windows PowerShell 5 rejects && command chaining."
        stored = self.store.remember({"kind": "mistake", "text": lesson, "origin": "self"}, "test:m")
        self.assertEqual(stored["status"], "stored", stored)
        kinds = [n["kind"] for n in self.store.recall(lesson, startup=True)["notes"]]
        self.assertIn("mistake", kinds)
        self.assertLessEqual(len(kinds), 5)

    def test_startup_without_relevant_lessons_fills_slots_with_preferences(self):
        for i in range(7):
            self.save(f"Preference number {i}.", source=f"test:p{i}")
        self.assertEqual(len(self.store.recall(startup=True)["notes"]), 5)

    def test_startup_has_standing_preferences_without_query(self):
        self.save()
        self.assertEqual(len(self.store.recall(startup=True)["notes"]), 1)
        with self.assertRaises(ValueError):
            self.store.recall()

    def test_secret_redaction_before_persistence(self):
        self.save("Never log password=secret123 or api_key='keep-out'.\nAvoid ghp_123456789xyz too.")
        raw = self.store.db.execute("SELECT text FROM notes").fetchone()[0]
        self.assertNotIn("secret123", raw)
        self.assertNotIn("keep-out", raw)
        self.assertNotIn("ghp_123456789xyz", raw)
        contribution = self.store.db.execute("SELECT original FROM contributions").fetchone()[0]
        self.assertNotIn("keep-out", contribution)

    def test_note_validation(self):
        for n in [note(""), note("a\n" * 7), note(kind="unknown"), note(reusable="yes"),
                  note(conditions="x\nx"), note(scope="random"), {"kind": "context", "text": "x", "extra": 1}]:
            with self.subTest(n=n), self.assertRaises(ValueError):
                validate_note(n)

    def test_identity_mismatch_fails_loudly(self):
        class Wrong(FakeEmbeddings):
            identity = "wrong"
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            Store(self.config, Wrong())

    def test_missing_model_never_downloads(self):
        with self.assertRaisesRegex(ValueError, "not installed"):
            Embeddings(self.root / "missing").encode(["hello"])

    def test_concurrent_local_writers(self):
        def writer(i):
            store = Store(self.config, FakeEmbeddings())
            try:
                return store.remember(note(), f"test:parallel-{i}")
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(writer, range(8)))
        self.assertTrue(all(r["status"] == "stored" for r in results))
        self.assertEqual(len(self.store.list_notes()["notes"]), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM contributions").fetchone()[0], 8)

    def test_optimize_includes_archived_and_preserves_history_sources(self):
        a = self.save("Use spaces for indentation.", source="test:a")
        b = self.save("Use four spaces.", source="test:b")
        self.commit("archive", {"ids": [b]})
        result = self.commit("optimize", {"groups": [
            {"ids": [a, b], "note": note("Indent using four spaces.")}
        ]})
        self.assertEqual(len(self.store.list_notes()["notes"]), 1)
        self.assertEqual(self.store.list_notes()["notes"][0]["text"], "Indent using four spaces.")
        self.assertTrue(self.store.history(a)["history"])
        self.assertTrue(self.store.history(b)["history"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM contributions").fetchone()[0], 2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 1)
        self.assertTrue(Path(result["backup"]).is_file())

    def test_chat_replacement_after_merge_removes_only_that_sources_knowledge(self):
        a = self.save("Prefer tea.", source="test:a")
        b = self.save("Prefer coffee for breakfast.", source="test:b")
        c = self.save("Prefer water at lunch.", source="test:b")
        self.commit("optimize", {"groups": [{
            "ids": [a, b, c], "note": note("Prefer tea, breakfast coffee and lunch water.")
        }]})
        self.commit("conversation", {"source": "test:a", "notes": [note("Prefer cocoa.")]})
        texts = {n["text"] for n in self.store.list_notes()["notes"]}
        self.assertEqual(texts, {"Prefer cocoa.", "Prefer coffee for breakfast.", "Prefer water at lunch."})

    def test_partial_replacement_preserves_archive_state_of_merged_remainder(self):
        a = self.save("Prefer tea.", source="test:a")
        b = self.save("Prefer coffee for breakfast.", source="test:b")
        merged = self.commit("optimize", {"groups": [{
            "ids": [a, b], "note": note("Prefer tea and breakfast coffee.")
        }]})["ids"][0]
        self.commit("archive", {"ids": [merged]})
        self.commit("conversation", {"source": "test:a", "notes": [note("Prefer cocoa.")]})
        self.assertEqual([n["text"] for n in self.store.list_notes()["notes"]], ["Prefer cocoa."])
        self.assertEqual([n["text"] for n in self.store.list_notes("archived")["notes"]], ["Prefer coffee for breakfast."])

    def test_explicit_supersession_does_not_restore_replaced_merged_knowledge(self):
        a = self.save("Prefer tea.", source="test:a")
        b = self.save("Prefer coffee for breakfast.", source="test:b")
        merged = self.commit("optimize", {"groups": [{
            "ids": [a, b], "note": note("Prefer tea and breakfast coffee.")
        }]})["ids"][0]
        self.commit("conversation", {"source": "test:c", "notes": [
            note("Prefer cocoa.", related=[{"id": merged, "decision": "replace"}])
        ]})
        self.assertEqual([n["text"] for n in self.store.list_notes()["notes"]], ["Prefer cocoa."])

    def test_optimization_group_cannot_overlap(self):
        a = self.save()
        b = self.save("Use four spaces.")
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.store.preview("optimize", {"groups": [
                {"ids": [a, b], "note": note("Merged.")},
                {"ids": [a, b], "note": note("Merged again.")},
            ]})

    def test_proposal_single_use(self):
        nid = self.save()
        p = self.store.preview("archive", {"ids": [nid]})
        self.store.commit(p["proposal_id"], True)
        with self.assertRaisesRegex(ValueError, "consumed"):
            self.store.commit(p["proposal_id"], True)

    def test_failed_merge_rolls_back(self):
        a = self.save(source="test:a")
        b = self.save("Use four spaces.", source="test:b")
        self.commit("remove", {"ids": [b]})
        p = self.store.preview("optimize", {"groups": [{"ids": [a, b], "note": note("Merged.")}]})
        with self.assertRaisesRegex(ValueError, "retired"):
            self.store.commit(p["proposal_id"], True)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 2)

    def test_failure_after_first_merge_rolls_back_all_groups(self):
        ids = [self.save(t, source=f"test:{i}") for i, t in enumerate(
            ("Tea.", "Coffee.", "Milk.", "Water."))]
        self.commit("remove", {"ids": [ids[-1]]})
        p = self.store.preview("optimize", {"groups": [
            {"ids": ids[:2], "note": note("Warm drinks.")},
            {"ids": ids[2:], "note": note("Cold drinks.")},
        ]})
        with self.assertRaisesRegex(ValueError, "retired"):
            self.store.commit(p["proposal_id"], True)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 4)
        self.assertEqual({n["text"] for n in self.store.list_notes()["notes"]}, {"Tea.", "Coffee.", "Milk."})

    def test_compaction_failure_explicitly_reports_already_committed_save(self):
        a = self.save("Tea.", source="test:a")
        b = self.save("Coffee.", source="test:b")
        p = self.store.preview("optimize", {"groups": [{"ids": [a, b], "note": note("Warm drinks.")}]})
        connection = self.store.db

        class FailVacuum:
            def __getattr__(self, attr):
                return getattr(connection, attr)

            def execute(self, sql, *args):
                if sql == "VACUUM":
                    raise sqlite3.OperationalError("locked for test")
                return connection.execute(sql, *args)

        self.store.db = FailVacuum()
        try:
            with self.assertRaisesRegex(ValueError, "Notes were committed.*compaction failed"):
                self.store.commit(p["proposal_id"], True)
        finally:
            self.store.db = connection
        self.assertEqual([n["text"] for n in self.store.list_notes()["notes"]], ["Warm drinks."])

    def test_empty_conversation_cannot_erase(self):
        with self.assertRaises(ValueError):
            self.store.preview("conversation", {"source": "test:a", "notes": []})

    def test_history_fallback_does_not_create_authoritative_memory(self):
        path = self.root / "sessions.db"
        src = sqlite3.connect(path)
        src.execute("CREATE TABLE turns(id INTEGER,session_id TEXT,turn_index INTEGER,user_message TEXT,assistant_response TEXT)")
        src.execute("INSERT INTO turns VALUES(1,'old',0,?,?)", ("No, wrong: never log password=abc123", "Fixed."))
        src.execute("INSERT INTO turns VALUES(2,'key',1,?,?)", (
            "Never store this -----BEGIN PRIVATE KEY-----\n" + "PRIVATE_DATA" * 100 + "\n-----END PRIVATE KEY-----", "OK"))
        src.commit()
        src.close()
        config = Config(self.config.data_dir, self.config.model_dir, history_store=path)
        store = Store(config, FakeEmbeddings())
        try:
            self.assertEqual(scan(store)["candidates"], 2)
            texts = str(list_candidates(store))
        finally:
            store.close()
        self.assertNotIn("abc123", texts)
        self.assertNotIn("PRIVATE_DATA", texts)
        self.assertEqual(self.store.list_notes()["notes"], [])

    def test_history_scan_cannot_be_pointed_at_an_arbitrary_file(self):
        other = self.root / "other.db"
        sqlite3.connect(other).close()
        with self.assertRaisesRegex(ValueError, "takes only 'limit'"):
            execute(self.store, "scan-history", {"path": str(other)})

    def test_history_scan_reports_a_missing_configured_store(self):
        config = Config(self.config.data_dir, self.config.model_dir, history_store=self.root / "absent.db")
        store = Store(config, FakeEmbeddings())
        try:
            with self.assertRaisesRegex(ValueError, "History store not found"):
                scan(store)
        finally:
            store.close()


class FakeModel:
    """Stands in for SentenceTransformer so install behavior is tested without a download."""
    fail_on_save = False
    downloads = 0

    def __init__(self, source, **kwargs):
        if source == MODEL_ID:
            FakeModel.downloads += 1
        else:
            assert Path(source).is_dir()

    def save(self, directory):
        if FakeModel.fail_on_save:
            Path(directory, "weights.partial").write_text("half")
            raise OSError("disk full")
        Path(directory, "weights.bin").write_text("weights")

    def get_embedding_dimension(self):
        return 384


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.model_dir = Path(self.temp.name) / "model"
        FakeModel.fail_on_save, FakeModel.downloads = False, 0
        patcher = mock.patch.dict(sys.modules, {
            "sentence_transformers": types.SimpleNamespace(SentenceTransformer=FakeModel)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)

    def leftovers(self):
        return [p.name for p in Path(self.temp.name).glob("model.partial-*")]

    def test_interrupted_install_leaves_nothing_behind_and_can_be_retried(self):
        FakeModel.fail_on_save = True
        with self.assertRaises(OSError):
            install_model(self.model_dir)
        self.assertFalse(self.model_dir.exists())
        self.assertEqual(self.leftovers(), [])
        FakeModel.fail_on_save = False
        install_model(self.model_dir)
        self.assertTrue(is_installed(self.model_dir))
        self.assertTrue((self.model_dir / "weights.bin").is_file())
        self.assertEqual(self.leftovers(), [])

    def test_complete_install_is_not_repeated(self):
        install_model(self.model_dir)
        install_model(self.model_dir)
        self.assertEqual(FakeModel.downloads, 1)

    def test_foreign_folder_is_refused_and_left_untouched(self):
        self.model_dir.mkdir()
        (self.model_dir / "mine.txt").write_text("keep")
        with self.assertRaisesRegex(ValueError, "not a complete MemBuddy model install"):
            install_model(self.model_dir)
        self.assertEqual((self.model_dir / "mine.txt").read_text(), "keep")
        self.assertEqual(FakeModel.downloads, 0)

    def test_existing_empty_folder_is_used(self):
        self.model_dir.mkdir()
        install_model(self.model_dir)
        self.assertTrue(is_installed(self.model_dir))

    def test_corrupt_marker_means_not_installed(self):
        self.model_dir.mkdir()
        (self.model_dir / "membuddy-model.json").write_text("{broken")
        self.assertFalse(is_installed(self.model_dir))


class RelatedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = Store(Config(root / "db", root / "model"), SameEmbeddings())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_similarity_requires_judgment_not_auto_overwrite(self):
        a = self.store.remember(note("Use tabs."), "test:a")["id"]
        result = self.store.remember(note("Use spaces."), "test:b")
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(len(self.store.list_notes()["notes"]), 1)
        saved = self.store.remember(note("Use spaces.", related=[{"id": a, "decision": "distinct"}]), "test:b")
        self.assertEqual(saved["status"], "stored")

    def test_opposition_replacement_requires_confirmed_preview(self):
        a = self.store.remember(note("Use tabs."), "test:a")["id"]
        replacement = note("Use spaces.", related=[{"id": a, "decision": "replace"}])
        with self.assertRaisesRegex(ValueError, "confirmed preview"):
            self.store.remember(replacement, "test:b")
        p = self.store.preview("conversation", {"source": "test:b", "notes": [replacement]})
        self.store.commit(p["proposal_id"], True)
        self.assertEqual([n["text"] for n in self.store.list_notes()["notes"]], ["Use spaces."])
        self.assertTrue(self.store.history(a)["history"])

    def test_noise_triggers_safe_cleanup(self):
        n = self.store.remember(note("Use tabs."), "test:a")["id"]
        # Simulate an older database with exact duplicates, not opposing instructions.
        self.store.db.execute(
            "INSERT INTO notes(kind,text,conditions,scope,origin,reusable,fingerprint,vector,created,updated) "
            "SELECT kind,text,conditions,scope,origin,reusable,fingerprint,vector,created,updated FROM notes WHERE id=?", (n,))
        self.store.db.execute(
            "INSERT INTO contributions SELECT last_insert_rowid(),'test:b',original FROM contributions WHERE note_id=?", (n,))
        result = self.store.recall("tabs")
        self.assertTrue(result["maintenance"]["optimized"])
        self.assertEqual(len(result["notes"]), 1)
        self.assertEqual(len(self.store.list_notes()["notes"]), 1)
        self.assertEqual(len(self.store.list_notes("archived")["notes"]), 1)


class SetupTests(unittest.TestCase):
    def test_bundled_skill_metadata_matches_directory_names(self):
        for name in SKILLS:
            body = files("membuddy").joinpath("assets", name, "SKILL.md").read_text(encoding="utf-8")
            self.assertTrue(body.startswith("---\n"))
            frontmatter = body.split("---", 2)[1]
            self.assertIn(f"name: {name}\n", frontmatter)
            self.assertIn('description: "', frontmatter)
            self.assertLess(len(body.splitlines()), 500)

    def test_config_paths(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text(json.dumps({"data_dir": "local", "history_store": "chats.db"}))
            c = Config.load(p)
            self.assertEqual(c.data_dir, Path(d) / "local")
            self.assertEqual(c.model_dir, Path(d) / "local" / "model")
            self.assertEqual(c.history_store, Path(d) / "chats.db")
            p.write_text("{}")
            self.assertEqual(Config.load(p).history_store, Path.home() / ".copilot" / "session-store.db")
            p.write_text('{"unexpected": 1}')
            with self.assertRaises(ValueError):
                Config.load(p)

    def test_setup_configure_both_clients_idempotent_preserves_existing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            config = root / "config.json"
            setup(config, root / "data")
            cli = root / ".copilot" / "mcp-config.json"
            cli.parent.mkdir()
            cli.write_text('{"mcpServers":{"other":{"command":"other"}}}')
            instr = cli.parent / "copilot-instructions.md"
            instr.write_text("Existing user instructions.\n")
            for client in ("cli", "vscode"):
                result = configure(config, client, root)
                self.assertTrue(Path(result["mcp_config"]).is_file())
                self.assertTrue((Path(result["skills"]) / "membuddy" / "SKILL.md").is_file())
            configure(config, "cli", root)
            self.assertIn("other", json.loads(cli.read_text())["mcpServers"])
            self.assertEqual(instr.read_text().count("<!-- membuddy:start -->"), 1)
            self.assertIn("Existing user instructions.", instr.read_text())
            self.assertTrue(list(cli.parent.glob("*.bak")))

    def test_cli_errors_are_nonzero_and_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                code = main(["--config", str(Path(d) / "config.json"), "recall", "{broken"])
            self.assertEqual(code, 1)
            self.assertIn("error", err.getvalue())

    def test_malformed_instruction_block_does_not_partly_configure(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            config = root / "config.json"
            setup(config, root / "data")
            instruction = root / ".copilot" / "copilot-instructions.md"
            instruction.parent.mkdir()
            instruction.write_text("<!-- membuddy:start -->\nMissing end marker.")
            with self.assertRaisesRegex(ValueError, "Malformed"):
                configure(config, "cli", root)
            self.assertFalse((instruction.parent / "mcp-config.json").exists())

    def test_project_identity(self):
        expected = "repo:github.com/Owner/Project"
        self.assertEqual(normalize_remote("git@github.com:Owner/Project.git"), expected)
        self.assertEqual(normalize_remote("https://user:pass@GitHub.com/Owner/Project.git?token=abc"), expected)
        self.assertEqual(project_scope(remote="ssh://git@github.com/Owner/Project.git"), expected)
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(project_scope(d), "folder:" + os.path.normcase(str(Path(d).resolve())))

    @unittest.skipUnless(os.name == "nt", "Windows paths are case-insensitive")
    def test_folder_scope_ignores_windows_path_case(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(project_scope(d.upper()), project_scope(d.lower()))

    def test_project_folder_must_exist(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError, "does not exist"):
                project_scope(str(Path(d) / "missing"))

    def test_mcp_identity_requires_an_explicit_absolute_folder(self):
        for folder in (None, "", "relative\\folder"):
            with self.subTest(folder=folder), self.assertRaisesRegex(ValueError, "working directory"):
                identify(folder)
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(identify(d), project_scope(d))
        self.assertEqual(identify(remote="git@example.com:o/r.git"), "repo:example.com/o/r")

    def test_redact_common_secret_formats(self):
        examples = [
            ('{"JIRA_PAT": "top-secret"}', "top-secret"),
            ("Authorization: Bearer abcdef123", "abcdef123"),
            ("https://bob:password@example.com", "bob:password"),
            ("token=confidential", "confidential"),
            ("-----BEGIN PRIVATE KEY-----\nmykey\n-----END PRIVATE KEY-----", "mykey"),
        ]
        for text, secret in examples:
            self.assertNotIn(secret, redact(text))


if __name__ == "__main__":
    unittest.main()
