"""Optional skill packing and backed-up replacement of conflicting skills."""
import hashlib
import json
import os
import pathlib
import shutil
import tempfile
import threading


def tree_info(path, fingerprint=False):
    count, size = 0, 0
    digest = hashlib.sha256()
    for folder, dirs, names in os.walk(path):
        dirs.sort()
        for name in dirs + sorted(names):
            item = os.path.join(folder, name)
            if os.path.normcase(os.path.realpath(item)) != os.path.normcase(os.path.abspath(item)):
                raise ValueError("技能包含链接，无法安全迁移：" + item)
            relative = os.path.relpath(item, path).replace("\\", "/")
            if fingerprint:
                digest.update((relative + "\0").encode("utf-8"))
            if os.path.isfile(item):
                count += 1
                size += os.path.getsize(item)
                if fingerprint:
                    with open(item, "rb") as stream:
                        while True:
                            block = stream.read(128 * 1024)
                            if not block:
                                break
                            digest.update(block)
    return count, size, digest.hexdigest()


def list_skills(cache_root, selected=None):
    root = os.path.join(cache_root, "skills")
    if not os.path.isdir(root):
        return []
    items = []
    for name in sorted(os.listdir(root)):
        if selected is not None and name not in selected:
            continue
        path = os.path.join(root, name)
        if not os.path.isdir(path) or name.startswith(".wb-skill-"):
            continue
        if os.path.normcase(os.path.realpath(path)) != os.path.normcase(os.path.abspath(path)):
            raise ValueError("技能目录包含链接，无法安全迁移：" + name)
        description = ""
        skill_file = os.path.join(path, "SKILL.md")
        if os.path.isfile(skill_file):
            text = pathlib.Path(skill_file).read_text(encoding="utf-8-sig", errors="replace")
            if text.startswith("---"):
                header = text.split("---", 2)[1]
                lines = header.splitlines()
                for index, line in enumerate(lines):
                    if line.startswith("description:"):
                        description = line.split(":", 1)[1].strip().strip('\"\'')
                        if description in ("", ">", "|", ">-", "|-"):
                            parts = []
                            for continuation in lines[index + 1:]:
                                if continuation and not continuation[0].isspace():
                                    break
                                parts.append(continuation.strip())
                            description = " ".join(parts).strip()
                        break
        count, size, _ = tree_info(path)
        items.append({"name": name, "description": description[:400], "files": count, "bytes": size})
    return items


def preview_skills(cache_root, target_root):
    items = list_skills(cache_root)
    for item in items:
        source = os.path.join(cache_root, "skills", item["name"])
        target = os.path.join(target_root, "skills", item["name"])
        item["local_bytes"] = 0
        if not os.path.exists(target):
            item["status"] = "new"
        elif not os.path.isdir(target) or os.path.normcase(os.path.realpath(target)) != os.path.normcase(os.path.abspath(target)):
            raise ValueError("本机同名技能不是普通目录，已停止迁移：" + item["name"])
        else:
            _, item["local_bytes"], local_hash = tree_info(target, True)
            item["status"] = "same" if tree_info(source, True)[2] == local_hash else "conflict"
    return items


def install_core(core):
    state = threading.local()
    original_collect = core["collect_source"]
    original_check = core["check_migrate"]
    original_apply = core["apply_migrate"]
    original_copy = core["_copy_tree"]
    original_backup = core["_backup_before_migrate"]

    def new_package_path(parent):
        base = os.path.join(parent, core["PKG_NAME_PREFIX"] + core["time"].strftime("%Y%m%d-%H%M%S"))
        path, number = base, 1
        while True:
            try:
                os.mkdir(path)
                return path
            except FileExistsError:
                number += 1
                path = base + "_%d" % number

    def copy_tree(src, dst, log=core["_noop"], label=""):
        packing = getattr(state, "packing", None)
        if not packing or os.path.normcase(os.path.abspath(src)) != packing["root"]:
            return original_copy(src, dst, log, label)
        if packing["mode"] == "none" or packing["mode"] == "custom" and packing["names"] == []:
            return 0, 0
        if packing["mode"] == "all":
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            for name in packing["names"]:
                shutil.copytree(os.path.join(src, name), os.path.join(dst, name), dirs_exist_ok=True)
        count, size, _ = tree_info(dst)
        return count, size

    def collect_source(cache_root, ws_root, dest_parent, log=core["_noop"], uids=None, session_ids=None,
                       skill_mode="all", skill_names=None):
        try:
            if skill_mode not in ("all", "none", "custom"):
                raise ValueError("无效的技能打包方式")
            if skill_mode == "custom":
                root = os.path.join(cache_root, "skills")
                available = set(os.listdir(root)) if os.path.isdir(root) else set()
                if not isinstance(skill_names, list) or any(not isinstance(name, str) or name not in available or not os.path.isdir(os.path.join(root, name)) for name in skill_names):
                    raise ValueError("所选技能不存在，请重新读取技能列表")
                names = sorted(set(skill_names))
                items = list_skills(cache_root, names) if names else []
            else:
                items = list_skills(cache_root) if skill_mode == "all" else []
                names = [item["name"] for item in items]
            state.packing = {"root": os.path.normcase(os.path.abspath(os.path.join(cache_root, "skills"))),
                             "mode": skill_mode, "names": names}
            result = original_collect(cache_root, ws_root, dest_parent, log, uids, session_ids)
            if result.get("ok"):
                manifest_path = pathlib.Path(result["pkg"]) / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                size = sum(item["bytes"] for item in items if item["name"] in names)
                manifest["skill_selection"] = {"mode": skill_mode, "names": names, "bytes": size}
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                result["text"] += "；携带技能 %d 个" % len(names)
                log("[技能] 携带 %d 个，%s" % (len(names), core["human"](size)))
            return result
        except (OSError, ValueError) as error:
            return {"ok": False, "text": "技能打包失败：" + str(error)}
        finally:
            state.packing = None

    def check_migrate(pkg, env, log=core["_noop"], target_uid=None):
        result = original_check(pkg, env, log, target_uid)
        if result.get("ok"):
            resolved = result.get("pkg") or core["resolve_pkg"](pkg)[0]
            cache = result.get("cache_dir") or core["pkg_cache_dir"](resolved)
            items = preview_skills(cache, env.cache_root)
            result["skills"] = items
            result["skill_new"] = [item["name"] for item in items if item["status"] == "new"]
            result["skill_conflict"] = [item["name"] for item in items if item["status"] == "conflict"]
            migrating = getattr(state, "migrating", None)
            if migrating:
                result["skill_new"] += [item["name"] for item in items if item["status"] == "conflict" and migrating["actions"].get(item["name"]) == "overwrite"]
        return result

    def backup(env, backup_dir, log, checked):
        original_backup(env, backup_dir, log, checked)
        migrating = getattr(state, "migrating", None)
        if not migrating:
            return
        migrating["backup_dir"] = backup_dir
        for item in checked.get("skills", []):
            if item["status"] == "conflict" and migrating["actions"].get(item["name"]) == "overwrite":
                source = os.path.join(env.cache_root, "skills", item["name"])
                dest = os.path.join(backup_dir, "缓存目录", "skills", item["name"])
                if os.path.exists(dest):
                    raise ValueError("技能备份位置已存在，请稍后重试：" + dest)
                shutil.copytree(source, dest)
                log("[技能备份] " + item["name"])

    def should_skip(src, dst):
        if not os.path.exists(dst):
            return False
        migrating = getattr(state, "migrating", None)
        if not migrating:
            return True
        name = os.path.basename(src)
        return migrating["statuses"].get(name) == "same" or migrating["actions"].get(name, "skip") != "overwrite"

    def install_skill(src, dst):
        migrating = state.migrating
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        stage = tempfile.mkdtemp(prefix=".wb-skill-", dir=os.path.dirname(dst))
        old = os.path.join(stage, "old")
        incoming = os.path.join(stage, "incoming")
        try:
            shutil.copytree(src, incoming)
            if os.path.exists(dst):
                os.replace(dst, old)
            migrating["installed"].append((dst, stage))
            os.replace(incoming, dst)
        except Exception:
            if not any(saved_stage == stage for _, saved_stage in migrating["installed"]):
                shutil.rmtree(stage)
            raise

    def apply_migrate(pkg, env, backup_dir=None, log=core["_noop"], target_uid=None, skill_actions=None):
        bypass = False
        try:
            checked = check_migrate(pkg, env, target_uid=target_uid)
            if not checked.get("ok"):
                return checked
            actions = {} if skill_actions is None else skill_actions
            statuses = {item["name"]: item["status"] for item in checked["skills"]}
            if not isinstance(actions, dict) or any(name not in statuses or action not in ("skip", "overwrite") for name, action in actions.items()):
                return {"ok": False, "text": "无效的技能冲突选择，请重新检查迁移包。"}
            if not statuses:
                bypass = True
                return original_apply(pkg, env, backup_dir, log, target_uid)
            state.migrating = {"actions": actions, "statuses": statuses, "installed": [], "backup_dir": None}
            try:
                result = original_apply(pkg, env, backup_dir, log, target_uid)
            except Exception as error:
                result = {"ok": False, "text": "技能迁移失败：" + str(error), "done": 0}
            context = state.migrating
            restore_errors = []
            for dst, stage in reversed(context["installed"]):
                try:
                    if not result.get("ok"):
                        if os.path.exists(dst):
                            shutil.rmtree(dst)
                        old = os.path.join(stage, "old")
                        if os.path.exists(old):
                            os.replace(old, dst)
                    shutil.rmtree(stage)
                except OSError as error:
                    if result.get("ok"):
                        log("[技能] 已迁入，但临时目录清理失败：%s；%s" % (stage, error))
                    else:
                        restore_errors.append(str(error))
            if restore_errors:
                result = {"ok": False, "text": "技能恢复失败：%s。请从备份恢复：%s" % (restore_errors[0], context["backup_dir"]), "done": 0}
            if context["backup_dir"]:
                result["backup_dir"] = context["backup_dir"]
            return result
        except (OSError, ValueError) as error:
            if bypass:
                raise
            return {"ok": False, "text": "技能检查失败：" + str(error), "done": 0}
        finally:
            state.migrating = None

    def requires_transaction():
        migrating = getattr(state, "migrating", None)
        return bool(migrating and any(status == "new" or status == "conflict" and migrating["actions"].get(name) == "overwrite"
                                     for name, status in migrating["statuses"].items()))

    core.update({"list_skills": list_skills, "collect_source": collect_source, "check_migrate": check_migrate,
                 "apply_migrate": apply_migrate, "_copy_tree": copy_tree, "_backup_before_migrate": backup,
                 "_should_skip_skill": should_skip, "_install_skill": install_skill,
                 "_skills_require_transaction": requires_transaction, "_new_package_path": new_package_path})


def install_web(web):
    original_post = web["Handler"]._do_post

    def post(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/api/skills", "/api/collect", "/api/check", "/api/apply"):
            return original_post(self)
        if not web["JOB_LOCK"].acquire(blocking=False):
            return self._json({"ok": False, "text": "有任务正在执行，请等它跑完。"})
        try:
            body = self._body()
            backend, core = web["BE"], web["core"]
            log = web["LOG"].add
            web["LAST_BEAT"][0] = web["time"].time()
            if path == "/api/skills":
                result = {"ok": True, "skills": core.list_skills(backend.env.cache_root)}
            elif path == "/api/collect":
                result = core.collect_source(backend.env.cache_root, backend.env.workspace_root,
                    (body.get("dest") or "").strip().strip('"'), log,
                    body.get("uids"), body.get("session_ids"), body.get("skill_mode", "all"), body.get("skill_names"))
            elif path == "/api/check":
                checked = core.check_migrate((body.get("pkg") or "").strip().strip('"'), backend.env,
                    log, body.get("target_uid"))
                result = {key: checked.get(key) for key in ("ok", "text", "pkg", "note", "local_accounts", "target_uid", "skills", "skill_conflict")}
                for key in ("todo", "repaired", "skill_new"):
                    result[key] = len(checked.get(key) or [])
            else:
                result = core.apply_migrate((body.get("pkg") or "").strip().strip('"'), backend.env,
                    log=log, target_uid=body.get("target_uid"), skill_actions=body.get("skill_actions"))
            return self._json(result)
        except (OSError, ValueError) as error:
            return self._json({"ok": False, "text": str(error)})
        finally:
            web["JOB_LOCK"].release()

    web["Handler"]._do_post = post
