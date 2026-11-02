# SiteHub 项目检查记录

检查日期：2026-11-02（上海时间）。代码基线：`a42103287f7e717f1ff1d65d5abb05c22a9b6945`。

本次包括代码与提交历史阅读、常规自动化测试、隔离服务与浏览器验收，以及文档和展示材料整理。不是全量安全审计或所有外部站点的实账号验收。

## 项目定位

SiteHub 将网站目录、商品监控与多账号签到整合为 Windows 本机工作台。技术展示适合围绕“本地自动化系统的可靠运行”展开：前后端界面只是入口，任务持久化、浏览器资源管理、异常恢复与数据维护构成主要工程内容。

它采用本机单用户模型，不能据此宣称已具备多租户隔离、公网高并发或分布式调度能力。目前也没有本次实测的吞吐量、延迟、覆盖率或长期可用率数据。

## 关键实现与阅读路线

### 1. 服务生命周期与组件协作

从 [main.py](../site_hub/main.py) 的 `create_app` 和 lifespan 阅读：先获取数据目录实例锁，再处理待恢复请求、升级前备份、数据初始化、任务恢复，最后启动后台组件。关闭时先停任务生产者，再收拢维护、分发、Worker 和通知任务，最后释放实例锁。

[架构说明](architecture.md)描述了 Python 控制面、SQLite、Node Worker 和浏览器的边界。Node 通过 JSON-RPC / JSON Lines 与 Python 通信，不直接访问业务数据库。这里适合解释为什么控制面与浏览器执行面分开，以及异常退出如何影响其他功能。

### 2. 任务租约、幂等与不确定结果

[任务服务](../site_hub/jobs/service.py)提供入队、领取、心跳、完成和恢复的业务接口；[任务存储](../site_hub/services/store_jobs.py)使用 SQLite 事务和每次租约的随机 Token 实现状态转换。同站点商品同步在领取时排除活动任务。

签到幂等键绑定账号与上海业务日期。这解决本地重复入队问题，不代表能对外部网站提供恰好一次执行保证。Worker 丢失、取消或超时可能留下远端效果不确定的结果；[runner.js](../worker-node/src/checkin/runner.js)包含核对分支，未获得充分证据时保留 `effect_unknown`。

### 3. Worker 与浏览器资源管理

[supervisor.py](../site_hub/workers/supervisor.py)负责握手、心跳、超时、恢复与 Windows 进程树约束。[Profile 锁](../worker-node/src/profile-locks.js)、[资源登记](../worker-node/src/resource-registry.js)及[进程登记](../worker-node/src/process-registry.js)分别约束账号资源使用和退出清理。

本次隔离验收实际终止了测试实例自己的 Worker，确认控制面仍可读取，随后 Worker 自动恢复；没有操作正式实例的 Worker。

### 4. 商品监控与通知

[商品写入](../site_hub/services/store_products.py)将目录更新、价格观察、监控状态和事件纳入事务；[监控规则](../site_hub/services/product_monitor.py)处理参考价格、金额与比例阈值、冷却和库存确认。[策略模型](../site_hub/monitoring_models.py)允许配置确认次数，默认是一次；不能把“支持多次确认”描述为所有站点默认多次确认。

通知采用[持久化 outbox](../site_hub/notifications/store.py)，[执行器](../site_hub/notifications/executor.py)租约领取、限制超时并按策略重试。网络投递后在确认前中断仍可能重复发送，外部接收方应自行处理幂等。

### 5. 数据保护与接口协作

[备份服务](../site_hub/services/backups.py)使用 SQLite Backup API、SHA-256 和完整性校验；恢复在启动时执行，并在验证失败时回滚。数据库快照不包含浏览器 Profile、截图和日志。

[前端任务 Store](../frontend/src/stores/tasks.ts)协调 SSE 与快照补拉；[DTO 生成器](../tools/generate-api-types.py)从 OpenAPI 生成前端类型。本次检查确认生成产物没有漂移。

## 本次验证结果

环境：Windows、Python 3.13.14、Node.js 24.12.0；使用本地已安装依赖。未重新演练空环境的首次依赖安装。

| 检查 | 结果 | 范围 |
| --- | --- | --- |
| Python 常规测试 | 232 通过，3 项排除 | `pytest -q -m "not integration and not profile"` |
| 前端测试 | 23 个测试文件，78 项通过 | Vitest / Vue Test Utils |
| Worker 测试 | 66 项通过 | Node 内置测试运行器 |
| 前端构建 | 通过 | `vue-tsc -b` 与 Vite；输出到隔离目录 |
| OpenAPI DTO | 无漂移 | `generate-api-types.py --check` |
| Python 依赖 | 无冲突 | `pip check` |
| 隔离服务与浏览器 | 全部通过 | 独立临时数据目录、非 8686 端口 |

隔离验收覆盖：健康与就绪接口、同一数据目录的第二实例拒绝、Worker 中断后的控制面可读性、Worker 自动恢复、维护设置与 CSRF、备份完整性、真实浏览器页面导航，以及优雅退出后无残留所属子进程。

未执行的部分：旧数据库只读集成演练、旧 Profile 哈希清单、真实站点登录与签到、外部 Webhook 实投，以及正式数据迁移或恢复。测试数量不是代码覆盖率。

## 展示资料整理

- README 按用途、功能、截图、启动、技术实现和文档导航重新编排。
- 原 README 的详细运行维护内容保留在 [operations.md](operations.md)，调整了移动后的文档链接。
- `docs/images/` 的四张截图由当前构建在隔离实例中拍摄，全部使用虚构的 `.example` 域名、商品和价格历史；它们说明界面能力，不代表真实抓取结果或签到成功记录。
- [开发说明](provenance.md)依据维护者确认，记录个人主导、Codex 与 Zcode 辅助开发的方式，并保留整合演进及依赖授权说明。

## 后续优先级

| 顺序 | 工作 | 完成标准 |
| --- | --- | --- |
| 1 | 整理开发案例 | 结合实际开发经历，说明需求提出、AI 协作、问题定位和验证过程 |
| 2 | 确定仓库授权 | 选择项目级 LICENSE，保留第三方依赖和实际引入代码的授权说明 |
| 3 | 接入持续集成 | 在隔离测试数据下跑常规测试、类型与 DTO 检查；不依赖真实账号 |
| 4 | 建立可重复演示 | 将本次临时截图流程整理为正式演示工具，禁止使用生产数据和真实凭据 |
| 5 | 形成可讲述的案例 | 围绕一次 Worker 故障、一次未知结果和一次恢复回滚解释触发条件、状态变化与验证方式 |

仓库简介：`Windows 本地网站管理与自动化工作台：收藏、商品监控、多账号签到、任务追踪与备份恢复。`

仓库 Topics：`vue3`、`typescript`、`fastapi`、`sqlite`、`playwright`、`automation`、`windows`、`product-monitoring`。

本次整理同步更新仓库简介和 Topics；未新增 CI 工作流或修改业务代码。
