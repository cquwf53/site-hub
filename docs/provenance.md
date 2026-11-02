# 开发方式与项目演进

## 开发方式

SiteHub 由维护者主导，借助 Codex 和 Zcode，以 Vibe Coding 方式开发。提交历史保留已有的 AI 协作署名。

项目围绕本地网站管理与自动化需求迭代，涵盖界面、服务端、浏览器 Worker、自动化测试和运行维护。技术实现与验证结果见[架构说明](architecture.md)和[项目检查记录](project-review.md)。

## 仓库中的演进记录

- 最早提交为 `5242bd1`，标题为 `Initial SiteHub merged application`，提交日期为 2026-08-03（上海时间）。它是整合后的初始快照，早期开发过程不全部体现在本仓库历史中。
- 后续提交包含网站类型管理、签到批次跟踪、快捷访问、数据查询修复、任务可靠性、商品监控和数据维护等演进。部分提交保留了 AI 协作署名。
- `site_hub/services/legacy_import.py` 与 `tools/migrate_legacy.py` 支持从旧 VShop 和 AutoCheckin 数据库导入，详细映射见[迁移文档](legacy-migration.md)。迁移工具用于衔接旧系统数据与当前的数据模型。

## 依赖与许可证

Python 和 npm 第三方依赖由锁文件记录，遵循各自许可证。仓库目前尚未设置项目级 LICENSE，后续由维护者选择。若引入第三方代码或素材，应同时保留相应来源、许可证及署名。
