# SiteHub 测试与验收

以下命令均在 SiteHub 项目根目录运行。自动化测试通过是发布门槛之一，不等于真实账号登录、Profile 或正式切换已经验收。

## Windows 统一验收（推荐）

端口 `8686` 空闲时，默认命令会依次安装锁定依赖、运行 Python/Node/Vue 测试、OpenAPI 类型漂移检查、Vue 类型检查、生产构建，并调用 Windows smoke 完成隔离启动、健康检查、Worker 恢复和进程清理：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\verify.ps1
```

所有测试进程都会收到系统临时目录下的独立 `SITEHUB_DATA_DIR`；脚本不会把项目的正式 `data/` 用作测试数据。若 `8686` 已被 SiteHub 或其他程序占用，脚本会在运行门槛前明确失败并报告 PID。需要在服务在线时先运行其余门槛，可显式跳过启动 smoke：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\verify.ps1 `
  -SkipDependencyInstall -SkipSmoke
```

`-SkipDependencyInstall` 只适用于 `.venv`、Node workspace 和浏览器运行时已经准备完整的情况；任何原生命令非零退出都会使统一脚本立即失败。

`-SkipSmoke` 仅跳过固定 8686 端口的启动检查，统一脚本仍会构建 `frontend/dist`。若在线服务运行旧代码，而工作区已修改为新版本，请使用下方“正式服务在线时的隔离浏览器验收”流程，把新产物输出到独立目录。

## Python 依赖锁

- `requirements.txt`、`requirements-dev.txt` 只保存生产/开发直接依赖输入；
- `requirements.lock` 锁定生产直接及传递依赖，`start.ps1` 和生产 smoke 只从该文件安装；
- `requirements-dev.lock` 包含生产锁及测试传递依赖，供统一验收和本地测试安装；
- 安装使用 `--no-deps --only-binary=:all:`，随后执行 `pip check`，避免安装时重新解析未锁定的传递版本。

运行环境要求 64 位 Windows CPython 3.11–3.13；代码使用 `datetime.UTC` 和 `StrEnum`，因此启动器会拒绝 Python 3.10。现有锁文件保留早期 3.10 的版本条件以避免无关依赖变更，这些条件不代表应用支持 3.10。生产锁用 `python_version` marker 保存逐版本 Windows wheel 解析结果；不能用任意单个 `.venv` 的裸 `pip freeze` 覆盖它。修改直接依赖时，必须分别重新解析 3.11、3.12、3.13，更新两个锁文件，并运行统一验收。

## Python 测试

`start.bat` 从生产锁安装运行依赖；单独运行测试前还需安装开发锁：

```powershell
.\.venv\Scripts\python.exe -m pip install --no-deps --only-binary=:all: -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q -m "not integration and not profile"
```

常规套件覆盖数据库事务与外键、任务状态/租约/幂等、调度、API 安全、系统任务、商品抓取、通知 outbox、Worker 监管和迁移逻辑。

带标记的测试不会默认承担日常快速验证：

```powershell
# 需要本机存在已审计的旧数据库，只读执行
.\.venv\Scripts\python.exe -m pytest -q -m integration

# 对旧浏览器 Profile 做只读 SHA-256 清单，耗时且磁盘读取量较大
.\.venv\Scripts\python.exe -m pytest -q -m profile
```

运行真实数据测试前，应确认旧服务和浏览器不会同时写入源数据。测试不得修改旧项目、旧数据库或旧 Profile。

## Node Worker

```powershell
npm.cmd run test:worker
```

Worker 测试使用 Node 内置测试运行器，覆盖 JSON Lines 拆包与协议污染、握手、适配器、取消/超时、Profile 路径与锁、Chrome/CDP/OCR 清理，以及结果不确定时的核对语义。测试不应打开业务数据库或监听端口。

## Vue 前端

```powershell
npm.cmd run test:frontend
npm.cmd run typecheck
npm.cmd run build
```

Vitest/Vue Test Utils 覆盖 API 客户端、任务跟踪、路由、动态 Schema 秘密字段、账号计划和通知通道等交互。`typecheck` 执行 Vue TypeScript 检查，`build` 再执行类型检查并生成 `frontend/dist`。

可用根命令一次运行两个 JavaScript 工作区的测试：

```powershell
npm.cmd test
```

该命令不替代单独的 `typecheck` 和生产构建。

## 迁移演练

迁移必须先分析和演练，不能把 `apply` 当测试命令：

```powershell
.\.venv\Scripts\python.exe tools\migrate_legacy.py analyze `
  --vshop-db C:\path\to\vshop.db `
  --autocheckin-db C:\path\to\checkin.db

.\.venv\Scripts\python.exe tools\migrate_legacy.py dry-run `
  --vshop-db C:\path\to\vshop.db `
  --autocheckin-db C:\path\to\checkin.db `
  --target-db C:\path\to\site-hub.db
```

`analyze` 不创建目标；`dry-run` 在临时数据库中执行完整导入并检查 `integrity_check`、外键、Legacy 映射和敏感 URL 明文，然后删除临时目标。可增加 `--include-profile-inventory --profile-source <目录>` 只读计算 Profile 清单。

只有冻结源数据、计数和哈希均确认后才允许正式 `apply`。若计数与审计基线不同，默认会中止；`--allow-count-drift` 是需要人工解释和复核的例外开关，不应为“让命令通过”而使用。

## Windows 进程验收

在端口 `8686` 空闲的 Windows 环境运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\smoke-windows.ps1
```

依赖和前端已经准备好时，可缩短为：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\smoke-windows.ps1 `
  -SkipDependencyInstall -SkipFrontendBuild
```

脚本使用系统临时目录中的隔离数据，验证：

- 空闲进程树只有 `127.0.0.1:8686` 一个长期业务监听；保留 CDP 的适配器执行时允许 Chrome 临时增加仅限回环的随机调试监听；
- 同数据目录的第二实例被拒绝；
- 杀死 Node Worker 后控制面、Vue 页面和商品 API 仍可用，Worker 能恢复；
- 停止控制面后，受跟踪的 Node/Chrome/OCR 子进程与端口不会残留。

脚本不会验证真实签到成功、每个导入 Profile 可登录或所有第三方站点仍兼容；这些仍需逐账号人工/端到端验收。

### 正式服务在线时的隔离浏览器验收

`tools/smoke-isolated.py` 使用系统临时数据目录和另一个回环端口，可在正式 8686 服务继续运行时验收新版本。依赖需提前准备完整：项目 Python 环境、Node workspace 与 Playwright Chromium。工具不安装依赖，也不构建前端。

将新版前端构建到项目内独立目录，再指定该目录验收：

```powershell
npm.cmd run typecheck
npm.cmd exec --workspace=frontend -- vite build --outDir ../.pytest-runs/optimization-dist
.\.venv\Scripts\python.exe tools\smoke-isolated.py `
  --frontend-dir .pytest-runs\optimization-dist
```

`--frontend-dir` 可省略，默认上述 `optimization-dist`，目录必须已有 `index.html`。默认自动选择空闲 `127.0.0.1` 端口；可用 `--port` 指定测试端口，工具明确拒绝 8686。可用 `--node` 指定 Node 可执行文件。生产启动器的监听地址和端口保持固定。

工具检查真实后台服务的 live/ready、同数据目录第二实例拒绝、Worker 丢失期间控制面可读及自动恢复，并验证维护配置读写、CSRF、手动备份和完整性状态。随后运行真实浏览器的路由、Worker 状态灯和响应式布局检查，最后确认服务优雅关闭、Windows Job 活动进程数为 0 且已跟踪 PID 无残留。

所有测试数据库、日志、备份和截图保存在系统临时目录 `sitehub-isolated-smoke-*`，结束时输出该目录与 `report.json` 路径供复核。工具优先通过控制文件优雅关闭测试服务，并使用 Windows Job Object 和 PID/启动时间核对清理自己创建的进程；报告记录 `remaining_processes`。临时报告目录会保留，不会清理正式 `data/` 或替换 `frontend/dist`。

### `start.bat` 启动器专项验收

依赖、Playwright Chromium 和前端构建已准备好时，运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\smoke-launcher.ps1 `
  -UseVerifiedSetup
```

该脚本从带空格的系统临时项目联接实际调用 `start.bat`，并使用独立的 `SITEHUB_DATA_DIR`。它先在另一个带空格的临时项目副本中验证损坏 `.venv` 可安全重建且真实 `.venv` 哈希不变，然后验证就绪、启动器级第二实例在依赖/构建写入前快速退出，以及停止精确测试实例后 `8686`、Node/Chrome/OCR 和临时目录均无残留。进程停止前同时核对 PID、启动时间和完整命令行，不能仅凭 PID 或进程名停止进程。

省略 `-UseVerifiedSetup` 时，启动器会按正常首次启动路径准备依赖和构建，可能需要网络连接。已验证模式只在依赖完整、两个 Chromium 可执行文件存在且前端产物存在时写入与当前清单对应的哈希标记；它不会绕过 `start.ps1` 的启动互斥。

正式数据实例的停止边界可先做只读检查，不应把 Windows 冒烟测试的临时 PID 当作正式进程：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\stop-sitehub.ps1 -AuditOnly
```

审计必须验证精确任务 action、当前 Windows 用户、本项目 `.venv` 进程路径和 `-m site_hub.main` 命令行。安全负例是：伪造额外 action、同名其他任务或让其他程序占用 8686 时，脚本应在任何 `Stop-ScheduledTask` / `Stop-Process` 调用前拒绝。

## 发布与切换清单

正式替代旧项目之前至少确认：

1. Python、Node Worker、Vue 测试和类型/构建全部通过；
2. Windows 冒烟测试通过，启动与停止无残留；
3. 两个旧项目各自的原有基线测试在只读代码状态下仍通过；
4. 真实数据库 `dry-run` 的计数、外键、Legacy 映射和明文扫描通过；
5. Profile 复制清单校验通过，逐账号验证登录，失败账号保持禁用；
6. 关键网站、商品同步、签到、任务恢复、收件箱和 Webhook 进行端到端抽查；
7. 旧项目、数据库和 Profile 已制作可恢复快照并转为只读保留。

迁移工具和 `start.bat` 都不会自动注销旧的 `AutoCheckin Hub` 任务、注册 SiteHub 开机任务或开启迁入调度。完成上述门槛后再人工切换。旧资产至少保留 7 天且不少于两个成功自动运行周期；若新系统已签到后回滚，旧签到调度暂停到下一个上海自然日。

## 维护验收

`tests/test_maintenance.py` 使用临时 SQLite 数据和文件，覆盖计划跨重启保存、自动补执行、默认不删除、备份失败阻止清理、恢复标记保护、手动/回滚备份保护、活动任务截图、哈希异常与路径边界、活动日志、每商品最新价格保留，以及 API 参数校验和并发维护拒绝。生产数据目录不得作为测试目录。

维护设置位于 `/api/v1/settings/maintenance`，状态位于 `/api/v1/maintenance/status`，显式执行位于 `POST /api/v1/maintenance/run`。修改设置和显式执行与其他浏览器写请求一样经过 CSRF 校验。启用清理策略是实际数据维护操作；验证时应先在独立临时目录准备过期/活动文件并检查保护边界。

## API 类型契约

前端的 `frontend/src/api/generated.ts` 从 FastAPI OpenAPI 模型生成。修改请求或响应模型后执行生成，并将产物与后端变更一并保存：

```powershell
npm.cmd run api:generate
npm.cmd run api:check
```

生成器仅构造 OpenAPI，不启动服务或初始化数据库。`tools/verify.ps1` 使用选定的 Python 环境执行 `tools/generate-api-types.py --check`；契约与生成文件不一致即失败，随后才进行前端测试和构建。不要手工修改生成文件。使用 `-PythonPath` 时，统一验收仍使用该解释器进行契约检查；单独的 npm 命令使用项目 `.venv`。

## 本轮功能与验证

接口契约由 OpenAPI 生成 DTO，并通过 `api:check` 检测漂移；Store 拆分为网站、账号、商品、任务、计划、活动六个模块后继续运行原有数据库/API 回归测试，验证对外行为与事务约束。常规启动升级旧数据库前自动创建受保护的 `Before schema upgrade` 手动快照，升级测试检查快照保留原 schema 和数据。

`tests/test_monitoring_optimizations.py` 覆盖同站商品任务串行、跨站并行，降价阈值累计与逐商品冷却、不同成功任务的库存确认、未知库存不产生补货、目录骤减/错误页面保留原目录，以及相关通知合并。`frontend/src/stores/tasks.test.ts` 验证 SSE 与快照轮询协作、过期请求不覆盖终态和重试事件隔离；其余前端交互由 Vue 测试与浏览器 smoke 验证。自动化通过后仍需逐账号确认真实第三方站点兼容性。
