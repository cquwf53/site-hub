# SiteHub

SiteHub 是面向 Windows 本机单用户的网站资产与自动化中心，用目录和标签管理网站，并统一承载收藏、商品监控、多账号签到、任务历史、收件箱和 Webhook 通知。

当前版本固定监听 `http://127.0.0.1:8686`。Vue 页面和 `/api/v1` 均由 FastAPI 提供，生产运行不需要 Vite 端口。

## 先说明“一个端口、一个进程”

正式运行唯一长期业务监听是 FastAPI 的 `127.0.0.1:8686`，Node 签到 Worker 本身不监听端口。但这不是只有一个操作系统进程：FastAPI 会监管无端口的 Node Worker，签到或登录时还可能出现 Chrome、Playwright、OCR 子进程。少数保留 CDP 的专用适配器运行期间，Chrome 会临时监听一个仅限 `127.0.0.1` 的随机调试端口；它不是 SiteHub 的业务入口，任务结束时会随 Chrome 一并清理。停止 SiteHub 时，监管器负责清理这些子进程。

SQLite 的业务写入只由 Python 控制面完成；Node Worker 通过标准输入/输出 JSON-RPC 执行浏览器任务，不直接访问业务数据库。

## 启动

环境要求：Windows、Python 3.10–3.13、Node.js 20.19+ 或 22.12+。首次准备依赖需要网络连接。

```bat
start.bat
```

启动脚本会按需创建 `.venv`、安装 Python/Node 依赖及 Playwright Chromium、构建 `frontend/dist`，随后启动 SiteHub，并在完整就绪后打开浏览器。再次启动同一数据目录会被单实例锁拒绝。

日常数据默认位于 `data/`：

- `data/site-hub.db`：SQLite 数据库；
- `data/browser-profiles/`：按账号保存的浏览器 Profile；
- `data/artifacts/`：受控截图等任务产物；
- `data/logs/`：运行日志。

这些目录可能包含敏感信息，不应提交到 Git 或随意复制。Webhook 端点和 URL 查询参数等秘密由 Windows DPAPI CurrentUser 加密，只能由同一 Windows 用户解密。

可选的登录启动任务不会自动安装。完成验收后才可运行 `tools\install-startup.ps1`。停止服务先运行 `tools\stop-sitehub.ps1`，确认停止后再用 `tools\uninstall-startup.ps1` 移除登录任务；卸载脚本本身不会停止仍在运行的实例。

停止前可执行只读审计：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\stop-sitehub.ps1 -AuditOnly
```

停止脚本只接受本项目根目录注册的 `SiteHub` 任务，或由本项目 `.venv\Scripts\python.exe -m site_hub.main` 持有的 8686 监听。它会先核验任务 action、Windows 用户、进程路径和命令行；遇到同名外部任务或非本项目端口 owner 时默认不做任何停止操作。`-Force` 只能忽略同名外部任务，不能停止或删除它。

## 主要目录

- `site_hub/`：FastAPI、SQLite 数据层、调度、任务队列、通知和 Worker 监管；
- `worker-node/`：签到/登录适配器与无端口 Node Worker；
- `frontend/`：Vue 3 + TypeScript 桌面界面；
- `tools/`：启动辅助、Windows 冒烟测试和旧项目迁移工具；
- `tests/`：Python 测试；
- `docs/`：架构、测试和迁移说明。

详细设计见 [架构说明](docs/architecture.md)，验证命令见 [测试说明](docs/testing.md)。

## 通知

需要处理的业务事件进入 SiteHub 收件箱。设置页可配置全局事件开关和多个 Webhook 通道；每个通道可筛选事件类型与最低级别。Webhook 端点只在创建或替换时提交，API 和页面只显示“已配置”，不会回显明文。发送由持久化 outbox 执行，支持租约恢复、有限重试和失败事件。

## 旧项目迁移与切换边界

迁移工具支持 `analyze`、`dry-run` 和 `apply`。建议先执行完整演练：

```powershell
.\.venv\Scripts\python.exe tools\migrate_legacy.py dry-run `
  --vshop-db C:\path\to\vshop.db `
  --autocheckin-db C:\path\to\checkin.db `
  --target-db C:\path\to\site-hub.db
```

`dry-run` 会在一次性数据库中完成真实导入、DPAPI、外键和明文检查，然后删除演练目标；不会初始化指定的正式目标。完整参数、Profile 清单与恢复方式见 [迁移说明](docs/legacy-migration.md)。

SiteHub 不会自动停止旧服务、注销旧的 `AutoCheckin Hub` 计划任务、注册新开机任务或启用迁入的旧调度。正式 `apply` 和切换必须在计数、外键、密钥、Profile、账号登录、关键界面及 Windows 冒烟测试全部通过后由操作员执行。

切换期间旧项目、旧数据库和旧 Profile 保持只读，至少保留 7 天且不少于两个成功自动运行周期。新系统已经执行过签到后若回滚，旧签到调度应暂停到下一个上海自然日，避免重复签到。
