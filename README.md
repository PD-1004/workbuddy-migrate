# WorkBuddy 会话迁移助手

Windows 会话迁移工具，支持按项目选择迁移、会话更新、产物复制和迁移前备份。

原项目源码已遗失。本仓库保存原始 EXE、审计记录，以及可复现的修复构建代码；不应将它误认为恢复完整的原始源码。

## 使用

运行 `修复版/` 下的 EXE。迁移前请完全退出 WorkBuddy。

右上角绿色“更新”按钮位于开发者微信左侧。启动时自动检查版本，发现新版会弹窗；也可点击按钮手动检查。下载页和版本信息来自独立的公开分发仓库，不需要用户登录。更新仅打开下载页，由用户下载并更换程序。

## 构建与验证

使用 Python 3.9，与原 EXE 的字节码版本一致，无需第三方构建依赖：

```powershell
python repair/build_repaired.py
python repair/test_repairs.py
python repair/test_updates.py
```

构建过程校验原始 EXE 的 SHA256，保留原启动器及其他资源，仅修改已确认的问题和更新入口。`audit/自查报告.md` 记录问题依据，`修复版/修复说明.md` 记录修复行为。

## 发布新版

1. 修改 `repair/wb_updates.py` 中的 `VERSION`（三段数字版本号），构建并验证。
2. 在公开分发仓库创建对应版本 Release，上传新版 EXE。
3. 将公开仓库的 `version.json` 更新为相同版本号、对应 Release URL 和更新说明。版本信息必须在安装包可下载后更新。

`repair/update_config.json` 保存公开版本接口和发布仓库地址，不包含账号凭据。本项目仓库与独立的更新下载仓库均公开；下载仓库提供版本信息与 Release 安装包。
