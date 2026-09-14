"""自动调束算法包。

本包只负责一件事：**根据历史观测提出下一个候选参数**。
边界、最大单步、变化速率、联锁与设备状态检查全部在执行层完成
（架构文档 6.6：优化算法只提出候选参数）。

约束（架构文档 5.3 / 包依赖方向）：

* 不导入 PySide6、FastAPI、SQLAlchemy 或任何具体 EPICS 库；
* 只依赖 numpy 与标准库——项目核心依赖里只有 numpy，现场机器常常装不上
  sklearn/scipy（本机 PyPI 不通，三者皆缺），优化器不该因为依赖装不上而不可用。

当前实现：``GpEiOptimizer``（高斯过程 + 期望改进）。接口是一次一步的
``propose`` / ``observe``，而不是 ``minimize(f, n)``——执行层需要在每轮之间
插入边界校验、写设备、读回、必要时等人工确认，整段循环塞进优化器就插不进去了。
"""

from .bayes import Dimension, GpEiOptimizer, Proposal

__all__ = ["Dimension", "GpEiOptimizer", "Proposal"]
