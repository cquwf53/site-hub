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

启动脚本会按需创建 `.venv`、从 `requirements.lock` 安装已锁定的 Python 生产依赖、安装 Node 依赖及 Playwright Chromium、构建 `frontend/dist`，随后启动 SiteHub，并在完整就绪后打开浏览器。再次启动同一数据目录会被单实例锁拒绝。直接依赖输入与生产/开发锁的维护规则，以及隔离数据的一键 Windows 验收命令，见 [测试说明](docs/testing.md)。

日常数据默认位于 `data/`：

- `data/site-hub.db`：SQLite 数据库；
- `data/browser-profiles/`：按账号保存的浏览器 Profile；
- `data/artifacts/`：受控截图等任务产物；
- `data/backups/`：由页面创建并校验的 SQLite 一致性备份；
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

## 网站类型、目录与标签

网站整理采用“一个主类型 + 一个目录 + 多个标签”：主类型用于商城、签到、工具、书签等稳定业务分类，目录保留树状归档位置，标签用于跨目录的多个补充维度。网站列表支持按主类型筛选和批量设置；新建网站、网站详情及“设置 → 网站类型”均可管理这一分类。

系统内置“商城、签到、工具、书签、其他”五种类型。自定义类型可配置名称、稳定标识、颜色、图标、排序和启停；仍被网站引用的类型不能删除，停用类型不能再分配给新网站。旧版 `sites.kind` 会在数据库第 3 版迁移时确定性映射并与正式类型保持同步，因此原有分类不会因升级丢失。

数据库升级到第 3 版后，如需回退到不认识网站类型的旧版 SiteHub，必须同时恢复升级前的第 2 版数据库备份，不能只回退代码。

## 快捷访问与本地使用偏好

总览的“快捷访问”提供“常用”和“最近”两种视图。手动置顶的网站始终优先；其余常用项根据打开次数和最近访问时间的 30 天半衰期自动排序。尚无使用记录时，以置顶和最近添加的网站作为起点。快捷卡片可以打开网站、进入详情、置顶或隐藏；隐藏后可在网站设置中恢复，也可单独清除全部使用记录而保留置顶和隐藏设置。

使用偏好只统计用户通过 SiteHub 的“访问网站”操作。商品链接、签到、商品抓取、详情页浏览和页面刷新不会计入；系统不会读取浏览器历史，也不会保存来源页、IP、User-Agent 或敏感 URL。SQLite 只保留每个网站的聚合次数和最后访问时间，且不会为点击生成活动、收件箱、通知或协议日志。

“从快捷访问隐藏”由数据库第 4 版字段承载。正式库升级前应创建一致性备份；如需回退到第 3 版代码，建议同时恢复升级前的第 3 版数据库备份，避免迁移元数据与旧代码不一致。

## 通知

需要处理的业务事件进入 SiteHub 收件箱。设置页可配置全局事件开关和多个 Webhook 通道；每个通道可筛选事件类型与最低级别。Webhook 端点只在创建或替换时提交，API 和页面只显示“已配置”，不会回显明文。发送由持久化 outbox 执行，支持租约恢复、有限重试和失败事件。

## 商品、分类与调度

商品页支持按网站、分类和库存状态筛选，可打开商品详情查看分页价格历史与按真实时间间隔绘制的价格曲线，并在站点范围内创建、重命名、排序、删除和分配商品分类。商品可跟随抓取分类，也可设置不会被后续同步覆盖的人工分类。

设置页的“调度”页面统一管理签到窗口、商品同步周期、能力启停、当前任务、上次最终结果和错误摘要，并可跳转到站点或账号的覆盖配置。`manual`/`none` 商品能力可保持启用以保留迁移状态，但不会进入自动抓取队列；页面会明确标注“手工目录”或“未配置抓取器”。商品同步并发固定受 Python 控制面限制。

## 本地数据库备份与恢复

设置页的“数据”页面可创建、列出和校验数据库备份。每份备份都使用 SQLite Backup API 生成一致性快照，并记录大小和 SHA-256；显式校验还会执行 SQLite `integrity_check`。

恢复不会热覆盖运行中的数据库。页面需要输入完整备份 ID 才能安排恢复；SiteHub 在下一次启动取得单实例锁后、打开数据库前执行恢复，并先创建当前数据库的 `pre_restore` 回滚备份。恢复标记只会在数据库迁移、任务恢复和通知恢复全部成功后清除；启动验证失败会在仍持有单实例锁时自动装回回滚备份。浏览器 Profile、截图和日志不包含在数据库备份中，仍需保留原数据目录或单独复制。

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
