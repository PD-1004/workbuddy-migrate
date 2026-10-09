"""Run the EXE's original core bytecode against disposable synthetic data."""
import hashlib
import json
import marshal
import pathlib
import sqlite3
import tempfile
import sys
import traceback
import types
from contextlib import contextmanager

HERE = pathlib.Path(__file__).absolute().parent
core = {"__name__": "wb_core_audit"}
exec(marshal.loads((HERE / "wb_core.bin").read_bytes()), core)
results = []


@contextmanager
def db_connection(path):
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def record(name, evidence):
    results.append({"case": name, **evidence})


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def database(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with db_connection(str(path)) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT, cwd TEXT, title TEXT, custom_title TEXT, updated_at INTEGER, deleted_at INTEGER)")
        conn.execute("CREATE TABLE workspaces (path TEXT PRIMARY KEY, last_opened_at INTEGER)")
        conn.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", rows)


with tempfile.TemporaryDirectory(prefix="fixtures_", dir=str(HERE)) as root:
    root = pathlib.Path(root)
    for mode in ["selected", "all"]:
        base = root / mode
        src_cache, src_ws = base / "source" / "cache", base / "source" / "workspace"
        dst_cache, dst_ws = base / "target" / "cache", base / "target" / "workspace"
        cwd = src_ws / "project-a"
        dst_cwd = dst_ws / "project-a"
        database(src_cache / "workbuddy.db", [
            ("session-a", "source-account", str(cwd), "title-v1", None, 100, None),
            ("session-b", "source-account", str(src_ws / "project-b"), "unselected", None, 100, None),
        ])
        database(dst_cache / "workbuddy.db", [
            ("local-seed", "target-account", str(dst_ws / "seed"), "seed", None, 50, None),
        ])
        dst_ws.mkdir(parents=True)
        src_body = src_cache / "projects" / core["cwd_to_slug"](str(cwd)) / "session-a.jsonl"
        dst_body = dst_cache / "projects" / core["cwd_to_slug"](str(dst_cwd)) / "session-a.jsonl"
        write(src_body, '{"message":"v1"}\n')
        write(cwd / "result.txt", "v1")
        write(cwd / "same-size.txt", "AAAA")
        write(src_ws / "project-b" / "result.txt", "excluded")
        env = core["LocalEnv"](str(dst_cache), str(dst_ws))

        def pack(round_name, selected):
            log = []
            kwargs = {"uids": ["source-account"], "session_ids": selected} if mode == "selected" else {}
            result = core["collect_source"](str(src_cache), str(src_ws), str(base / round_name), log.append, **kwargs)
            assert result["ok"], result
            return result["pkg"]

        pkg = pack("first", ["session-a"])
        pkg_db = pathlib.Path(core["pkg_cache_dir"](pkg)) / "workbuddy.db"
        with db_connection(str(pkg_db)) as conn:
            packaged_ids = [r[0] for r in conn.execute("SELECT id FROM sessions ORDER BY id")]
        first = core["apply_migrate"](pkg, env, target_uid="target-account")
        assert first["ok"] and first["done"] == (1 if mode == "selected" else 2), first
        assert dst_body.read_text(encoding="utf-8") == '{"message":"v1"}\n'
        assert (dst_cwd / "result.txt").read_text(encoding="utf-8") == "v1"
        record(mode + ": initial migration", {"done": first["done"], "packaged_ids": packaged_ids, "body_and_output_present": True})

        with db_connection(str(src_cache / "workbuddy.db")) as conn:
            conn.execute("UPDATE sessions SET updated_at=200,title='title-v2' WHERE id='session-a'")
        write(src_body, '{"message":"v1"}\n{"message":"new reply v2"}\n')
        write(cwd / "result.txt", "v2 with new output")
        write(cwd / "same-size.txt", "BBBB")
        write(cwd / "new-file.txt", "new output file")
        pkg = pack("second", ["session-a"])
        logs = []
        checked = core["check_migrate"](pkg, env)
        second = core["apply_migrate"](pkg, env, log=logs.append, target_uid="target-account")
        assert second["ok"] and second["done"] == 0, second
        with db_connection(str(dst_cache / "workbuddy.db")) as conn:
            title, updated = conn.execute("SELECT title,updated_at FROM sessions WHERE id='session-a'").fetchone()
        assert (title, updated) == ("title-v1", 100)
        assert "new reply" not in dst_body.read_text(encoding="utf-8")
        assert (dst_cwd / "result.txt").read_text(encoding="utf-8") == "v1"
        assert not (dst_cwd / "new-file.txt").exists()
        record(mode + ": same ID updated on source", {
            "todo": len(checked["todo"]), "done": second["done"], "target_updated_at": updated,
            "new_reply_imported": False, "changed_output_imported": False, "new_file_imported": False,
            "result_text": second["text"],
        })
        (HERE / (mode + "_second_import_log.txt")).write_text("\n".join(logs), encoding="utf-8")

        with db_connection(str(src_cache / "workbuddy.db")) as conn:
            conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", ("session-c", "source-account", str(cwd), "new session", None, 300, None))
        write(src_body.with_name("session-c.jsonl"), '{"message":"new session"}\n')
        pkg = pack("third", ["session-a", "session-c"])
        third = core["apply_migrate"](pkg, env, target_uid="target-account")
        assert third["ok"] and third["done"] == 1, third
        assert (dst_cwd / "result.txt").read_text(encoding="utf-8") == "v2 with new output"
        assert (dst_cwd / "same-size.txt").read_text(encoding="utf-8") == "AAAA"
        assert (dst_cwd / "new-file.txt").exists()
        assert "new reply" not in dst_body.read_text(encoding="utf-8")
        record(mode + ": new ID in existing workspace", {
            "done": third["done"], "different_size_output_overwritten": True,
            "equal_size_changed_output_skipped": True, "new_file_imported": True,
            "existing_session_reply_still_old": True,
        })

web_code = marshal.loads((HERE / "wb_web.bin").read_bytes())
web_functions = {c.co_name: c for c in web_code.co_consts if isinstance(c, types.CodeType)}
for fail_startfile in [False, True]:
    events = []

    def startfile(url):
        events.append("system default browser requested")
        if fail_startfile:
            raise OSError("synthetic browser launch failure")

    globals_ = {
        "__builtins__": __builtins__,
        "diag": events.append,
        "find_browser": lambda: "",
        "tempfile": types.SimpleNamespace(mkdtemp=lambda **kw: "synthetic-profile"),
        "os": types.SimpleNamespace(startfile=startfile, getpid=lambda: 1),
        "sys": types.SimpleNamespace(executable="audit", argv=["audit"]),
        "LOG": types.SimpleNamespace(add=events.append),
        "run_server": lambda: types.SimpleNamespace(server_address=("127.0.0.1", 12345), shutdown=lambda: events.append("HTTP server shut down")),
        "traceback": traceback,
    }
    globals_["open_window"] = types.FunctionType(web_functions["open_window"], globals_)
    fallback = types.ModuleType("wb_gui")
    fallback.main = lambda: events.append("simplified GUI launched")
    previous = sys.modules.get("wb_gui")
    sys.modules["wb_gui"] = fallback
    try:
        types.FunctionType(web_functions["_run"], globals_)()
    finally:
        if previous is None:
            del sys.modules["wb_gui"]
        else:
            sys.modules["wb_gui"] = previous
    assert "HTTP server shut down" in events and "simplified GUI launched" in events
    record("UI: no detected Edge/Chrome, default browser " + ("fails" if fail_startfile else "opens"), {
        "server_shut_down": True, "simplified_GUI_launched": True,
        "events": events,
    })

exe = next(HERE.parent.glob("*.exe"))
output = {"exe_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(), "results": results}
(HERE / "results.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(output, ensure_ascii=False, indent=2))
