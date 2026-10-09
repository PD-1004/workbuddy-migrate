"""Skill migration regressions against modules from the built EXE."""
import json
import pathlib
import shutil
import threading
import time
import types
import sys
import marshal
import urllib.request
from http.server import ThreadingHTTPServer
import unittest
from unittest import mock

import test_repairs as repairs
from test_repairs import connect, write


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.fixture = repairs.MigrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.core = self.fixture.core
        self.source = self.fixture.sc / "skills"
        self.target = self.fixture.dc / "skills"
        write(self.source / "alpha" / "SKILL.md", '---\nname: Alpha\ndescription: "Alpha description"\n---\nnew skill\n')
        write(self.source / "alpha" / "assets" / "template.txt", "required resource")
        write(self.source / "beta" / "SKILL.md", "---\ndescription: Beta description\n---\nbeta")
        write(self.target / "alpha" / "SKILL.md", "old skill")
        write(self.target / "alpha" / "obsolete.txt", "old-only file")
        write(self.fixture.sc / "blobs" / "image.bin", "unchanged blob")

    def pack(self, mode="all", names=None):
        self.fixture.round += 1
        result = self.core["collect_source"](str(self.fixture.sc), str(self.fixture.sw),
            str(self.fixture.root / ("round-%s" % self.fixture.round)),
            session_ids=["session-a"], skill_mode=mode, skill_names=names)
        self.assertTrue(result["ok"], result)
        return pathlib.Path(result["pkg"])

    def apply(self, pkg, actions=None):
        return self.core["apply_migrate"](str(pkg), self.fixture.env,
            target_uid="target-user", skill_actions=actions)

    def test_inventory_lists_real_skills_descriptions_and_sizes(self):
        items = self.core["list_skills"](str(self.fixture.sc))
        self.assertEqual([item["name"] for item in items], ["alpha", "beta"])
        self.assertEqual(items[0]["description"], "Alpha description")
        self.assertEqual(items[0]["files"], 2)
        self.assertGreater(items[0]["bytes"], 0)

    def test_none_omits_only_skills_and_records_selection(self):
        pkg = self.pack("none")
        self.assertFalse((pkg / "缓存目录" / "skills").exists())
        self.assertEqual((pkg / "缓存目录" / "blobs" / "image.bin").read_text(), "unchanged blob")
        manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["skill_selection"]["mode"], "none")

    def test_custom_keeps_entire_selected_skill_and_omits_others(self):
        pkg = self.pack("custom", ["alpha"])
        skills = pkg / "缓存目录" / "skills"
        self.assertEqual((skills / "alpha" / "assets" / "template.txt").read_text(), "required resource")
        self.assertFalse((skills / "beta").exists())

    def test_all_and_legacy_default_keep_all_skills(self):
        for pkg in (self.pack(), pathlib.Path(self.fixture.pack())):
            self.assertTrue((pkg / "缓存目录" / "skills" / "alpha" / "SKILL.md").is_file())
            self.assertTrue((pkg / "缓存目录" / "skills" / "beta" / "SKILL.md").is_file())

    def test_empty_custom_selection_is_valid(self):
        self.assertFalse((self.pack("custom", []) / "缓存目录" / "skills").exists())

    def test_sequential_pack_choices_do_not_leak(self):
        self.pack("none")
        self.assertTrue((self.pack("custom", ["beta"]) / "缓存目录" / "skills" / "beta").is_dir())
        self.assertTrue((self.pack("all") / "缓存目录" / "skills" / "alpha").is_dir())

    def test_invalid_skill_name_is_rejected_before_pack(self):
        result = self.core["collect_source"](str(self.fixture.sc), str(self.fixture.sw),
            str(self.fixture.root / "invalid"), skill_mode="custom", skill_names=["../alpha"])
        self.assertFalse(result["ok"], result)
        self.assertFalse((self.fixture.root / "invalid").exists())

    def test_check_distinguishes_new_same_and_conflicting_skills(self):
        pkg = self.pack()
        shutil.copytree(self.source / "beta", self.target / "beta")
        write(self.source / "gamma" / "SKILL.md", "gamma")
        pkg = self.pack()
        result = self.core["check_migrate"](str(pkg), self.fixture.env)
        self.assertTrue(result["ok"], result)
        self.assertEqual({x["name"]: x["status"] for x in result["skills"]},
            {"alpha": "conflict", "beta": "same", "gamma": "new"})

    def test_default_skip_keeps_local_skill_and_imports_new_skill(self):
        result = self.apply(self.pack())
        self.assertTrue(result["ok"], result)
        self.assertEqual((self.target / "alpha" / "SKILL.md").read_text(), "old skill")
        self.assertTrue((self.target / "beta" / "SKILL.md").exists())

    def test_overwrite_replaces_whole_directory_and_backs_up_old_skill(self):
        result = self.apply(self.pack(), {"alpha": "overwrite"})
        self.assertTrue(result["ok"], result)
        self.assertFalse((self.target / "alpha" / "obsolete.txt").exists())
        self.assertEqual((self.target / "alpha" / "assets" / "template.txt").read_text(), "required resource")
        backup = pathlib.Path(result["backup_dir"]) / "缓存目录" / "skills" / "alpha"
        self.assertEqual((backup / "SKILL.md").read_text(), "old skill")
        self.assertEqual((backup / "obsolete.txt").read_text(), "old-only file")

    def test_identical_skill_is_skipped_even_if_overwrite_requested(self):
        shutil.rmtree(self.target / "alpha")
        shutil.copytree(self.source / "alpha", self.target / "alpha")
        old_time = (self.target / "alpha" / "SKILL.md").stat().st_mtime_ns
        result = self.apply(self.pack(), {"alpha": "overwrite"})
        self.assertTrue(result["ok"], result)
        self.assertEqual((self.target / "alpha" / "SKILL.md").stat().st_mtime_ns, old_time)
        self.assertEqual(result["n_skill"], 1)

    def test_overwrite_works_when_sessions_already_migrated(self):
        pkg = self.pack()
        self.assertTrue(self.apply(pkg)["ok"])
        result = self.apply(pkg, {"alpha": "overwrite"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["n_skill"], 1)
        self.assertFalse((self.target / "alpha" / "obsolete.txt").exists())

    def test_backup_failure_leaves_database_and_skill_untouched(self):
        pkg = self.pack()
        real_copytree = shutil.copytree
        def fail_backup(src, dst, *args, **kwargs):
            if pathlib.Path(src) == self.target / "alpha":
                raise OSError("synthetic backup failure")
            return real_copytree(src, dst, *args, **kwargs)
        with mock.patch.object(shutil, "copytree", side_effect=fail_backup):
            result = self.apply(pkg, {"alpha": "overwrite"})
        self.assertFalse(result["ok"], result)
        self.assertEqual((self.target / "alpha" / "SKILL.md").read_text(), "old skill")
        with connect(self.fixture.dc / "workbuddy.db") as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions WHERE id='session-a'").fetchone()[0], 0)

    def test_failed_skill_copy_restores_previous_overwrite_and_new_skills(self):
        pkg = self.pack()
        real_copytree = shutil.copytree
        def fail_beta(src, dst, *args, **kwargs):
            if pathlib.Path(src).name == "beta" and ".wb-skill-" in str(dst):
                raise OSError("synthetic skill copy failure")
            return real_copytree(src, dst, *args, **kwargs)
        with mock.patch.object(shutil, "copytree", side_effect=fail_beta):
            result = self.apply(pkg, {"alpha": "overwrite"})
        self.assertFalse(result["ok"], result)
        self.assertEqual((self.target / "alpha" / "SKILL.md").read_text(), "old skill")
        self.assertTrue((self.target / "alpha" / "obsolete.txt").exists())
        self.assertFalse((self.target / "beta").exists())
        with connect(self.fixture.dc / "workbuddy.db") as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions WHERE id='session-a'").fetchone()[0], 0)

    def test_invalid_action_is_rejected_before_migration(self):
        result = self.apply(self.pack(), {"alpha": "delete"})
        self.assertFalse(result["ok"], result)
        with connect(self.fixture.dc / "workbuddy.db") as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions WHERE id='session-a'").fetchone()[0], 0)

    def test_same_second_pack_never_reuses_previous_skill_selection(self):
        dest = str(self.fixture.root / "same-destination")
        with mock.patch.object(self.core["time"], "strftime", return_value="fixed-time"):
            first = self.core["collect_source"](str(self.fixture.sc), str(self.fixture.sw), dest,
                session_ids=["session-a"], skill_mode="all")
            second = self.core["collect_source"](str(self.fixture.sc), str(self.fixture.sw), dest,
                session_ids=["session-a"], skill_mode="none")
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertNotEqual(first["pkg"], second["pkg"])
        self.assertFalse((pathlib.Path(second["pkg"]) / "缓存目录" / "skills").exists())

    def test_http_check_returns_serializable_skill_decisions(self):
        pkg = self.pack()
        class Handler:
            path = "/api/check"
            def _do_post(self):
                raise AssertionError("skill route must use the new handler")
            def _body(self):
                return {"pkg": str(pkg)}
            def _json(self, value):
                return json.loads(json.dumps(value))
        web = {"Handler": Handler, "JOB_LOCK": threading.Lock(), "LAST_BEAT": [0], "time": time,
            "BE": types.SimpleNamespace(env=self.fixture.env), "core": types.SimpleNamespace(**self.core),
            "LOG": types.SimpleNamespace(add=lambda _: None)}
        sys.modules["wb_skills"].install_web(web)
        result = Handler()._do_post()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["todo"], 1)
        self.assertEqual(result["skills"][0]["status"], "conflict")
        self.assertFalse(web["JOB_LOCK"].locked())

    def test_none_and_empty_custom_do_not_read_unselected_skills(self):
        with mock.patch.object(sys.modules["wb_skills"], "list_skills", side_effect=OSError("unreadable unselected skill")):
            self.pack("none")
            self.pack("custom", [])

    def test_skill_pack_copy_failure_is_not_reported_as_success(self):
        real_copytree = shutil.copytree
        def fail_alpha(src, dst, *args, **kwargs):
            if pathlib.Path(src) == self.source / "alpha":
                raise OSError("synthetic skill export failure")
            return real_copytree(src, dst, *args, **kwargs)
        with mock.patch.object(shutil, "copytree", side_effect=fail_alpha):
            result = self.core["collect_source"](str(self.fixture.sc), str(self.fixture.sw),
                str(self.fixture.root / "failed-export"), skill_mode="custom", skill_names=["alpha"])
        self.assertFalse(result["ok"], result)
        self.assertIn("synthetic skill export failure", result["text"])

    def test_real_http_skill_selection_check_and_overwrite(self):
        core_module = types.ModuleType("wb_core")
        core_module.__dict__.update(self.core)
        web = {"__name__": "wb_web_skill_tests", "__file__": str(self.fixture.root / "wb_web.py")}
        with mock.patch.dict(sys.modules, {"wb_core": core_module}):
            exec(marshal.loads(repairs.unpack(repairs.entries["wb_web"])), web)
        web["BE"].env = self.core["LocalEnv"](str(self.fixture.sc), str(self.fixture.sw))
        server = ThreadingHTTPServer(("127.0.0.1", 0), web["Handler"])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(route, payload):
            req = urllib.request.Request("http://127.0.0.1:%s/api/%s" % (server.server_port, route),
                data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as response:
                return json.load(response)
        try:
            inventory = request("skills", {})
            self.assertTrue(inventory["ok"], inventory)
            result = request("collect", {"dest": str(self.fixture.root / "http-pack"),
                "session_ids": ["session-a"], "skill_mode": "custom", "skill_names": ["alpha"]})
            self.assertTrue(result["ok"], result)
            pkg = result["pkg"]
            self.assertFalse((pathlib.Path(pkg) / "缓存目录" / "skills" / "beta").exists())
            web["BE"].env = self.fixture.env
            checked = request("check", {"pkg": pkg, "target_uid": "target-user"})
            self.assertTrue(checked["ok"], checked)
            self.assertEqual(checked["skills"][0]["status"], "conflict")
            migrated = request("apply", {"pkg": pkg, "target_uid": "target-user", "skill_actions": {"alpha": "overwrite"}})
            self.assertTrue(migrated["ok"], migrated)
            self.assertFalse((self.target / "alpha" / "obsolete.txt").exists())
            self.assertFalse((self.target / "beta").exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
