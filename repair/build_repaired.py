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


LOAD_TARGETS_OLD = """function loadTargets(){
  TG.loading = true; renderTargets();
  get("accounts").then(function(r){
    TG.loading = false;
    if(r.ok){
      TG.accounts = r.accounts || [];
      if(!TG.uid && TG.accounts.length) TG.uid = TG.accounts[0].uid;
    }
    renderTargets();
  }).catch(function(){ TG.loading=false; renderTargets(); });
}"""

LOAD_TARGETS_NEW = """function loadTargets(){
  TG.loading = true; renderTargets();
  get("accounts").then(function(r){
    TG.loading = false;
    if(r.ok){
      TG.accounts = r.accounts || [];
      if(!TG.uid && TG.accounts.length) TG.uid = TG.accounts[0].uid;
      TG.err = TG.accounts.length ? "" : "本机还没读到已登录的账号：先启动一次 WorkBuddy 并登录，再回到这里点「刷新」。";
    }else{
      // 原版这里把 r.text 直接丢了，用户只看到空列表、不知道为什么
      TG.accounts = [];
      var msg = r.text || "读取本机账号失败";
      if(TG.err !== msg) toast(msg, true);
      TG.err = msg;
    }
    renderTargets();
  }).catch(function(){
    TG.loading = false;
    var msg = "读取本机账号失败：本地服务没有响应";
    if(TG.err !== msg) toast(msg, true);
    TG.err = msg;
    renderTargets();
  });
}"""

PICK_EMPTY_OLD = """    else if(!TG.accounts.length) h += '<div class="empty">本机会话库里还没读到账号。<br>第一次用 WorkBuddy 登录后再来检查。</div>';"""

PICK_EMPTY_NEW = """    else if(!TG.accounts.length) h += '<div class="empty">'+(TG.err ? esc(TG.err).replace(/\\n/g,"<br>") : '本机会话库里还没读到账号。<br>第一次用 WorkBuddy 登录后再来检查。')+'</div>';"""

SUMMARY_EMPTY_OLD = """  if(!TG.accounts.length){ sum.textContent = "未选择 —— 在上方「打包范围」旁的弹窗里挑账号"; return }"""

SUMMARY_EMPTY_NEW = """  if(!TG.accounts.length){ sum.textContent = TG.err || "未选择 —— 在上方「打包范围」旁的弹窗里挑账号"; return }"""


def fix_accounts_ui(html):
    """Issue #1：账号列表为空或读取失败时，前端不再静默吞掉原因。"""
    for old, new in (
        (LOAD_TARGETS_OLD, LOAD_TARGETS_NEW),
        (PICK_EMPTY_OLD, PICK_EMPTY_NEW),
        (SUMMARY_EMPTY_OLD, SUMMARY_EMPTY_NEW),
    ):
        assert html.count(old) == 1, "wb_ui.html 结构已变化，账号提示补丁无法应用：%r" % old[:48]
        html = html.replace(old, new, 1)
    return html


def guard_version_requests(html):
    # Discard responses belonging to selection state replaced by a profile switch.
    for name, state, stale in (
        ("loadAccounts", "PK", "versionState!==PK"),
        ("pickAccount", "PK", "versionState!==PK||PK.uid!==uid"),
        ("loadAllPreview", "PK", "versionState!==PK"),
        ("loadTargets", "TG", "versionState!==TG"),
    ):
        start = html.index("function " + name + "(")
        end = html.index("\nfunction ", start)
        section = html[start:end]
        signature_end = section.index("{\n") + 2
        section = section[:signature_end] + "  var versionState=" + state + ";\n" + section[signature_end:]
        if name == "loadAllPreview":
            anchors = ("  function next(){",)
        else:
            anchors = (".then(function(r){", ".catch(function(){")
        for anchor in anchors:
            assert section.count(anchor) == 1, (name, anchor)
            section = section.replace(anchor, anchor + "\n    if(" + stale + ")return;", 1)
        html = html[:start] + section + html[end:]
    return html


"""布局补丁的锚点。全部带 assert count==1，原程序结构一变就立刻报错。"""

# 标题旁的帮助图标：说明文字收进悬浮提示，平时不占版面
HELP_DOT = ('<span class="helpwrap"><button type="button" class="helpdot" '
            'aria-label="查看说明" aria-describedby="{tip_id}">'
            '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">'
            '<line x1="12" y1="17" x2="12" y2="17"/><path d="M12 13.5V11a2 2 0 1 0-2-2"/></svg>'
            '</button><span class="helptip" id="{tip_id}" role="tooltip">{tip}</span></span>')

# ① 源机路径卡片：标题行（右侧留给版本切换）+ 去掉说明行（信息已在悬浮提示里）
SRC_HEAD_OLD = """          <h3>本机（源机）的两个存储路径</h3>
          <div class="desc">程序已按 WorkBuddy【设置 - 通用】自动识别并锁定；识别错了点右侧锁图标解锁编辑。</div>
          <div id="src-paths"></div>"""

SRC_HEAD_NEW = """          <div class="cardhead"><div class="htitle"><h3>本机（源机）的两个存储路径</h3>__SRC_TIP__</div>
            <div class="verhost" id="ver-host-src"></div></div>
          <div id="src-paths"></div>"""

# ② 现机路径卡片：同上
DST_HEAD_OLD = """          <h3>本机（现机）的两个存储路径</h3>
          <div class="desc">导入时会把源机路径改写成本机路径，这两个值必须准确；读不到或识别错了点右侧锁图标解锁编辑。</div>
          <div id="dst-paths"></div>"""

DST_HEAD_NEW = """          <div class="cardhead"><div class="htitle"><h3>本机（现机）的两个存储路径</h3>__DST_TIP__</div>
            <div class="verhost" id="ver-host-dst"></div></div>
          <div id="dst-paths"></div>"""

# ③ 「选择要迁走的数据」：说明行改成标题旁的感叹号图标
PACK_HEAD_OLD = """          <h3>选择要迁走的数据</h3>
          <div class="desc" id="pack-desc">在「打包范围」里选全部或指定，选定后会弹出窗口确认要打包的内容。</div>"""

PACK_HEAD_NEW = """          <div class="cardhead"><div class="htitle"><h3>选择要迁走的数据</h3>__PACK_TIP__</div></div>
          <div class="desc" id="pack-desc" hidden></div>"""

# ④ 「打包范围」改成垂直字段：标签在上、下拉框在下，宽度撑满
SCOPE_ROW_OLD = """          <div class="prow selrow" id="scope-row">
            <div class="prow-inner"><span class="plabel">打包范围</span>
              <div class="seldrop" id="scope-drop">"""

SCOPE_ROW_NEW = """          <div class="prow vfield selrow" id="scope-row">
            <span class="plabel">打包范围</span>
            <div class="prow-inner">
              <div class="seldrop" id="scope-drop">"""

# ⑤ 收起 scope 字段后，让「携带技能」独立成同样结构的垂直字段行
SCOPE_END_OLD = """              </div></div>
          </div>
          <div id="skills-src"></div><div id="dest-row"></div>"""

SCOPE_END_NEW = """              </div>
            </div>
          </div>
          <div class="prow vfield" id="skills-src"></div>
          <div id="dest-row"></div>"""

# ⑥ 两个路径行压缩上下留白
PATH_ROW_OLD = """  return '<div class="prow locked" id="row-'+id+'">'+"""
PATH_ROW_NEW = """  return '<div class="prow locked pcompact" id="row-'+id+'">'+"""

PATH_EDIT_OLD = """  return '<div class="prow editing" id="row-'+id+'">'+"""
PATH_EDIT_NEW = """  return '<div class="prow editing pcompact" id="row-'+id+'">'+"""

TIPS = {
    "__SRC_TIP__": ("tip-src-path",
                    "按 WorkBuddy【设置 - 通用】自动识别并锁定。<br>"
                    "识别错了：点右侧锁图标解锁编辑，或用标题右侧的"
                    "<b>国内版 / 国际版</b> 一键换回该版本的默认路径。"),
    "__DST_TIP__": ("tip-dst-path",
                    "导入时会把源机路径改写成本机路径，这两个值必须准确。<br>"
                    "读不到或识别错了：点右侧锁图标解锁编辑，或用标题右侧的"
                    "<b>国内版 / 国际版</b> 切换。"),
    "__PACK_TIP__": ("tip-pack",
                     "在「打包范围」里选<b>全部打包</b>或<b>指定打包</b>，"
                     "选定后会弹出窗口确认要打包的内容。<br>"
                     "两项都定了（账号、会话）才能点「一键拷贝」。"),
}


def fix_layout(html):
    """压缩路径行、版本切换提到标题行、字段改垂直堆叠、说明收进悬浮提示。"""
    for old, new in (
        (SRC_HEAD_OLD, SRC_HEAD_NEW),
        (DST_HEAD_OLD, DST_HEAD_NEW),
        (PACK_HEAD_OLD, PACK_HEAD_NEW),
        (SCOPE_ROW_OLD, SCOPE_ROW_NEW),
        (SCOPE_END_OLD, SCOPE_END_NEW),
        (PATH_ROW_OLD, PATH_ROW_NEW),
        (PATH_EDIT_OLD, PATH_EDIT_NEW),
    ):
        assert html.count(old) == 1, "wb_ui.html 结构已变化，布局补丁无法应用：%r" % old[:56]
        html = html.replace(old, new, 1)
    # 占位符是在上面的替换里插进来的，所以这里才展开成图标 + tooltip
    for token, (tip_id, tip) in TIPS.items():
        assert html.count(token) == 1, "布局补丁占位符丢失：%s" % token
        html = html.replace(token, HELP_DOT.format(tip_id=tip_id, tip=tip), 1)
    return html


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


def wrap(code, installer, run_main=False, worker_source=None):
    if run_main or worker_source is not None:
        code = code.replace(co_consts=tuple("__wb_before_repair__" if x == "__main__" else x for x in code.co_consts))
    source = "import marshal as _repair_marshal\nexec(_repair_marshal.loads(%r), globals())\n" % marshal.dumps(code)
    if worker_source is not None:
        return compile(source + worker_source, code.co_filename, "exec")
    source += "from wb_fixes import %s as _repair_install\n_repair_install(globals())\n" % installer
    if run_main:
        source += "from wb_updates import install as _update_install\n_update_install(globals())\n"
        source += "if __name__ == '__main__':\n    main()\n"
    return compile(source, code.co_filename, "exec")


def rebuild_pyz(data, build_dir=BUILD):
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
            (build_dir / (name + ".bin")).write_bytes(raw)
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


def main(build_dir=BUILD, release_dir=RELEASE, worker_source=None):
    assert sys.version_info[:2] == (3, 9), "Use Python 3.9 to match the original EXE"
    original = next(ROOT.glob("*.exe"))
    data = original.read_bytes()
    assert hashlib.sha256(data).hexdigest() == ORIGINAL_HASH, "Original EXE changed; do not apply offset patches"
    build_dir.mkdir(exist_ok=True)
    release_dir.mkdir(exist_ok=True)
    start, version, library, entries = archive(data)
    output, toc, changes = bytearray(data[:start]), [], []
    for entry in entries:
        payload = entry["payload"]
        if entry["name"] == "PYZ.pyz":
            raw, modules = rebuild_pyz(unpack(entry), build_dir)
        elif entry["name"] == "wb_web":
            code = transform(marshal.loads(unpack(entry)), "web")
            (build_dir / "wb_web_patched.bin").write_bytes(marshal.dumps(code))
            raw = marshal.dumps(wrap(code, "install_web", run_main=True, worker_source=worker_source))
            (build_dir / "wb_web.bin").write_bytes(raw)
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
            html = guard_version_requests(fix_accounts_ui(html))
            html = fix_layout(html)
            html = html.replace('<div class="wxwrap" id="wxwrap">', '<div class="header-actions"><button class="update-button" id="update-button" hidden onclick="checkUpdate(true)">更新</button><div class="wxwrap" id="wxwrap">', 1)
            html = html.replace('</header>', '</div></header>', 1)
            html = html.replace('</body>', (HERE / "update_ui.html").read_text(encoding="utf-8") + '\n</body>', 1)
            html = html.replace('</body>', (HERE / "layout_ui.html").read_text(encoding="utf-8") + '\n</body>', 1)
            html = html.replace('</body>', (HERE / "version_ui.html").read_text(encoding="utf-8") + '\n</body>', 1)
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
    target = release_dir / (original.stem + "_修复版.exe" if worker_source is None else "migration-worker.exe")
    target.write_bytes(output)
    report = {"original_sha256": ORIGINAL_HASH, "repaired_sha256": hashlib.sha256(output).hexdigest(), "changed_archive_entries": changes, "changed_pyz_modules": modules, "bootloader_unchanged": bytes(output[:start]) == data[:start], "output": target.relative_to(ROOT).as_posix()}
    (build_dir / "build_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, indent=2))
