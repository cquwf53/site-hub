# SiteHub Windows 切换手册

本手册用于从 VShop 与 AutoCheckin Hub 一次性切换到 SiteHub。切换前的开发、分析和演练不会修改旧项目；正式切换必须由操作者明确执行，任何工具都不会自动停服务或注销旧任务。

## 工具边界

- `tools/cutover-preflight.ps1` 默认完全只读。显式提供 `-ReceiptPath` 时，它仍先完成所有只读检查，并且只在零阻断项时原子写入一份短时效切换收据；它不会创建探针文件、停止进程、修改任务、复制 Profile 或运行迁移。
- `tools/migrate_legacy.py analyze` 只分析源库；`dry-run` 将完整迁移写入系统临时数据库后删除；`apply` 才会写目标数据库和可选的 Profile 目标。
- 迁移器不会替操作者停止旧服务、Chrome、Node 或 OCR。正式冻结前必须手工确认这些进程的归属。
- `tools/install-startup.ps1` 使用 `-Force` 注册或覆盖新的 `SiteHub` 登录任务。只有新系统验收完成后才能执行。
- `tools/uninstall-startup.ps1` 只删除新的 `SiteHub` 任务，不处理旧 `AutoCheckin Hub` 任务。
- `start.bat` 会创建 Python 环境、安装 Python/Node/Playwright 依赖并构建前端。依赖准备必须在冻结窗口之前完成。

## 1. 冻结前准备

先完成依赖安装、前端构建、迁移演练和 Windows smoke。此阶段旧服务可以继续运行。不要直接用 `start.bat` 做准备，因为它在完成安装后会启动默认正式数据目录；让 smoke 使用隔离临时数据完成同样的依赖准备和构建。

```powershell
Set-Location E:\myprojects\site-hub
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\smoke-windows.ps1 `
  -StartupTimeoutSeconds 120

powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\smoke-launcher.ps1 `
  -UseVerifiedSetup
```

首次 `smoke-windows.ps1` 会创建 `.venv`、安装依赖、构建前端，然后使用独立的系统临时数据目录完成验收并自动清理；它不应指向正式 `data`。后续复跑可以增加 `-PythonPath .\.venv\Scripts\python.exe -SkipDependencyInstall -SkipFrontendBuild`。`smoke-launcher.ps1` 额外验证真实 `start.bat`、损坏 `.venv` 自愈、重复启动早退和精确进程清理。完成后确认 8686 已释放。

运行只读预检。路径均可覆盖：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\cutover-preflight.ps1 `
  -SiteHubProject E:\myprojects\site-hub `
  -VShopProject C:\Users\31587\.gemini\antigravity\scratch\vshop_monitor `
  -AutoCheckinProject E:\myprojects\auto-checkin-hub `
  -TargetData E:\myprojects\site-hub\data `
  -ReceiptPath E:\myprojects\site-hub\.runtime-state\cutover-receipt.json
```

返回码 `0` 表示没有阻断项；返回码 `2` 表示仍有阻断项。警告需要人工判断，但不会单独令脚本失败。预检显示 Chrome、Node 或 OCR 时，必须先确认 PID 归属，禁止按进程名批量结束其他应用的进程。

## 2. 只读分析与完整演练

```powershell
$python = 'E:\myprojects\site-hub\.venv\Scripts\python.exe'
$migrate = 'E:\myprojects\site-hub\tools\migrate_legacy.py'
$vshopDb = 'C:\Users\31587\.gemini\antigravity\scratch\vshop_monitor\data\vshop.db'
$checkinDb = 'E:\myprojects\auto-checkin-hub\data\checkin.db'
$profiles = 'E:\myprojects\auto-checkin-hub\data\browser-profiles'
$targetDb = 'E:\myprojects\site-hub\data\site-hub.db'
$receipt = 'E:\myprojects\site-hub\.runtime-state\cutover-receipt.json'

& $python $migrate analyze `
  --vshop-db $vshopDb --autocheckin-db $checkinDb `
  --include-profile-inventory --profile-source $profiles

& $python $migrate dry-run `
  --vshop-db $vshopDb --autocheckin-db $checkinDb --target-db $targetDb `
  --include-profile-inventory --profile-source $profiles
```

演练必须满足基线计数、`integrity=ok`、零外键错误、零明文敏感 URL 命中。计数漂移需要先解释并重新冻结基线，不要直接使用 `--allow-count-drift` 绕过。

## 3. 正式冻结

选择没有自动签到或商品同步正在执行的窗口：

1. 禁用旧 `AutoCheckin Hub` 登录任务，保留任务定义以便审计。
2. 正常停止两套旧服务。
3. 关闭使用旧 Profile 的 Chrome、Playwright、CDP 和 OCR 子进程。
4. 确认旧数据库没有非空 WAL，AutoCheckin 锁文件已消失，旧 Profile 没有 `Singleton*` 或 `DevToolsActivePort`。
5. 确认目标 `data` 为空、VShop、AutoCheckin 与 SiteHub 三个 Git 工作树均已提交且干净，并重新运行预检直到零阻断项。
6. 在准备立即执行正式迁移的同一次零阻断预检中使用 `-ReceiptPath $receipt` 生成收据；收据默认 30 分钟失效，超时或源库哈希变化后必须重新预检。

计划任务命令会改变系统状态，必须由操作者在冻结窗口明确执行：

```powershell
Disable-ScheduledTask -TaskName 'AutoCheckin Hub'
Get-ScheduledTask -TaskName 'AutoCheckin Hub' | Format-List TaskName,State
```

若任务不存在，记录这一事实即可。不要结束无法确认归属的 Chrome、Node 或 Python 进程。

## 4. 正式应用

只有冻结后的预检与演练均通过时才运行：

```powershell
& $python $migrate apply `
  --vshop-db $vshopDb --autocheckin-db $checkinDb --target-db $targetDb `
  --preflight-receipt $receipt `
  --copy-profiles --profile-source $profiles `
  --profile-target 'E:\myprojects\site-hub\data\browser-profiles'
```

迁移器在首次创建目标前验证收据，并在业务导入后再次验证源路径、SHA-256 和 WAL 冻结状态。初始化或业务导入失败时，只删除本次独占创建的目标 DB/WAL/SHM，因此可在收据有效期内安全重试。Profile 阶段中断时，成功导入的数据库与收据绑定会保留；修复 Profile 源后使用 `--profiles-only` 恢复，禁止重复导入业务数据。恢复阶段不再接收短时效收据，而是从目标数据库读取已持久化绑定，并重新核对源库哈希及 Profile 源/目标路径。

## 5. 切换验收

正式数据迁移后先保持新调度禁用：

1. 校验迁移报告、数据库完整性、外键、Legacy 映射和计数。
2. 扫描目标数据库，确认敏感 URL 查询参数、Cookie、Token、Authorization 和 Webhook 无明文。
3. 启动 SiteHub，核对目录、标签、网站、商品、历史、批次、账号和事件。
4. 逐账号验证复制的 Profile。初始 `unverified` 属正常状态；验证失败的账号必须保持禁用并重新登录。
5. 执行一次商品同步和受控签到 E2E；确认唯一长期业务监听是 `127.0.0.1:8686`、CDP 任务的随机端口仅绑定回环且任务后释放、第二实例被拒绝、Worker 可恢复、停止后无子进程残留。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\smoke-windows.ps1 `
  -PythonPath .\.venv\Scripts\python.exe `
  -SkipDependencyInstall -SkipFrontendBuild
```

Smoke 始终使用自己的临时数据库；正式数据功能验收需另行在 UI 中完成。

## 6. 启用新系统

全部验收通过后再注销旧任务、注册新任务并启用新调度：

```powershell
Unregister-ScheduledTask -TaskName 'AutoCheckin Hub' -Confirm:$false
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\install-startup.ps1
Get-ScheduledTask -TaskName 'SiteHub' | Format-List TaskName,State
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\stop-sitehub.ps1 -AuditOnly
```

`install-startup.ps1` 只注册登录任务，不代表业务调度已经通过验收。任务 action 必须精确指向当前项目的 `tools\start-hidden.vbs`；设置中 `DisallowStartIfOnBatteries=False`、`StopIfGoingOnBatteries=False`、`MultipleInstances=IgnoreNew`。新调度必须在 SiteHub 设置中单独确认。

## 7. 保留与回滚

- 旧项目、旧数据库和旧 Profile 至少只读保留 7 天，并且不少于两个成功自动运行周期。不要移动、覆盖或清理它们。
- 回滚前先执行 `tools\stop-sitehub.ps1 -AuditOnly`，审计通过后运行 `tools\stop-sitehub.ps1`；确认 8686 和受控 Node/Chrome/OCR 子进程均已清理，再执行 `tools\uninstall-startup.ps1`。恢复旧任务前确认旧数据库仍完整。
- SiteHub 已经执行过当日签到时，旧签到调度必须暂停到下一个上海自然日，避免重复远端副作用。
- `effect_unknown` 任务必须先核对远端“今日是否已签”，不能盲目重试。
- 回滚不会自动反向同步 SiteHub 中新产生的网站、商品或历史；这些差异需要单独导出和审计。

## 预检阻断项速查

以下任一项存在时不得正式应用：任一 Git 工作树不干净、旧数据库不可读/哈希失败、非空 WAL、AutoCheckin 锁文件仍在、Profile 活跃锁存在、8686 已占用、计划任务或项目进程状态无法确认、旧任务仍启用、新任务提前启用、目标数据非空或权限不可确认、前端构建缺失、Python/Node 依赖缺失。
