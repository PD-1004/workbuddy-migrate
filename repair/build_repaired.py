"""Patch only known CPython 3.9 instructions and repack original resources.

Requires the original EXE, Python 3.9, and this directory's wb_fixes.py.
No decompiler, compiler toolchain, or third-party build dependencies are used.
"""
import dis
import hashlib
import json
import marshal
import pathlib
import struct
import sys
import types
import zlib

ROOT = pathlib.Path(__file__).absolute().parent.parent
HERE = ROOT / "repair"
BUILD = HERE / "build"
RELEASE = ROOT / "修复版"
ORIGINAL_HASH = "d129fb12d51083019f07235b566d3d7d34edd5cde80e0e9e3501c180f3567519"
MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"


def archive(data):
    cookie_at = data.rfind(MAGIC)
    cookie = struct.unpack("!8sIIII64s", data[cookie_at:cookie_at + 88])
    _, length, offset, size, version, library = cookie
    start = cookie_at + 88 - length
    entries, cursor = [], start + offset
    while cursor < start + offset + size:
        entry_size, pos, compressed, raw, flag, kind = struct.unpack("!IIIIBc", data[cursor:cursor + 18])
        name_bytes = data[cursor + 18:cursor + entry_size]
        name = name_bytes.rstrip(b"\0").decode("utf-8")
        payload = data[start + pos:start + pos + compressed]
        entries.append({"name": name, "name_bytes": name_bytes, "kind": kind, "flag": flag, "raw_size": raw, "payload": payload})
        cursor += entry_size
    return start, version, library, entries


def unpack(entry):
    return zlib.decompress(entry["payload"]) if entry["flag"] else entry["payload"]


class Patch:
    def __init__(self, code):
        self.code = code
        self.names = list(code.co_names)
        self.data = bytearray(code.co_code)

    def emit(self, instructions):
        output = bytearray()
        for name, value in instructions:
            op = dis.opmap[name]
            if op in dis.hasname:
                if value not in self.names:
                    self.names.append(value)
                value = self.names.index(value)
            elif op in dis.haslocal:
                value = self.code.co_varnames.index(value)
            elif op in dis.hasfree:
                value = (self.code.co_cellvars + self.code.co_freevars).index(value)
            value = value or 0
            assert 0 <= value < 256, (name, value)
            output.extend((op, value))
        return output

    def replace(self, start, end, instructions):
        # Keep instruction addresses, branch destinations and exception blocks
        # unchanged. Reject a patch that would erase a branch target.
        original = list(dis.get_instructions(self.code))
        assert all(not (start < i.offset < end and i.is_jump_target) for i in original)
        output = self.emit(instructions)
        assert len(output) <= end - start and (end - start - len(output)) % 2 == 0
        self.data[start:end] = output + bytes((dis.opmap["NOP"], 0)) * ((end - start - len(output)) // 2)

    def finish(self):
        return self.code.replace(co_code=bytes(self.data), co_names=tuple(self.names), co_stacksize=max(self.code.co_stacksize, 16))


def condition_span(code, line):
    instructions = list(dis.get_instructions(code))
    begin = next(i for i, instruction in enumerate(instructions) if instruction.starts_line == line)
    call = next(i for i in range(begin, len(instructions)) if instructions[i].opname == "CALL_METHOD")
    return instructions[begin].offset, instructions[call].offset + 2


def patch_core_function(code):
    patch = Patch(code)
    if code.co_name == "_copy_tree":
        patch.replace(98, 132, [("LOAD_GLOBAL", "_same_content"), ("LOAD_FAST", "s"), ("LOAD_FAST", "d"), ("CALL_FUNCTION", 2)])
    elif code.co_name == "collect_source":
        patch.replace(290, 314, [("LOAD_GLOBAL", "_new_package_path"), ("LOAD_FAST", "dest_parent"), ("CALL_FUNCTION", 1), ("STORE_FAST", "pkg")])
    elif code.co_name == "check_migrate":
        patch.replace(404, 422, [("LOAD_GLOBAL", "_retained_ids"), ("LOAD_FAST", "local_rows"), ("LOAD_FAST", "src_rows"), ("CALL_FUNCTION", 2), ("STORE_DEREF", "local_ids")])
    elif code.co_name == "apply_migrate":
        patch.replace(198, 218, [("LOAD_GLOBAL", "_backup_before_migrate"), ("LOAD_FAST", "env"), ("LOAD_FAST", "backup_dir"), ("LOAD_FAST", "log"), ("LOAD_FAST", "r"), ("CALL_FUNCTION", 4), ("POP_TOP", 0)])
        patch.replace(548, 590, [("LOAD_GLOBAL", "_upsert_sql"), ("LOAD_FAST", "keys"), ("CALL_FUNCTION", 1), ("STORE_FAST", "sql")])
        # Existing-session updates commit only after required copies succeed.
        patch.replace(330, 340, [("LOAD_GLOBAL", "_migration_connect"), ("LOAD_FAST", "env"), ("LOAD_ATTR", "db"), ("CALL_FUNCTION", 1)])
        patch.replace(876, 884, [("LOAD_GLOBAL", "_migration_commit"), ("LOAD_FAST", "con"), ("CALL_FUNCTION", 1), ("POP_TOP", 0)])
        patch.replace(884, 892, [("LOAD_GLOBAL", "_migration_close"), ("LOAD_FAST", "con"), ("CALL_FUNCTION", 1), ("POP_TOP", 0)])
        for line in (1281, 1316):
            start, end = condition_span(code, line)
            patch.replace(start, end, [("LOAD_GLOBAL", "_keep_existing"), ("LOAD_FAST", "d"), ("LOAD_FAST", "it"), ("LOAD_FAST", "local_rows"), ("CALL_FUNCTION", 3)])
        for line, source in ((1292, "s"), (1321, "cand")):
            start, end = condition_span(code, line)
            patch.replace(start, end, [("LOAD_GLOBAL", "_merge_updated_tree"), ("LOAD_FAST", source), ("LOAD_FAST", "d"), ("CALL_FUNCTION", 2)])
        start, end = condition_span(code, 1375)
        patch.replace(start, end, [("LOAD_GLOBAL", "_should_skip_skill"), ("LOAD_FAST", "s"), ("LOAD_FAST", "d"), ("CALL_FUNCTION", 2)])
        patch.replace(2452, 2464, [("LOAD_GLOBAL", "_install_skill"), ("LOAD_FAST", "s"), ("LOAD_FAST", "d"), ("CALL_FUNCTION", 2), ("POP_TOP", 0)])
    return patch.finish() if bytes(patch.data) != code.co_code else code


TEXT = {
    "⑤ 合并技能目录（只补缺失，不覆盖本机）": "⑤ 迁入技能（同名技能按所选方式处理）",
    "  [技能] %d 个技能两边都有，保留本机版不覆盖：%s": "  [技能] %d 个同名技能，请查看技能列表选择跳过或覆盖：%s",
    "会话：源机未删除 %d 条，本机缺 %d 条（只补缺，不覆盖）": "会话：源机未删除 %d 条，本机需迁移/更新 %d 条（仅更新源机较新的会话）",
    "没有需要处理的内容，本机已是最新": "无需迁移：已有会话以本机较新或相同版本为准",
    "完成：新迁移 %d 条，修复路径 %d 处，补技能 %d 个，产物文件 %d 个": "完成：迁移/更新 %d 条，修复路径 %d 处，补技能 %d 个，产物文件 %d 个",
    "  · 只补本机缺少的会话，已有的不覆盖": "  · 补入缺少的会话；源机较新的已有会话会更新",
    "先自动备份本机数据库": "先备份本机数据库和将替换的文件",
    "迁移了 %d 条会话。": "迁移/更新了 %d 条会话。",
    "  [归属] %d 条会话改写为本机账号 %s": "  [归属] 处理 %d 条会话；新增会话使用 %s，已有会话保留原账号",
    "迁移会：补登记会话、搬产物文件本体、把所有源机路径改写成本机路径、补装本机没有的技能。只补缺不覆盖，执行前自动备份。请先完全退出 WorkBuddy。": "迁移会：补登记会话、更新源机较新的已有会话、搬产物文件本体、改写源机路径、补装缺少的技能。替换前自动备份。请先完全退出 WorkBuddy。",
}


def transform(code, kind):
    constants = []
    for item in code.co_consts:
        if isinstance(item, types.CodeType):
            item = transform(item, kind)
        elif isinstance(item, str):
            for old, new in TEXT.items():
                item = item.replace(old, new)
        constants.append(item)
    code = code.replace(co_consts=tuple(constants))
    if kind == "core":
        code = patch_core_function(code)
    elif kind == "web" and code.co_name == "open_window":
        patch = Patch(code)
        assert next(i for i in dis.get_instructions(code) if i.offset == 242).argval is None
        patch.replace(242, 244, [("LOAD_GLOBAL", "_DEFAULT_BROWSER_PROC")])
        code = patch.finish()
    return code


def wrap(code, installer, run_main=False):
    if run_main:
        code = code.replace(co_consts=tuple("__wb_before_repair__" if x == "__main__" else x for x in code.co_consts))
    source = "import marshal as _repair_marshal\nexec(_repair_marshal.loads(%r), globals())\nfrom wb_fixes import %s as _repair_install\n_repair_install(globals())\n" % (marshal.dumps(code), installer)
    if run_main:
        source += "from wb_updates import install as _update_install\n_update_install(globals())\n"
        source += "if __name__ == '__main__':\n    main()\n"
    return compile(source, code.co_filename, "exec")


def rebuild_pyz(data):
    old_toc = list(marshal.loads(data[struct.unpack("!I", data[8:12])[0]:]))
    output, toc = bytearray(data[:17]), []
    changed = []
    for name, (kind, pos, length) in old_toc:
        payload = data[pos:pos + length]
        if name in ("wb_core", "wb_gui"):
            code = marshal.loads(zlib.decompress(payload))
            code = transform(code, "core" if name == "wb_core" else "gui")
            if name == "wb_core":
                code = wrap(code, "install_core")
            raw = marshal.dumps(code)
            (BUILD / (name + ".bin")).write_bytes(raw)
            payload = zlib.compress(raw, 6)
            changed.append(name)
        toc.append((name, (kind, len(output), len(payload))))
        output.extend(payload)
    helper = compile((HERE / "wb_fixes.py").read_text(encoding="utf-8"), "wb_fixes.py", "exec")
    payload = zlib.compress(marshal.dumps(helper), 6)
    toc.append(("wb_fixes", (0, len(output), len(payload))))
    output.extend(payload)
    helper = compile((HERE / "wb_skills.py").read_text(encoding="utf-8"), "wb_skills.py", "exec")
    payload = zlib.compress(marshal.dumps(helper), 6)
    toc.append(("wb_skills", (0, len(output), len(payload))))
    output.extend(payload)
    config = json.loads((HERE / "update_config.json").read_text(encoding="utf-8"))
    updates = (HERE / "wb_updates.py").read_text(encoding="utf-8")
    for key in ("RELEASE_BASE", "UPDATE_FEED"):
        updates = updates.replace('"__' + key + '__"', repr(config[key]))
    helper = compile(updates, "wb_updates.py", "exec")
    payload = zlib.compress(marshal.dumps(helper), 6)
    toc.append(("wb_updates", (0, len(output), len(payload))))
    output.extend(payload)
    offset = len(output)
    output.extend(marshal.dumps(toc))
    output[8:12] = struct.pack("!I", offset)
    return bytes(output), changed + ["wb_fixes (added)", "wb_updates (added)", "wb_skills (added)"]


def main():
    assert sys.version_info[:2] == (3, 9), "Use Python 3.9 to match the original EXE"
    original = next(ROOT.glob("*.exe"))
    data = original.read_bytes()
    assert hashlib.sha256(data).hexdigest() == ORIGINAL_HASH, "Original EXE changed; do not apply offset patches"
    BUILD.mkdir(exist_ok=True)
    RELEASE.mkdir(exist_ok=True)
    start, version, library, entries = archive(data)
    output, toc, changes = bytearray(data[:start]), [], []
    for entry in entries:
        payload = entry["payload"]
        if entry["name"] == "PYZ.pyz":
            raw, modules = rebuild_pyz(unpack(entry))
        elif entry["name"] == "wb_web":
            code = transform(marshal.loads(unpack(entry)), "web")
            (BUILD / "wb_web_patched.bin").write_bytes(marshal.dumps(code))
            raw = marshal.dumps(wrap(code, "install_web", run_main=True))
            (BUILD / "wb_web.bin").write_bytes(raw)
        elif entry["name"] == "wb_ui.html":
            html = unpack(entry).decode("utf-8")
            html = html.replace('flex:1;overflow:auto;padding:10px 16px 14px;', 'flex:1;min-height:0;contain:size;overflow:auto;padding:10px 16px 14px;', 1)
            html = html.replace('.logwrap{min-width:auto;min-height:180px}', '.logwrap{flex:none;min-width:auto;min-height:180px;height:300px}', 1)
            html = html.replace("只补缺不覆盖", "补缺与更新")
            html = html.replace("只补本机缺少的会话，已有的不覆盖", "补入缺少的会话；源机较新的已有会话会更新")
            html = html.replace("会话会登记到这个账号下", "新增会话登记到这个账号下，已有会话保留原账号")
            html = html.replace("先自动备份本机数据库", "先备份本机数据库和将替换的文件")
            html = html.replace("技能只补缺失，不覆盖本机已有", "技能按已选方式处理，覆盖前备份本机技能")
            html = html.replace("全部会话正文 + 产物文件 + 技能", "全部会话正文 + 产物文件 + 所选技能")
            html = html.replace('<div id="dest-row"></div>', '<div id="skills-src"></div><div id="dest-row"></div>', 1)
            html = html.replace('<div id="pkg-row"></div>', '<div id="pkg-row"></div><div id="skills-dst"></div>', 1)
            html = html.replace("补登记会话、搬产物本体、改写路径、补装技能。补缺与更新，执行前自动备份。", "补登记会话、更新源机较新的已有会话、搬产物本体、改写路径、补装技能。替换前自动备份。")
            html = html.replace('<div class="wxwrap" id="wxwrap">', '<div class="header-actions"><button class="update-button" id="update-button" hidden onclick="checkUpdate(true)">更新</button><div class="wxwrap" id="wxwrap">', 1)
            html = html.replace('</header>', '</div></header>', 1)
            html = html.replace('</body>', (HERE / "update_ui.html").read_text(encoding="utf-8") + '\n</body>', 1)
            html = html.replace('</body>', (HERE / "skills_ui.html").read_text(encoding="utf-8") + '\n</body>', 1)
            raw = html.encode("utf-8")
        else:
            raw = None
        if raw is not None:
            payload = zlib.compress(raw, 9) if entry["flag"] else raw
            entry["raw_size"] = len(raw)
            changes.append(entry["name"])
        position = len(output) - start
        output.extend(payload)
        name_bytes = entry["name_bytes"]
        toc.append(struct.pack("!IIIIBc", 18 + len(name_bytes), position, len(payload), entry["raw_size"], entry["flag"], entry["kind"]) + name_bytes)
    toc_offset = len(output) - start
    toc_data = b"".join(toc)
    output.extend(toc_data)
    output.extend(struct.pack("!8sIIII64s", MAGIC, len(output) - start + 88, toc_offset, len(toc_data), version, library))
    target = RELEASE / (original.stem + "_修复版.exe")
    target.write_bytes(output)
    report = {"original_sha256": ORIGINAL_HASH, "repaired_sha256": hashlib.sha256(output).hexdigest(), "changed_archive_entries": changes, "changed_pyz_modules": modules, "bootloader_unchanged": bytes(output[:start]) == data[:start], "output": str(target)}
    (BUILD / "build_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
