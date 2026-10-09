"""Regression tests run against original or repaired EXE modules."""
import contextlib
import json
import marshal
import os
import pathlib
import sqlite3
import sys
import struct
import tempfile
import threading
import types
import unittest
from unittest import mock
import urllib.request
import zlib

ROOT = pathlib.Path(__file__).absolute().parent.parent
ORIGINAL = "--original" in sys.argv
if ORIGINAL:
    sys.argv.remove("--original")
MODULE_DIR = ROOT / "audit" if ORIGINAL else ROOT / "repair" / "build"

if not ORIGINAL:
    from build_repaired import archive, unpack
    packed_exe = pathlib.Path(json.loads((MODULE_DIR / "build_report.json").read_text(encoding="utf-8"))["output"]).read_bytes()
    _, _, _, entries = archive(packed_exe)
    entries = {entry["name"]: entry for entry in entries}
    pyz = unpack(entries["PYZ.pyz"])
    toc = dict(marshal.loads(pyz[struct.unpack("!I", pyz[8:12])[0]:]))

    def packed_code(name):
        _, position, length = toc[name]
        return marshal.loads(zlib.decompress(pyz[position:position + length]))

    helper = types.ModuleType("wb_fixes")
    sys.modules["wb_fixes"] = helper
    exec(packed_code("wb_fixes"), helper.__dict__)
    if "wb_skills" in toc:
        skills = types.ModuleType("wb_skills")
        sys.modules["wb_skills"] = skills
        exec(packed_code("wb_skills"), skills.__dict__)
    updates = types.ModuleType("wb_updates")
    sys.modules["wb_updates"] = updates
    exec(packed_code("wb_updates"), updates.__dict__)


def load_core():
    ns = {"__name__": "wb_core_tests"}
    code = marshal.loads((MODULE_DIR / "wb_core.bin").read_bytes()) if ORIGINAL else packed_code("wb_core")
    exec(code, ns)
    return ns


@contextlib.contextmanager
def connect(path):
    conn = sqlite3.connect(str(path))
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(ROOT / "repair"))
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.core = load_core()
        self.sc = self.root / "source" / "cache"
        self.sw = self.root / "source" / "workspace"
        self.dc = self.root / "target" / "cache"
        self.dw = self.root / "target" / "workspace"
        self.cwd = self.sw / "project-a"
        self.target = self.dw / "project-a"
        self.round = 0
        for cache, ws, rows in [
            (self.sc, self.sw, [("session-a", "source-user", str(self.cwd), "v1", None, 100, None), ("session-b", "source-user", str(self.sw / "project-b"), "other", None, 100, None)]),
            (self.dc, self.dw, [("seed", "target-user", str(self.dw / "seed"), "seed", None, 50, None)]),
        ]:
            cache.mkdir(parents=True)
            ws.mkdir(parents=True)
            with connect(cache / "workbuddy.db") as conn:
                conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT, cwd TEXT, title TEXT, custom_title TEXT, updated_at INTEGER, deleted_at INTEGER, local_only TEXT DEFAULT 'keep-me')")
                conn.execute("CREATE TABLE workspaces (path TEXT PRIMARY KEY, last_opened_at INTEGER)")
                conn.executemany("INSERT INTO sessions (id,user_id,cwd,title,custom_title,updated_at,deleted_at) VALUES (?,?,?,?,?,?,?)", rows)
        # The destination-only column must survive an update.
        with connect(self.sc / "workbuddy.db") as conn:
            conn.execute("ALTER TABLE sessions DROP COLUMN local_only")
        self.body = self.sc / "projects" / self.core["cwd_to_slug"](str(self.cwd)) / "session-a.jsonl"
        self.target_body = self.dc / "projects" / self.core["cwd_to_slug"](str(self.target)) / "session-a.jsonl"
        write(self.body, '{"message":"old"}\n')
        write(self.sc / "artifact-index" / "session-a.json", '{"version":1}')
        write(self.sc / "changes-detail" / "session-a" / "edits.jsonl", '{"version":1}\n')
        write(self.cwd / "result.txt", "AAAA")
        write(self.sw / "project-b" / "result.txt", "unselected")
        self.env = self.core["LocalEnv"](str(self.dc), str(self.dw))

    def pack(self, mode="selected", ids=None):
        self.round += 1
        kw = {} if mode == "all" else {"uids": ["source-user"], "session_ids": ids or ["session-a"]}
        result = self.core["collect_source"](str(self.sc), str(self.sw), str(self.root / ("round-%s" % self.round)), **kw)
        self.assertTrue(result["ok"], result)
        return result["pkg"]

    def apply(self, pkg):
        return self.core["apply_migrate"](pkg, self.env, target_uid="target-user")

    def initial(self, mode="selected"):
        result = self.apply(self.pack(mode))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["done"], 2 if mode == "all" else 1)
        self.assertEqual(self.target_body.read_text(encoding="utf-8"), '{"message":"old"}\n')

    def change_source(self, timestamp=200):
        with connect(self.sc / "workbuddy.db") as conn:
            conn.execute("UPDATE sessions SET updated_at=?,title='v2' WHERE id='session-a'", (timestamp,))
        write(self.body, '{"message":"old"}\n{"message":"new reply"}\n')
        write(self.sc / "artifact-index" / "session-a.json", '{"version":2}')
        write(self.sc / "changes-detail" / "session-a" / "edits.jsonl", '{"version":2}\n')
        write(self.cwd / "result.txt", "BBBB")
        write(self.cwd / "added.txt", "new output")

    def test_first_selected_import_still_excludes_unselected_session(self):
        self.initial()
        with connect(self.dc / "workbuddy.db") as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions WHERE id='session-b'").fetchone()[0], 0)
        self.assertFalse((self.dw / "project-b").exists())

    def test_first_full_import_still_imports_all_sessions(self):
        self.initial("all")
        self.assertTrue((self.dw / "project-b" / "result.txt").exists())

    def test_missing_package_database_is_reported_without_creating_empty_database(self):
        pkg = self.pack()
        db = pathlib.Path(pkg) / "缓存目录" / "workbuddy.db"
        db.unlink()
        result = self.core["check_migrate"](pkg, self.env)
        self.assertFalse(result["ok"], result)
        self.assertIn("没有 workbuddy.db", result["text"])
        self.assertFalse(db.exists(), "checking must not create a missing source database")

    def test_missing_package_database_cannot_use_previous_check_snapshot(self):
        pkg = self.pack()
        self.assertTrue(self.core["check_migrate"](pkg, self.env)["ok"])
        (pathlib.Path(pkg) / "缓存目录" / "workbuddy.db").unlink()
        result = self.core["check_migrate"](pkg, self.env)
        self.assertFalse(result["ok"], result)
        self.assertIn("没有 workbuddy.db", result["text"])

    def test_empty_package_database_explains_incomplete_transfer(self):
        pkg = self.pack()
        (pathlib.Path(pkg) / "缓存目录" / "workbuddy.db").write_bytes(b"")
        result = self.core["check_migrate"](pkg, self.env)
        self.assertFalse(result["ok"], result)
        self.assertIn("空", result["text"])

    def test_package_database_without_sessions_explains_invalid_schema(self):
        pkg = self.pack()
        db = pathlib.Path(pkg) / "缓存目录" / "workbuddy.db"
        db.unlink()
        with connect(db) as conn:
            conn.execute("CREATE TABLE unrelated (id INTEGER)")
        result = self.core["check_migrate"](pkg, self.env)
        self.assertFalse(result["ok"], result)
        self.assertIn("缺少 sessions 会话表", result["text"])

    def test_missing_snapshot_source_is_not_created(self):
        missing = self.root / "missing.db"
        self.assertIsNone(self.core["_snapshot_db"](str(missing), str(self.root / "snapshot")))
        self.assertFalse(missing.exists())

    def test_empty_standard_cache_does_not_hide_legacy_cache_database(self):
        pkg = pathlib.Path(self.pack())
        cache = pkg / "缓存目录"
        cache.rename(pkg / "cache")
        cache.mkdir()
        result = self.core["check_migrate"](str(pkg), self.env)
        self.assertTrue(result["ok"], result)

    def test_readonly_database_uri_preserves_network_share_paths(self):
        original_connect = sqlite3.connect
        def open_share(database, **kwargs):
            self.assertTrue(database.startswith("file:////fileserver/share/"), database)
            self.assertTrue(kwargs.get("uri"))
            return original_connect(str(self.sc / "workbuddy.db"))
        with mock.patch.object(sqlite3, "connect", side_effect=open_share):
            rows, _, _ = self.core["_read_sessions"](r"\\fileserver\share\workbuddy.db")
        self.assertEqual(len(rows), 2)

    def test_missing_package_database_cannot_use_workspace_artifact_database(self):
        pkg = pathlib.Path(self.pack())
        db = pkg / "缓存目录" / "workbuddy.db"
        artifact = pkg / "工作空间" / "project-a" / "workbuddy.db"
        artifact.write_bytes((self.sc / "workbuddy.db").read_bytes())
        db.unlink()
        result = self.core["check_migrate"](str(pkg), self.env)
        self.assertFalse(result["ok"], result)
        self.assertIn("没有 workbuddy.db", result["text"])

    def test_selected_import_updates_newer_session_and_outputs_with_backup(self):
        self._assert_newer_update("selected")

    def test_full_import_updates_newer_session_and_outputs_with_backup(self):
        self._assert_newer_update("all")

    def _assert_newer_update(self, mode):
        self.initial(mode)
        self.change_source()
        result = self.apply(self.pack(mode))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["done"], 1, "a newer existing session must be migrated")
        with connect(self.dc / "workbuddy.db") as conn:
            row = conn.execute("SELECT updated_at,title,user_id,local_only FROM sessions WHERE id='session-a'").fetchone()
        self.assertEqual(row, (200, "v2", "target-user", "keep-me"))
        self.assertIn("new reply", self.target_body.read_text(encoding="utf-8"))
        self.assertEqual((self.dc / "artifact-index" / "session-a.json").read_text(encoding="utf-8"), '{"version":2}')
        self.assertEqual((self.dc / "changes-detail" / "session-a" / "edits.jsonl").read_text(encoding="utf-8"), '{"version":2}\n')
        self.assertEqual((self.target / "result.txt").read_text(encoding="utf-8"), "BBBB")
        self.assertTrue((self.target / "added.txt").exists())
        backup = pathlib.Path(result["backup_dir"])
        self.assertEqual((backup / "工作空间" / "project-a" / "result.txt").read_text(encoding="utf-8"), "AAAA")
        saved_body = backup / "缓存目录" / self.target_body.relative_to(self.dc)
        self.assertEqual(saved_body.read_text(encoding="utf-8"), '{"message":"old"}\n')

    def test_newer_local_session_is_preserved(self):
        self.initial()
        with connect(self.dc / "workbuddy.db") as conn:
            conn.execute("UPDATE sessions SET updated_at=300,title='local-newer' WHERE id='session-a'")
        write(self.target_body, '{"message":"local-newer"}\n')
        write(self.target / "result.txt", "LOCAL")
        self.change_source(200)
        result = self.apply(self.pack())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["done"], 0)
        self.assertEqual(self.target_body.read_text(encoding="utf-8"), '{"message":"local-newer"}\n')
        self.assertEqual((self.target / "result.txt").read_text(encoding="utf-8"), "LOCAL")

    def test_equal_timestamp_does_not_overwrite_local_content(self):
        self.initial()
        self.change_source(100)
        result = self.apply(self.pack())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["done"], 0)
        self.assertNotIn("new reply", self.target_body.read_text(encoding="utf-8"))

    def test_repeated_import_is_idempotent(self):
        self.initial()
        self.change_source()
        pkg = self.pack()
        self.assertEqual(self.apply(pkg)["done"], 1)
        self.assertEqual(self.apply(pkg)["done"], 0)

    def test_existing_session_keeps_its_current_account(self):
        self.initial()
        with connect(self.dc / "workbuddy.db") as conn:
            conn.execute("UPDATE sessions SET user_id='other-local-user' WHERE id='session-a'")
        self.change_source()
        result = self.apply(self.pack())
        self.assertEqual(result["done"], 1)
        with connect(self.dc / "workbuddy.db") as conn:
            self.assertEqual(conn.execute("SELECT user_id FROM sessions WHERE id='session-a'").fetchone()[0], "other-local-user")

    def test_equal_size_file_changes_are_copied(self):
        source, target = self.root / "copy-source", self.root / "copy-target"
        write(source / "file.txt", "BBBB")
        write(target / "file.txt", "AAAA")
        self.core["_copy_tree"](str(source), str(target))
        self.assertEqual((target / "file.txt").read_text(encoding="utf-8"), "BBBB")

    def test_backup_error_aborts_before_updating_database_or_files(self):
        self.initial()
        self.change_source()
        pkg = self.pack()
        copy2 = self.core["shutil"].copy2

        def fail_backup(src, dst, *args, **kw):
            if "_backup_" in str(dst):
                raise OSError("synthetic backup failure")
            return copy2(src, dst, *args, **kw)

        self.core["shutil"].copy2 = fail_backup
        try:
            with self.assertRaisesRegex((OSError, RuntimeError), "备份|backup"):
                self.apply(pkg)
        finally:
            self.core["shutil"].copy2 = copy2
        self.assertNotIn("new reply", self.target_body.read_text(encoding="utf-8"))
        with connect(self.dc / "workbuddy.db") as conn:
            self.assertEqual(conn.execute("SELECT updated_at FROM sessions WHERE id='session-a'").fetchone()[0], 100)

    def test_failed_body_update_reports_failure_and_can_be_retried(self):
        self.initial()
        self.change_source()
        pkg = self.pack()
        real_open = open

        def fail_body(path, mode="r", *args, **kwargs):
            if str(path) == str(self.target_body) and mode == "w":
                raise PermissionError("synthetic body write failure")
            return real_open(path, mode, *args, **kwargs)

        self.core["open"] = fail_body
        try:
            result = self.apply(pkg)
        finally:
            del self.core["open"]
        self.assertFalse(result["ok"], result)
        with connect(self.dc / "workbuddy.db") as conn:
            self.assertEqual(conn.execute("SELECT updated_at FROM sessions WHERE id='session-a'").fetchone()[0], 100)
        self.assertNotIn("new reply", self.target_body.read_text(encoding="utf-8"))
        retried = self.apply(pkg)
        self.assertTrue(retried["ok"], retried)
        self.assertEqual(retried["done"], 1)
        self.assertIn("new reply", self.target_body.read_text(encoding="utf-8"))

    def test_old_slug_body_is_backed_up_before_path_repair(self):
        self.initial()
        old_body = self.dc / "projects" / self.core["cwd_to_slug"](str(self.cwd)) / "session-a.jsonl"
        old_body.parent.mkdir(parents=True)
        self.target_body.rename(old_body)
        self.target_body.parent.rmdir()
        with connect(self.dc / "workbuddy.db") as conn:
            conn.execute("UPDATE sessions SET cwd=? WHERE id='session-a'", (str(self.cwd),))
        self.change_source()
        result = self.apply(self.pack())
        self.assertTrue(result["ok"], result)
        saved = pathlib.Path(result["backup_dir"]) / "缓存目录" / old_body.relative_to(self.dc)
        self.assertTrue(saved.exists(), "old-location body must be saved before it is renamed and overwritten")
        self.assertEqual(saved.read_text(encoding="utf-8"), '{"message":"old"}\n')
        self.assertIn("new reply", self.target_body.read_text(encoding="utf-8"))

    def test_path_rewritten_local_index_is_backed_up(self):
        self.initial()
        index = self.dc / "artifact-index" / "seed.json"
        original = '{"path":"' + (self.cwd / "local.txt").as_posix() + '"}'
        write(index, original)
        self.change_source()
        result = self.apply(self.pack())
        self.assertTrue(result["ok"], result)
        saved = pathlib.Path(result["backup_dir"]) / "缓存目录" / "artifact-index" / "seed.json"
        self.assertTrue(saved.exists(), "path repair must back up the text it changes")
        self.assertEqual(saved.read_text(encoding="utf-8"), original)
        self.assertNotEqual(index.read_text(encoding="utf-8"), original)

    def test_path_rewritten_change_detail_json_is_backed_up(self):
        self.initial()
        index = self.dc / "changes-detail" / "seed" / "edits.json"
        original = '{"path":"' + (self.cwd / "local.txt").as_posix() + '"}'
        write(index, original)
        self.change_source()
        result = self.apply(self.pack())
        self.assertTrue(result["ok"], result)
        saved = pathlib.Path(result["backup_dir"]) / "缓存目录" / "changes-detail" / "seed" / "edits.json"
        self.assertTrue(saved.exists(), "change-detail JSON rewritten by path repair must be backed up")
        self.assertEqual(saved.read_text(encoding="utf-8"), original)

    def test_missing_updated_session_body_does_not_mark_it_current(self):
        self.initial()
        self.change_source()
        self.body.unlink()
        result = self.apply(self.pack())
        self.assertFalse(result["ok"], result)
        with connect(self.dc / "workbuddy.db") as conn:
            self.assertEqual(conn.execute("SELECT updated_at FROM sessions WHERE id='session-a'").fetchone()[0], 100)
        self.assertNotIn("new reply", self.target_body.read_text(encoding="utf-8"))

    def test_failed_workspace_copy_rolls_back_session_and_allows_retry(self):
        self.initial()
        self.change_source()
        pkg = self.pack()
        copy2 = self.core["shutil"].copy2

        def fail_workspace(src, dst, *args, **kwargs):
            if str(dst) == str(self.target / "result.txt"):
                raise PermissionError("synthetic workspace write failure")
            return copy2(src, dst, *args, **kwargs)

        self.core["shutil"].copy2 = fail_workspace
        try:
            result = self.apply(pkg)
        finally:
            self.core["shutil"].copy2 = copy2
        self.assertFalse(result["ok"], result)
        with connect(self.dc / "workbuddy.db") as conn:
            self.assertEqual(conn.execute("SELECT updated_at FROM sessions WHERE id='session-a'").fetchone()[0], 100)
        self.assertNotIn("new reply", self.target_body.read_text(encoding="utf-8"))
        self.assertEqual((self.target / "result.txt").read_text(encoding="utf-8"), "AAAA")
        result = self.apply(pkg)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["done"], 1)
        self.assertEqual((self.target / "result.txt").read_text(encoding="utf-8"), "BBBB")


class BrowserTests(unittest.TestCase):
    def logging_web(self, directory):
        functions = self.web_functions()
        ns = {"__builtins__": __builtins__, "os": os, "time": __import__("time"),
              "tempfile": tempfile, "_script_dir": lambda: directory, "_LOG_PATH": None}
        for name in ("_log_path", "diag"):
            ns[name] = types.FunctionType(functions[name], ns)
        if not ORIGINAL:
            from wb_fixes import install_web
            ns["Handler"] = type("Handler", (), {"_do_post": lambda self: None})
            install_web(ns)
        return ns

    def test_diagnostics_do_not_create_log_file(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT / "repair")) as directory:
            web = self.logging_web(directory)
            web["diag"]("startup")
            web["diag"]("migration finished")
            self.assertFalse((pathlib.Path(directory) / "wbmig-log.txt").exists())

    def test_diagnostics_leave_existing_log_file_untouched(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT / "repair")) as directory:
            path = pathlib.Path(directory) / "wbmig-log.txt"
            path.write_bytes(b"previous log")
            self.logging_web(directory)["diag"]("startup")
            self.assertEqual(path.read_bytes(), b"previous log")

    def web_functions(self):
        if ORIGINAL:
            code = marshal.loads((ROOT / "audit" / "wb_web.bin").read_bytes())
        else:
            wrapper = marshal.loads(unpack(entries["wb_web"]))
            payload = next(c for c in wrapper.co_consts if isinstance(c, bytes))
            code = marshal.loads(payload)
        return {c.co_name: c for c in code.co_consts if isinstance(c, types.CodeType)}

    def test_default_browser_keeps_web_interface_available(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"web interface alive")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        opened = []
        functions = self.web_functions()
        ns = {
            "__builtins__": __builtins__, "diag": lambda *a: None,
            "find_browser": lambda: "",
            "tempfile": types.SimpleNamespace(mkdtemp=lambda **kw: "synthetic-profile"),
            "os": types.SimpleNamespace(startfile=opened.append, getpid=lambda: 1),
            "sys": types.SimpleNamespace(executable="audit", argv=["audit"]),
            "LOG": types.SimpleNamespace(add=lambda *a: None), "run_server": lambda: server,
            "LAST_BEAT": [0], "FIRST_BEAT": [False],
        }

        class StopLoop(Exception):
            pass

        def stop_sleep(_seconds):
            raise StopLoop()

        ns["time"] = types.SimpleNamespace(time=lambda: 0, sleep=stop_sleep)
        if not ORIGINAL:
            from wb_fixes import default_browser_process
            ns["_DEFAULT_BROWSER_PROC"] = default_browser_process
        ns["open_window"] = types.FunctionType(functions["open_window"], ns)
        fallback = types.ModuleType("wb_gui")
        fallback.main = lambda: None
        prior = sys.modules.get("wb_gui")
        sys.modules["wb_gui"] = fallback
        try:
            try:
                types.FunctionType(functions["_run"], ns)()
            except StopLoop:
                pass
        finally:
            if prior is None:
                del sys.modules["wb_gui"]
            else:
                sys.modules["wb_gui"] = prior
        self.assertTrue(opened)
        self.assertFalse(server._BaseServer__is_shut_down.is_set(), "default browser must not shut down the web interface")
        url = "http://127.0.0.1:%s/" % server.server_address[1]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=1) as response:
            self.assertEqual(response.read(), b"web interface alive")

    def test_user_installed_browser_is_detected(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT / "repair")) as directory:
            user_browser = pathlib.Path(directory) / "Google" / "Chrome" / "Application" / "chrome.exe"
            write(user_browser, "synthetic executable")
            original = os.environ.get("LOCALAPPDATA")
            os.environ["LOCALAPPDATA"] = directory
            try:
                if ORIGINAL:
                    find = types.FunctionType(self.web_functions()["find_browser"], {"__builtins__": __builtins__, "os": os, "EDGE_CANDIDATES": ()})
                else:
                    from wb_fixes import find_browser
                    find = lambda: find_browser(())
                self.assertEqual(find(), str(user_browser))
            finally:
                if original is None:
                    del os.environ["LOCALAPPDATA"]
                else:
                    os.environ["LOCALAPPDATA"] = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
