# packages/spectrum

谱图编解码与（规划中的）团簇谱 showcase 算法。本包不得依赖 `apps/` 或 PySide6。

## 已有

- `codec.py`：NPZ 编解码、SHA-256、格式版本。

## Showcase 迁入映射

算法目前在仓库外的 `web_manager/`。迁入时按职责落到本包，经 `data_service` 暴露，不经 `instrument_service`。

| 职责 | 现网来源（旁路） | 本包目标模块 |
| --- | --- | --- |
| 峰匹配 / 标注桥 | `web_manager/annotate/` | `annotate.py`（待建） |
| 单同位素分段 | `web_manager/correct/correct_mono.py` | `correct_mono.py` |
| 双同位素指纹分段 | `web_manager/correct/correct_dual.py` | `correct_dual.py` |
| 多同位素包络分段 | `web_manager/correct/correct_ni.py` | `correct_multi.py` |
| 分段残差搜索 | `web_manager/correct/piecewise_resid.py` | `piecewise_resid.py` |
| Showcase 轴门禁 | `web_manager/axis/showcase_axis_safe.py` | `axis_safe.py` |
| good3 / 同事包 | `web_manager/pack/` | `pack_good3.py` |
| 元素推断 / 质量分 | `web_manager/quality/` | `quality.py` |

契约 DTO 在 `packages/contracts/showcase.py`；HTTP 在 `apps/data_service/showcase_api.py`。
当前桌面谱图库写路径与曲线/长图经 `SHOWCASE_UPSTREAM` 代理现网 `web_manager`，算法尚未迁入本包。
