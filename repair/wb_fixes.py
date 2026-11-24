"""Runtime helpers for the confirmed migration defects."""
import json
import os
import pathlib
import shutil
import sqlite3
import threading


# 国内版与国际版 WorkBuddy 的默认路径不是同一套。原版代码把国内版写死成
# ~/.workbuddy + ~/WorkBuddy，国际版用户启动后两个框都是空/错路径，必须手填。
VERSION_PROFILES = (
    ("cn", ".workbuddy", "WorkBuddy"),
    ("intl", ".workbuddy-ai", "WorkBuddy AI"),
)


def home_dir():
    return os.path.expanduser("~")


def db_readable(cache_root):
    """真正能打开且能查 sessions 表才算可用；损坏/加密/被占用的库不算候选。"""
    path = os.path.join(cache_root or "", "workbuddy.db")
    if not path or not os.path.isfile(path):
        return False
    try:
        conn = sqlite3.connect(pathlib.Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1.5)
        try:
            conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
            return True
        finally:
            conn.close()
    except Exception:
        return False


def db_ready(cache_root):
    return db_readable(cache_root)


def profile_cache_dir(key, home=None):
    home = home or home_dir()
    for name, cache_name, _ in VERSION_PROFILES:
        if name == key:
            return os.path.join(home, cache_name)
    return os.path.join(home, VERSION_PROFILES[0][1])


def db_sessions(cache_root):
    """只读数一下会话条数：两套目录都存在时，用它挑出用户真正在用的那一套。"""
    path = os.path.join(cache_root, "workbuddy.db")
    if not os.path.isfile(path):
        return 0
    try:
        conn = sqlite3.connect(pathlib.Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1.5)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return 0


def detect_cache_dir(home=None):
    """按 workbuddy.db 实际存在的位置判断是哪一个版本，都没有就沿用国内版默认值。

    两套目录都在时不能只看修改时间：国际版可能被启动过一次、留下一个几乎空的
    数据库，而修改时间反而是最新的。这里以「会话条数」为准，条数相同才比时间。
    """
    home = home or home_dir()
    forced = (os.environ.get("WBMIG_PROFILE") or "").strip().lower()
    if forced in ("cn", "intl"):
        forced_root = profile_cache_dir(forced, home)
        # 强制指定也要校验可读性，否则拼写正确但库损坏时会把用户导向空路径
        if db_ready(forced_root):
            return forced_root
    found = [os.path.join(home, item[1]) for item in VERSION_PROFILES if db_ready(os.path.join(home, item[1]))]
    if not found:
        return os.path.join(home, VERSION_PROFILES[0][1])
    if len(found) == 1:
        return found[0]
    ranked = sorted(found, key=lambda root: (db_sessions(root), os.path.getmtime(os.path.join(root, "workbuddy.db"))), reverse=True)
    return ranked[0]


def default_workspace(cache_root, home=None):
    """由缓存目录名反查同版本的工作空间默认目录。"""
    home = home or home_dir()
    name = os.path.basename(os.path.normpath(cache_root or "")).lower()
    for _, cache_name, workspace_name in VERSION_PROFILES:
        if name == cache_name.lower():
            return os.path.join(home, workspace_name)
    return os.path.join(home, VERSION_PROFILES[0][2])


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
        # 首次迁入（updates 为空）以前直接 return original_apply，绕开事务/备份/回滚：
        # 写到一半失败时会话已入库、产物半写，且没有任何备份。现在统一进入同一套
        # 事务壳，让首次迁入也享有数据库回滚 + 文件恢复。
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

    # ---- 双版本路径：不再把国内版路径写死 ----
    original_check = core["check_migrate"]
    detected = detect_cache_dir((os.environ.get("USERPROFILE") or home_dir()))
    core["WB_DIR"] = detected

    def read_workspace_root(cache_root):
        cfg = os.path.join(cache_root, "app", "app-config.json")
        path = None
        try:
            with open(cfg, encoding="utf-8") as stream:
                path = (json.load(stream) or {}).get("defaultWorkspacePath")
        except Exception:
            path = None
        if path:
            return os.path.normpath(path)
        return os.path.normpath(default_workspace(cache_root))

    def local_env(cache_root=None, workspace_root=None):
        cache_root = cache_root or core["WB_DIR"]
        if workspace_root is None:
            workspace_root = read_workspace_root(cache_root)
        return core["LocalEnv"](cache_root, workspace_root)

    def local_env_from_prefs(app_dir=None, cache_root=None):
        # prefs 里可能留着上一个版本（或上一次安装）的路径。存在 workbuddy.db
        # 才算有效，否则重新探测一次，不要直接拿死路径当默认值。
        prefs = core["load_prefs"](app_dir) or {}
        cache = prefs.get("cache_root") or cache_root or core["WB_DIR"]
        workspace = prefs.get("workspace_root") or ""
        if not db_ready(cache):
            probe = detect_cache_dir()
            if probe and os.path.normcase(probe) != os.path.normcase(cache):
                cache = probe
            workspace = ""
        if not workspace or not os.path.isdir(workspace):
            workspace = read_workspace_root(cache)
        return core["LocalEnv"](cache, workspace)

    # ---- 账号发现：account-snapshot.json 里登录过、但还没有会话的账号也要算 ----
    def account_profiles(cache_root):
        """{小写 uid: {"uid": 原始写法, "name": 昵称}}，保留快照里的 uid 原始大小写。"""
        found = {}
        for rel in core["ACCOUNT_SNAPSHOT_RELS"]:
            path = os.path.join(cache_root, *rel.split("/"))
            try:
                with open(path, encoding="utf-8") as stream:
                    data = json.load(stream)
            except Exception:
                continue
            stack = [data]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    uid = node.get("uid")
                    if isinstance(uid, str) and uid:
                        entry = found.setdefault(uid.lower(), {"uid": uid, "name": ""})
                        name = node.get("nickname") or node.get("name") or node.get("email")
                        if name:
                            entry["name"] = name
                    stack.extend(node.values())
                elif isinstance(node, list):
                    stack.extend(node)
        return found

    def account_names(cache_root):
        return {uid: info["name"] for uid, info in account_profiles(cache_root).items() if info["name"]}

    def _extra_accounts(cache_root, existing):
        """快照里有、会话表里还没有的账号。新电脑 / 新登录账号都在这一批。"""
        seen = {str(uid or "").lower() for uid in existing}
        extra = []
        for uid, info in account_profiles(cache_root).items():
            if uid in seen or uid in ("", "(未知)"):
                continue
            extra.append({
                "uid": info["uid"], "name": info["name"], "short": info["uid"][:8],
                "total": 0, "alive": 0, "deleted": 0, "latest": 0,
            })
        return extra

    def list_accounts(cache_root, log=None):
        db = os.path.join(cache_root, "workbuddy.db")
        if not os.path.isfile(db):
            return {"ok": False, "text": "系统缓存目录里没有 workbuddy.db。\n请填 WorkBuddy【设置 - 通用 → 系统缓存目录】里显示的那个路径：\n" + db}
        snap, root = core["_tmp_snapshot"](db)
        if not snap:
            return {"ok": False, "text": "复制数据库快照失败（磁盘空间不足或文件被占用）。"}
        try:
            rows, _cols, _ws = core["_read_sessions"](snap)
        except Exception as error:
            shutil.rmtree(root, ignore_errors=True)
            return {"ok": False, "text": "读取数据库失败：\n%s" % error}
        shutil.rmtree(root, ignore_errors=True)
        names = account_names(cache_root)
        agg = {}
        for r in rows:
            uid = r.get("user_id") or "(未知)"
            a = agg.get(uid)
            if a is None:
                a = agg[uid] = {"uid": uid, "total": 0, "alive": 0, "deleted": 0, "latest": 0}
            a["total"] += 1
            if r.get("deleted_at") is None:
                a["alive"] += 1
            else:
                a["deleted"] += 1
            try:
                ts = int(r.get("updated_at") or 0)
            except Exception:
                ts = 0
            if ts > a["latest"]:
                a["latest"] = ts
        out = sorted(agg.values(), key=lambda a: (-a["alive"], -a["latest"]))
        for a in out:
            a["name"] = names.get(str(a["uid"]).lower(), "")
            a["short"] = (a["uid"] or "")[:8]
        # 顺序：已经聊过的账号靠前，零会话账号排在它们后面，但仍可选择。
        out = out + _extra_accounts(cache_root, [a["uid"] for a in out])
        return {"ok": True, "accounts": out, "total": len(rows)}

    def check_migrate(pkg, env, log=None, target_uid=None):
        result = original_check(pkg, env, log if log is not None else core["_noop"], target_uid=target_uid)
        if not isinstance(result, dict) or not result.get("ok"):
            return result
        existing = result.get("local_accounts") or []
        accounts = list(existing) + _extra_accounts(env.cache_root, [a.get("uid") for a in existing])
        result["local_accounts"] = accounts
        # 原版用「会话表里出现过的 uid」判合法，零会话账号会被丢掉再回落成
        # local_accounts[0]，于是用户选的账号被静默换掉。这里以账号快照为准保留。
        if target_uid:
            lowered = str(target_uid).lower()
            match = next((a["uid"] for a in accounts if str(a["uid"] or "").lower() == lowered), None)
            if match:
                result["target_uid"] = match
            else:
                # 用户明确选了一个账号却在本机找不到：原版会悄悄回落成 local_accounts[0]，
                # 把会话登记到别的账号下。这里必须显式失败。
                return {"ok": False,
                        "text": "所选账号在当前版本本机不存在（%s）。请先在 WorkBuddy 登录该账号，或重新选择账号。" % target_uid}
        return result

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
        "read_workspace_root": read_workspace_root,
        "local_env": local_env,
        "local_env_from_prefs": local_env_from_prefs,
        "account_names": account_names,
        "list_accounts": list_accounts,
        "check_migrate": check_migrate,
    })
    from wb_skills import install_core as install_skills
    install_skills(core)


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
    web["diag"] = lambda msg: None
    web["_DEFAULT_BROWSER_PROC"] = default_browser_process
    web["find_browser"] = lambda: find_browser(web["EDGE_CANDIDATES"])
    from wb_skills import install_web as install_skills
    install_skills(web)
    install_loopback_proxy_fix(web)


def install_loopback_proxy_fix(web):
    """让界面（本机 127.0.0.1）绕开系统/环境变量里的代理。

    故障机型上 HTTP_PROXY/HTTPS_PROXY 有值、NO_PROXY 只写了 "::1"，于是
    http://127.0.0.1:<port>/ 被丢给代理并超时：页面白屏 → 没有 /api/poll
    心跳 → 45 秒后进程自杀。服务端本身是好的（curl --noproxy '*' 返回 200）。
    """
    import http.server as _http_server
    import subprocess as _subprocess
    import tempfile
    import time

    for key in ("NO_PROXY", "no_proxy"):
        os.environ[key] = "127.0.0.1,localhost,::1"

    log_path = os.path.join(tempfile.gettempdir(), "wbmig-diag.log")

    def diag(message):
        try:
            with open(log_path, "a", encoding="utf-8") as stream:
                stream.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
        except Exception:
            pass

    web["diag"] = diag
    handler = web["Handler"]

    class _Server(_http_server.ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True
        request_queue_size = 128

    def run_server():
        server = _Server(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        diag("server bound=%r thread_alive=%s" % (server.server_address, thread.is_alive()))
        try:
            import socket
            probe = socket.socket()
            probe.settimeout(2)
            code = probe.connect_ex(("127.0.0.1", server.server_address[1]))
            probe.close()
            diag("SELF-PROBE connect_ex=%s thread_alive_after=%s" % (code, thread.is_alive()))
        except Exception as error:
            diag("SELF-PROBE ERR %r" % (error,))
        return server

    web["run_server"] = run_server

    def open_window(url):
        exe = web["find_browser"]()
        profile = tempfile.mkdtemp(prefix="wbmig_ui_")
        if exe:
            diag("open window: browser=%s" % exe)
            try:
                sw, sh = web["_screen_size"]()
            except Exception:
                sw, sh = 0, 0
            win_w = max(900, min(1060, sw - 40)) if sw else 1060
            win_h = max(560, min(820, sh - 120)) if sh else 820
            try:
                x, y = web["_screen_center"](win_w, win_h)
            except Exception:
                x, y = 0, 0
            cmd = [
                exe,
                "--app=" + url,
                "--window-size=%d,%d" % (win_w, win_h),
                "--window-position=%d,%d" % (x, y),
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-sync",
                "--disable-features=msImplicitSignin,msSleepingTabs,CalculateNativeWinOcclusion",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--user-data-dir=" + profile,
                "--no-proxy-server",
            ]
            diag("browser cmd=%r" % (cmd,))
            try:
                return _subprocess.Popen(cmd)
            except Exception as error:
                diag("Popen failed: %r, fallback to os.startfile" % (error,))
        diag("no browser / Popen failed, fallback to os.startfile")
        try:
            os.startfile(url)
        except Exception:
            pass
        return web["_DEFAULT_BROWSER_PROC"]

    web["open_window"] = open_window
    diag("loopback proxy fix installed, NO_PROXY=%s" % os.environ.get("NO_PROXY"))
