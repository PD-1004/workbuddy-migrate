"""Runtime helpers for the confirmed migration defects."""
import os
import pathlib
import shutil
import sqlite3
import threading


def same_content(src, dst):
    if not os.path.isfile(dst) or os.path.getsize(src) != os.path.getsize(dst):
        return False
    with open(src, "rb") as a, open(dst, "rb") as b:
        while True:
            block = a.read(128 * 1024)
            if block != b.read(128 * 1024):
                return False
            if not block:
                return True


def retained_ids(local_rows, source_rows):
    source = {r.get("id"): r for r in source_rows}
    return {
        r.get("id") for r in local_rows
        if float(source.get(r.get("id"), {}).get("updated_at") or 0)
        <= float(r.get("updated_at") or 0)
    }


def upsert_sql(keys):
    quote = lambda key: '"' + key.replace('"', '""') + '"'
    updates = [quote(key) + '=excluded.' + quote(key) for key in keys if key not in ("id", "user_id")]
    conflict = "DO UPDATE SET " + ",".join(updates) if updates else "DO NOTHING"
    return "INSERT INTO sessions (%s) VALUES (%s) ON CONFLICT(id) %s" % (
        ",".join(map(quote, keys)), ",".join("?" for _ in keys), conflict,
    )


def keep_existing(path, item, local_rows):
    # New registrations keep the old skip-existing rule; newer existing
    # sessions replace their body and indexes after backing them up.
    return os.path.exists(path) and not any(r.get("id") == item["id"] for r in local_rows)


def merge_updated_tree(src, dst):
    return shutil.copytree(src, dst, dirs_exist_ok=True)


def install_core(core):
    state = threading.local()
    original_apply = core["apply_migrate"]
    original_copy_tree = core["_copy_tree"]
    original_snapshot = core["_snapshot_db"]

    def pkg_cache_dir(pkg):
        cache_names = (core["PKG_CACHE"], "cache", "缓存目录")
        for name in cache_names:
            cache = os.path.join(pkg, name)
            if os.path.isfile(os.path.join(cache, "workbuddy.db")):
                return cache
        if any(os.path.isdir(os.path.join(pkg, name)) for name in cache_names):
            return ""
        for folder, dirs, names in os.walk(pkg):
            # Snapshots, backups and workspace artifacts are not cache inputs.
            dirs[:] = [name for name in dirs if name not in ("_tmp_db", core["PKG_WS"], "workspace", "工作空间", "skills") and not name.startswith("_backup_")]
            if folder != pkg and "workbuddy.db" in names:
                return folder
        return ""

    def snapshot_db(src_db, tmp_dir):
        if not os.path.isfile(src_db):
            return None
        return original_snapshot(src_db, tmp_dir)

    def read_sessions(db_path):
        path = pathlib.Path(db_path).absolute()
        uri = path.as_uri()
        if path.drive.startswith("\\\\"):
            uri = uri.replace("file://", "file:////", 1)
        conn = sqlite3.connect(uri + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                raise ValueError("数据库为空，请从源机重新传输完整迁移包（包括缓存目录/workbuddy.db）。")
            if "sessions" not in tables:
                raise ValueError("数据库缺少 sessions 会话表，迁移包可能不完整或数据库格式不兼容。请确认源机缓存路径并重新打包。")
            rows = [dict(row) for row in conn.execute("SELECT * FROM sessions")]
            cols = [row[1] for row in conn.execute("PRAGMA table_info(sessions)")]
            ws = [dict(row) for row in conn.execute("SELECT * FROM workspaces")] if "workspaces" in tables else []
            return rows, cols, ws
        finally:
            conn.close()

    def strict_log(log):
        def emit(message):
            log(message)
            if getattr(state, "backup_ready", False) and "[!]" in message:
                raise RuntimeError(message)
        return emit

    def migration_connect(path):
        conn = sqlite3.connect(path)
        if getattr(state, "active", False):
            state.conn = conn
        return conn

    def migration_commit(conn):
        if not getattr(state, "active", False):
            conn.commit()

    def migration_close(conn):
        if not getattr(state, "active", False):
            conn.close()

    def copy_tree(src, dst, log=core["_noop"], label=""):
        return original_copy_tree(src, dst, strict_log(log), label)

    def apply_migrate(pkg, env, backup_dir=None, log=core["_noop"], target_uid=None):
        checked = core["check_migrate"](pkg, env, target_uid=target_uid)
        if not checked.get("ok"):
            return original_apply(pkg, env, backup_dir, log, target_uid)
        local_rows, _, _ = core["_read_sessions"](env.db)
        ids = {row.get("id") for row in local_rows}
        updates = [item for item in checked.get("todo") or [] if item["id"] in ids]
        if not updates:
            return original_apply(pkg, env, backup_dir, log, target_uid)
        if any(not item["has_body"] for item in updates):
            return {"ok": False, "text": "迁移包中的已有会话缺少正文，已停止更新，请重新打包。", "done": 0}
        state.active, state.backup_ready, state.conn = True, False, None
        try:
            result = original_apply(pkg, env, backup_dir, strict_log(log), target_uid)
            if state.conn is not None:
                state.conn.commit()
            return result
        except Exception as error:
            if state.conn is not None:
                state.conn.rollback()
                state.conn.close()
                state.conn = None
            if not state.backup_ready:
                raise
            restore_errors = []
            try:
                source = sqlite3.connect(state.snapshot)
                target = sqlite3.connect(env.db)
                try:
                    source.backup(target)
                finally:
                    source.close()
                    target.close()
            except Exception as restore_error:
                restore_errors.append(str(restore_error))
            for label, root in (("缓存目录", env.cache_root), ("工作空间", env.workspace_root)):
                saved_root = os.path.join(state.backup_dir, label)
                for folder, _, names in os.walk(saved_root):
                    for name in names:
                        saved = os.path.join(folder, name)
                        dest = os.path.join(root, os.path.relpath(saved, saved_root))
                        try:
                            if same_content(saved, dest):
                                continue
                            os.makedirs(os.path.dirname(dest), exist_ok=True)
                            shutil.copy2(saved, dest)
                        except Exception as restore_error:
                            restore_errors.append(str(restore_error))
            text = "更新失败：%s。已恢复数据库和已备份文件，可重新执行迁移。" % error
            if restore_errors:
                text = "更新失败：%s。部分备份恢复失败：%s，请从备份恢复后重试。" % (error, restore_errors[0])
            text += "\n备份位置：" + state.backup_dir
            log(text)
            return {"ok": False, "text": text, "done": 0, "failed": 1, "backup_dir": state.backup_dir}
        finally:
            if state.conn is not None:
                state.conn.close()
            state.active, state.backup_ready, state.conn = False, False, None

    def backup_before_migrate(env, backup_dir, log, checked):
        log("先备份本机数据库及将替换的文件")
        errors = []

        def db_log(message):
            log(message)
            if "[!]" in message:
                errors.append(message)

        core["_backup_local_db"](env, backup_dir, db_log)
        if errors:
            raise RuntimeError("备份失败，已停止迁移：" + errors[0])

        count = 0

        def save(path, root, label):
            nonlocal count
            if os.path.isdir(path):
                for folder, _, names in os.walk(path):
                    for name in names:
                        save(os.path.join(folder, name), root, label)
            elif os.path.isfile(path):
                dest = os.path.join(backup_dir, label, os.path.relpath(path, root))
                if not os.path.exists(dest):
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copy2(path, dest)
                    count += 1

        def save_changed_tree(src, dst, root, label):
            for folder, _, names in os.walk(src):
                for name in names:
                    source = os.path.join(folder, name)
                    target = os.path.join(dst, os.path.relpath(source, src))
                    if os.path.isfile(target) and not same_content(source, target):
                        save(target, root, label)

        cache = checked["cache_dir"]
        source_projects = os.path.join(cache, "projects")
        body_dirs = {}
        for folder, _, names in os.walk(source_projects):
            for name in names:
                if name.endswith(".jsonl"):
                    body_dirs.setdefault(name[:-6], folder)
        local_rows, _, _ = core["_read_sessions"](env.db)
        local_ids = {r.get("id") for r in local_rows}
        todo = checked.get("todo") or []
        for item in todo:
            if item["id"] not in local_ids:
                continue
            source_dir = body_dirs.get(item["id"])
            if source_dir:
                slug = core["cwd_to_slug"](item.get("new_cwd") or item["cwd"]) or os.path.basename(source_dir)
                for name in os.listdir(source_dir):
                    if name.startswith(item["id"]):
                        save(os.path.join(env.cache_root, "projects", slug, name), env.cache_root, "缓存目录")
            for label in ("artifact-index", "changes-index", "changes-detail", "file-tree-manifests", "tasks"):
                for name in (item["id"], item["id"] + ".json"):
                    save(os.path.join(env.cache_root, label, name), env.cache_root, "缓存目录")

        # Path repair runs before updates. Save files at their current locations
        # before the original repair renames directories or rewrites their text.
        pairs = checked.get("pairs") or []
        if pairs:
            rewrite = core["make_rewriter"](pairs)
            for row in local_rows:
                old_cwd = row.get("cwd") or ""
                new_cwd, _ = core["rewrite_path"](old_cwd, rewrite)
                if core["_norm"](old_cwd) != core["_norm"](new_cwd):
                    save(os.path.join(env.cache_root, "projects", core["cwd_to_slug"](old_cwd)), env.cache_root, "缓存目录")
            for label, extension, recursive in (("projects", ".jsonl", True), ("artifact-index", ".json", False), ("changes-index", ".json", False), ("file-tree-manifests", ".json", False), ("changes-detail", ".json", True)):
                base = os.path.join(env.cache_root, label)
                for folder, dirs, names in os.walk(base):
                    if not recursive:
                        dirs[:] = []
                    for name in names:
                        if not name.endswith(extension):
                            continue
                        path = os.path.join(folder, name)
                        try:
                            with open(path, "r", encoding="utf-8", errors="surrogateescape") as stream:
                                _, replacements = rewrite(stream.read())
                        except OSError:
                            continue  # The original path repair also skips unreadable text.
                        if replacements:
                            save(path, env.cache_root, "缓存目录")

        seen = set()
        entities = todo + [{"cwd": item["old"], "rel": ""} for item in checked.get("repaired") or []]
        for item in entities:
            candidates = [item["rel"]] if item.get("rel") else []
            candidates += core["_ws_rel_candidates"](item.get("cwd") or "", checked.get("manifest") or {}, env)
            for relative in candidates:
                if relative and relative not in seen:
                    seen.add(relative)
                    save_changed_tree(os.path.join(checked["ws_src"], relative), os.path.join(env.workspace_root, relative), env.workspace_root, "工作空间")
        for label in ("blobs", "clipboard-images"):
            save_changed_tree(os.path.join(cache, label), os.path.join(env.cache_root, label), env.cache_root, "缓存目录")
        if count:
            log("  [备份] 将替换的文件 %d 个" % count)
        if getattr(state, "active", False):
            state.backup_dir = backup_dir
            state.snapshot = core["_snapshot_db"](env.db, os.path.join(backup_dir, "_rollback_db"))
            if not state.snapshot:
                raise RuntimeError("数据库回滚快照备份失败，已停止更新")
            state.backup_ready = True

    core.update({
        "pkg_cache_dir": pkg_cache_dir,
        "_snapshot_db": snapshot_db,
        "_read_sessions": read_sessions,
        "_same_content": same_content,
        "_retained_ids": retained_ids,
        "_upsert_sql": upsert_sql,
        "_keep_existing": keep_existing,
        "_merge_updated_tree": merge_updated_tree,
        "_backup_before_migrate": backup_before_migrate,
        "_migration_connect": migration_connect,
        "_migration_commit": migration_commit,
        "_migration_close": migration_close,
        "apply_migrate": apply_migrate,
        "_copy_tree": copy_tree,
    })


class _DefaultBrowserProcess:
    # The existing watchdog owns the lifetime via page heartbeats when the
    # system browser has no Popen process handle.
    def poll(self):
        return None


default_browser_process = _DefaultBrowserProcess()


def find_browser(candidates):
    paths = list(candidates)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        paths += [
            os.path.join(local, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
        ]
    for browser in ("msedge.exe", "chrome.exe"):
        found = shutil.which(browser)
        if found:
            paths.append(found)
    return next((path for path in paths if os.path.isfile(path)), "")


def install_web(web):
    web["_DEFAULT_BROWSER_PROC"] = default_browser_process
    web["find_browser"] = lambda: find_browser(web["EDGE_CANDIDATES"])
