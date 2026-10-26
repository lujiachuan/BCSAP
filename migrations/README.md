# 数据库迁移

下一阶段建立 PostgreSQL 数据模型时初始化 Alembic。迁移只由数据服务器的受控部署步骤执行，客户端和普通服务工作进程不能自行升级数据库。

## 首版表范围（Showcase / 团簇谱）

对齐现网旁路库 `cluster_ms`（本机常见端口 `5433`），来源可参考仓库外项目根目录的 `pg_schema_*.sql`。首版至少覆盖：

| 领域 | 表意图 |
| --- | --- |
| 谱图 | 原始曲线、元素集、芯片/样品名、质量公式 |
| 标注 | working / baseline lanes、峰位分子式、容差与 superseded |
| 校正 | 同位素/分段残差（mono、dual、multi-envelope）、metrics |
| 质量 | 打分、可疑元素、审核侧车 |

原始谱图与分析结果分开保存；分析操作不得覆盖原始数据（见根 README §11）。

## 执行边界

- 只有数据服务器部署流水线可跑 `alembic upgrade`。
- 桌面客户端、`instrument_service`、普通开发机不得自行改库结构。
- Showcase HTTP（`apps/data_service/showcase_api.py`）在库未配置前保持骨架/`501`，不读写生产库。
