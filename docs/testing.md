# SiteHub 测试与验收

以下命令均在 SiteHub 项目根目录运行。自动化测试通过是发布门槛之一，不等于真实账号登录、Profile 或正式切换已经验收。

## Python 测试

`start.bat` 安装运行依赖；测试前还需安装开发依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
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
