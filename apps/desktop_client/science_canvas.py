"""结果分析的 Matplotlib 画布。
运行中曲线继续用 pyqtgraph；这里只负责可导出、可复现的科研图。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from matplotlib import patheffects as path_effects
from matplotlib import rcParams, style

try:
    import scienceplots  # noqa: F401 - 注册 SciencePlots 的 ``science`` 样式。
except ImportError:
    _HAS_SCIENCEPLOTS = False
else:
    _HAS_SCIENCEPLOTS = True
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.ticker import AutoMinorLocator, FuncFormatter, MaxNLocator

# ---------------------------------------------------------------------------
# 响应面矩阵配色（选型依据，改之前请先看这段）
# ---------------------------------------------------------------------------
# 预测均值：``viridis``。三个理由，按重要性排：
# 1) 它中段就是蓝绿色（50% 处 #21918c，Lab a*≈-30），正是要的那一支"蓝绿"，
#    而且是**单调的**蓝绿→黄，不是调色盘式的彩虹；
# 2) 感知均匀性在候选里最好：实测相邻 ΔL* 变异 0.06、最大步长 0.35（cividis 0.09/0.39，
#    ColorBrewer 的 GnBu/YlGnBu 0.45/0.36）——色阶上"同样的一段距离"对应同样大小的
#    数值差，读等高线间距才是可靠的；
# 3) 明度单调且跨度大（L* 14.9→90.9）：黑白打印、投影仪、红绿色觉障碍下依然分得开。
# 备选与不选的理由：GnBu / YlGnBu 更像"蓝绿"，但感知不均匀（相邻 ΔL 变异 0.36~0.45），
# 且浅端接近白色，白等高线压在上面完全看不见（最低对比度 1.02）；winter / summer
# 明度非单调，会把"亮"误读成"高"；cividis 偏灰蓝，中段是灰色（#7d7c78），不"蓝绿"。
SURFACE_MEAN_CMAP = "viridis"
# 不确定性：``magma``。与 viridis 冷/暖对立、同样感知均匀，远看不会与均值面板混淆。
SURFACE_STD_CMAP = "magma"
# 叠加墨色：均值面板顶部接近亮黄（L*≈91），纯白等高线/标记压上去几乎看不见
# （实测最低对比度 1.26）。所以所有叠加元素都画两遍——先粗一笔深色衬底，再细一笔
# 白色——在色标的任何一段上都读得出来。
OVERLAY_HALO = "#111827"
# 全矩阵共用一个色标（各配对图的 GP 均值在同一目标量量纲上），色标标题写清单位。


class ScientificAnalysisCanvas(FigureCanvasQTAgg):
    """将三类结果图统一为可发表的轴、色标和图例风格。"""

    def __init__(self, parent=None) -> None:
        # Windows 现场机通常自带微软雅黑；不依赖 LaTeX 或额外字体包，中文导出也可读。
        if _HAS_SCIENCEPLOTS:
            style.use(["science", "no-latex"])
        rcParams.update(
            {
                # SciencePlots 默认 serif 会把中文落到缺字字体；强制无衬线后，
                # Windows 的微软雅黑负责中文、Arial 负责拉丁字符和数字。
                "font.family": "sans-serif",
                "font.sans-serif": [
                    "Microsoft YaHei",
                    "Microsoft YaHei UI",
                    "SimHei",
                    "Arial",
                    "DejaVu Sans",
                ],
                "axes.unicode_minus": False,
                "pdf.fonttype": 42,
                "svg.fonttype": "none",
            }
        )
        # 默认 6.4×4.0：正好装进结果页左面板视口（整页不滚动）；
        # 响应面矩阵按维数单独放大画布，在滚动区里看大图。
        self.figure = Figure(figsize=(6.4, 4.0), facecolor="white")
        super().__init__(self.figure)
        self.setParent(parent)
        self.setMinimumHeight(280)
        self._interactive_axes = []
        self._scroll_area = None
        self._base_canvas_size = (640, 400)
        self._canvas_scale = 1.0
        self._canvas_pan_start = None
        # 单图模式"跟随视口"：面板/窗口尺寸变了就重排整图（结果页左栏会被拖动、
        # 窗口会被拉大缩小）。操作员一旦手动缩放/点了 100%，就交还控制权不自动改；
        # 点「适合窗口」重新交回自动。
        self._auto_fit = True
        self._last_fit_size: tuple[int, int] | None = None
        # 结果页的交互作用于整张图：左键拖动浏览，滚轮缩放整图。
        self.mpl_connect("button_press_event", self._on_button_press)
        self.mpl_connect("motion_notify_event", self._on_motion)
        self.mpl_connect("button_release_event", self._on_button_release)
        self.mpl_connect("scroll_event", self._on_scroll)

    def clear(self) -> None:
        self.figure.clear()
        # 与 __init__ 默认一致：单图默认 6.4×4.0（适配结果页视口），
        # 矩阵图在 surface() 里自行放大画布。
        self.figure.set_size_inches(6.4, 4.0, forward=True)
        self._interactive_axes = []
        self.sync_canvas_size()

    def attach_scroll_area(self, scroll_area) -> None:
        self._scroll_area = scroll_area

    def sync_canvas_size(self) -> None:
        """让长宽随矩阵维数增长，结果页用滚动框承载完整图像。

        默认图（6.4×4.0 → 640×400）不超过结果页左面板视口，整页无滚动；
        矩阵图维数增多时画布放大、超出视口的部分靠滚动区承载。
        """
        width, height = self.figure.get_size_inches() * self.figure.dpi
        self._base_canvas_size = (max(640, int(width)), max(400, int(height)))
        self._canvas_scale = 1.0
        self._last_fit_size = None
        self._apply_canvas_scale()

    def fit_to(self, width: int, height: int) -> None:
        """把画布缩放到视口内（保持宽高比），单图模式整页不出现滚动条。

        视口尺寸来自结果页 analysisFigureScroll；矩阵图不调用本方法，
        仍按维数放大画布并靠滚动区承载。

        留 2px 余量并允许缩到 0.25：不留余量时算出来的高度正好等于视口高度，
        Qt 仍会为它加一条滚动条（实测留 9px）；下限卡在 0.45 时，"适合窗口"
        在 700px 高的窗口上根本放不下（0.45×400=180 > 171 的可用高度）。
        上限放到 2.0：大屏上"适合窗口"应当把可视面积用满，而不是停在 100%
        留一片空白（图是矢量画布，放大是等比缩放，字号与线宽同步变大，不失真）。
        """
        if width < 120 or height < 120:
            return
        base_width, base_height = self._base_canvas_size
        self._auto_fit = True
        # 同一个视口尺寸只排一次：缩放会改变滚动条，反过来又触发视口 resize，
        # 不设这道闸就会和滚动条互相推着循环。
        if (width, height) == self._last_fit_size:
            return
        self._last_fit_size = (width, height)
        self._canvas_scale = min(
            2.0,
            max(0.25, min((width - 2) / base_width, (height - 2) / base_height)),
        )
        self._apply_canvas_scale()

    @property
    def auto_fit(self) -> bool:
        """是否仍在"跟随视口"（用户手动缩放后为 False）。"""
        return self._auto_fit

    def zoom_canvas(self, factor: float) -> None:
        """按比例缩放整张图，而不改变 GP 曲面、曲线或坐标范围。"""
        self._auto_fit = False
        self._last_fit_size = None
        self._canvas_scale = min(3.0, max(0.25, self._canvas_scale * factor))
        self._apply_canvas_scale()

    def reset_canvas_zoom(self) -> None:
        """回到 100%（原始尺寸）：显式动作，之后不再自动跟随视口。"""
        self._auto_fit = False
        self._last_fit_size = None
        self._canvas_scale = 1.0
        self._apply_canvas_scale()

    def _apply_canvas_scale(self) -> None:
        width = max(1, round(self._base_canvas_size[0] * self._canvas_scale))
        height = max(1, round(self._base_canvas_size[1] * self._canvas_scale))
        self.figure.set_size_inches(width / self.figure.dpi, height / self.figure.dpi, forward=True)
        self.setMinimumSize(width, height)
        self.resize(width, height)
        self.draw_idle()

    def _on_button_press(self, event) -> None:
        if event.button != 1 or self._scroll_area is None:
            return
        horizontal = self._scroll_area.horizontalScrollBar()
        vertical = self._scroll_area.verticalScrollBar()
        self._canvas_pan_start = (
            event.x,
            event.y,
            horizontal.value(),
            vertical.value(),
        )

    def _on_motion(self, event) -> None:
        if self._canvas_pan_start is None or self._scroll_area is None:
            return
        x0, y0, horizontal_start, vertical_start = self._canvas_pan_start
        self._scroll_area.horizontalScrollBar().setValue(round(horizontal_start + x0 - event.x))
        self._scroll_area.verticalScrollBar().setValue(round(vertical_start + y0 - event.y))

    def _on_button_release(self, event) -> None:
        del event
        self._canvas_pan_start = None

    def _on_scroll(self, event) -> None:
        self.zoom_canvas(1.15 if event.step > 0 else 1 / 1.15)

    def export(self, path: str | Path) -> None:
        self.figure.savefig(path, dpi=300, bbox_inches="tight")

    @staticmethod
    def _style(axis, x_label: str, y_label: str) -> None:
        axis.set_xlabel(x_label)
        axis.set_ylabel(y_label)
        axis.tick_params(
            direction="in",
            which="both",
            top=True,
            right=True,
            labelsize=8,
            length=4,
            width=0.75,
        )
        axis.minorticks_on()
        axis.xaxis.set_major_locator(MaxNLocator(nbins=6, min_n_ticks=4))
        axis.yaxis.set_major_locator(MaxNLocator(nbins=6, min_n_ticks=4))
        axis.grid(True, color="#cbd5e1", alpha=0.42, linewidth=0.45)
        axis.set_axisbelow(True)
        for spine in axis.spines.values():
            spine.set_linewidth(0.75)
            spine.set_color("#334155")

    def history(
        self,
        xs: list[float],
        ys: list[float],
        best: list[float],
        *,
        baseline: float | None,
        y_label: str,
        note: str,
    ) -> None:
        self.clear()
        axis = self.figure.add_subplot(111)
        self._interactive_axes = [axis]
        axis.plot(xs, ys, color="#0072B2", linewidth=0.85, alpha=0.72)
        axis.scatter(
            xs,
            ys,
            s=22,
            color="#0072B2",
            edgecolor="white",
            linewidth=0.5,
            label="单轮实测",
            zorder=3,
        )
        axis.step(xs, best, where="post", color="#D55E00", linewidth=1.8,
                  label="历史最优")
        if baseline is not None:
            axis.axhline(baseline, color="#64748b", linestyle="--", linewidth=1.0,
                         label="启动基线")
        if ys:
            index = int(np.argmax(ys))
            axis.scatter([xs[index]], [ys[index]], s=58, color="#D55E00", zorder=4)
            axis.annotate(
                f"最优 {ys[index]:.4g} @ 第 {xs[index]:g} 轮",
                (xs[index], ys[index]), xytext=(8, -18), textcoords="offset points",
                fontsize=9, color="#334155",
            )
        self._style(axis, "Trial / 轮次", y_label)
        axis.set_title("优化历史", loc="left", fontweight="bold", fontsize=10)
        axis.legend(frameon=False, ncol=3, fontsize=9, loc="lower right")
        axis.text(0.01, 0.01, note, transform=axis.transAxes, fontsize=8,
                  color="#64748b", va="bottom")
        self.sync_canvas_size()
        self.draw_idle()

    def importance(self, names: list[str], scores: list[float], *, note: str) -> None:
        self.clear()
        axis = self.figure.add_subplot(111)
        self._interactive_axes = [axis]
        order = np.argsort(scores)
        labels = [names[index] for index in order]
        values = np.asarray(scores)[order]
        bars = axis.barh(
            labels,
            values,
            color="#0072B2",
            edgecolor="#00527A",
            linewidth=0.5,
            height=0.62,
        )
        axis.set_xlim(0, max(float(values.max(initial=0)), 1e-9) * 1.2)
        for bar, value in zip(bars, values):
            axis.text(value, bar.get_y() + bar.get_height() / 2, f" {value:.3f}",
                      va="center", fontsize=9)
        self._style(axis, "FANOVA 重要性", "参数")
        axis.set_title("参数重要性", loc="left", fontweight="bold", fontsize=10)
        axis.text(0.01, 0.01, note, transform=axis.transAxes, fontsize=8,
                  color="#64748b", va="bottom")
        self.sync_canvas_size()
        self.draw_idle()

    def slice(
        self,
        xs: np.ndarray,
        ys: np.ndarray,
        mean_x: np.ndarray,
        mean_y: np.ndarray,
        band: np.ndarray,
        *,
        x_label: str,
        y_label: str,
        note: str,
    ) -> None:
        self.clear()
        axis = self.figure.add_subplot(111)
        self._interactive_axes = [axis]
        axis.scatter(xs, ys, s=26, color="#0072B2", edgecolor="white", linewidth=0.5,
                     label="实际采样", zorder=3)
        if mean_x.size:
            axis.plot(mean_x, mean_y, color="#009E73", linewidth=1.8, label="局部均值")
            axis.fill_between(mean_x, mean_y - band, mean_y + band, color="#009E73",
                              alpha=0.18, label="局部离散度")
        if ys.size:
            index = int(np.argmax(ys))
            axis.scatter([xs[index]], [ys[index]], s=58, color="#D55E00", zorder=4)
            axis.axvline(xs[index], color="#D55E00", linestyle=":", linewidth=1)
        self._style(axis, x_label, y_label)
        axis.set_title("单参数切片", loc="left", fontweight="bold", fontsize=10)
        axis.legend(frameon=False, ncol=3, fontsize=9, loc="best")
        axis.text(0.01, 0.01, note, transform=axis.transAxes, fontsize=8,
                  color="#64748b", va="bottom")
        self.sync_canvas_size()
        self.draw_idle()

    def surface(
        self,
        data: dict,
        labels: list[str],
        *,
        note: str,
        importance: dict[str, float] | None = None,
        value_label: str = "",
    ) -> int:
        """绘制最多五个最重要参数的 GP 矩阵，返回被省略的参数数。"""
        self.clear()
        data, labels, omitted = self._surface_subset(data, labels, importance or {})
        params = data.get("params") or []
        bounds = data.get("bounds") or []
        pairs = data.get("pairs") or {}
        points = np.asarray(data.get("points") or [], dtype=float)
        best = np.asarray(data.get("best") or [], dtype=float)
        n = len(params)
        if n < 2:
            self.draw_idle()
            return omitted
        # 导出尺寸随矩阵维数增长；屏幕仍按容器缩放，避免 5×5 时格子过小。
        self.figure.set_size_inches(max(8.0, n * 2.0 + 2.8), max(6.0, n * 1.9 + 1.5))
        axes = self.figure.subplots(n, n, squeeze=False)
        self._interactive_axes = [
            axes[row, column]
            for row in range(n)
            for column in range(n)
            if row != column and f"{min(row, column)}_{max(row, column)}" in pairs
        ]
        self.figure.subplots_adjust(
            left=0.12,
            right=0.78,
            bottom=0.08,
            top=0.92,
            # 每格都显示自己的刻度值，留出足够间距避免相邻图的数字相撞。
            wspace=0.25,
            hspace=0.22,
        )
        mean_images, std_images = [], []
        means = [np.asarray(pair["mean"], dtype=float) for pair in pairs.values()]
        stds = [np.asarray(pair["std"], dtype=float) for pair in pairs.values()]
        mean_range = (min(item.min() for item in means), max(item.max() for item in means))
        std_range = (min(item.min() for item in stds), max(item.max() for item in stds))
        for row in range(n):
            for column in range(n):
                axis = axes[row, column]
                if row == column:
                    axis.text(
                        0.5,
                        0.5,
                        self._diagonal_label(labels[row]),
                        ha="center",
                        va="center",
                        fontsize=8,
                        fontweight="normal",
                        wrap=True,
                    )
                    axis.set_facecolor("#f5f7fa")
                    for spine in axis.spines.values():
                        spine.set_color("#d5dce5")
                        spine.set_linewidth(0.65)
                    axis.set_axis_off()
                    continue
                i, j = min(row, column), max(row, column)
                pair = pairs.get(f"{i}_{j}")
                if not pair:
                    axis.set_axis_off()
                    continue
                is_mean = row > column
                raw = np.asarray(pair["mean" if is_mean else "std"], dtype=float)
                image = raw.T if is_mean else raw
                extent = [*bounds[column], *bounds[row]]
                cmap = SURFACE_MEAN_CMAP if is_mean else SURFACE_STD_CMAP
                levels = mean_range if is_mean else std_range
                shown = axis.imshow(image, origin="lower", extent=extent, aspect="auto",
                                    cmap=cmap, vmin=levels[0], vmax=levels[1])
                axis.set_xlim(bounds[column])
                axis.set_ylim(bounds[row])
                axis.set_box_aspect(1)
                (mean_images if is_mean else std_images).append(shown)
                if is_mean:
                    x_grid = np.linspace(bounds[column][0], bounds[column][1], image.shape[1])
                    y_grid = np.linspace(bounds[row][0], bounds[row][1], image.shape[0])
                    # 两遍等高线 = 自带描边：白色线在"暗端"看得见，深色衬底让它在
                    # "亮端"（viridis 顶部接近亮黄）也看得见。只画白线时，最优区
                    # 恰恰是白线最看不清的地方。
                    axis.contour(
                        x_grid,
                        y_grid,
                        image,
                        colors=OVERLAY_HALO,
                        linewidths=0.95,
                        alpha=0.42,
                        levels=7,
                    )
                    axis.contour(
                        x_grid,
                        y_grid,
                        image,
                        colors="white",
                        linewidths=0.42,
                        alpha=0.92,
                        levels=7,
                    )
                if points.size:
                    # 采样点同为空心白圈：先画深色描边再画白圈（同上，保证亮端可读）
                    axis.scatter(
                        points[:, column],
                        points[:, row],
                        s=14,
                        facecolors="none",
                        edgecolors=OVERLAY_HALO,
                        alpha=0.45,
                        linewidths=1.0,
                    )
                    axis.scatter(
                        points[:, column],
                        points[:, row],
                        s=10,
                        facecolors="none",
                        edgecolors="white",
                        alpha=0.88,
                        linewidths=0.45,
                    )
                if best.size > max(row, column):
                    for line in (
                        axis.axvline(best[column], color="white", linestyle="--",
                                     linewidth=0.6),
                        axis.axhline(best[row], color="white", linestyle="--",
                                     linewidth=0.6),
                    ):
                        line.set_path_effects(
                            [path_effects.withStroke(linewidth=1.9, foreground=OVERLAY_HALO,
                                                     alpha=0.55)]
                        )
                axis.xaxis.set_major_locator(
                    MaxNLocator(nbins=4, min_n_ticks=3, steps=[1, 2, 2.5, 5, 10])
                )
                axis.yaxis.set_major_locator(
                    MaxNLocator(nbins=4, min_n_ticks=3, steps=[1, 2, 2.5, 5, 10])
                )
                axis.xaxis.set_minor_locator(AutoMinorLocator(2))
                axis.yaxis.set_minor_locator(AutoMinorLocator(2))
                axis.tick_params(
                    direction="in",
                    labelsize=5.5,
                    top=True,
                    right=True,
                    length=2.8,
                    width=0.65,
                    pad=1.5,
                )
                axis.tick_params(which="minor", length=1.6, width=0.55)
                for spine in axis.spines.values():
                    spine.set_linewidth(0.65)
                axis.xaxis.set_major_formatter(FuncFormatter(self._tick_label))
                axis.yaxis.set_major_formatter(FuncFormatter(self._tick_label))
        if mean_images:
            mean_bar_axis = self.figure.add_axes([0.835, 0.14, 0.018, 0.30])
            mean_bar = self.figure.colorbar(mean_images[0], cax=mean_bar_axis)
            mean_bar.set_label(
                f"GP 预测均值 · {value_label}" if value_label else "GP 预测均值",
                fontsize=7,
                labelpad=5,
            )
            mean_bar.ax.tick_params(labelsize=6, length=2.5, width=0.6, direction="in")
        if std_images:
            std_bar_axis = self.figure.add_axes([0.835, 0.56, 0.018, 0.30])
            std_bar = self.figure.colorbar(std_images[0], cax=std_bar_axis)
            std_bar.set_label("GP 不确定性", fontsize=7, labelpad=5)
            std_bar.ax.tick_params(labelsize=6, length=2.5, width=0.6, direction="in")
        self.figure.suptitle(
            "响应面矩阵：下三角为预测均值（矩阵内共用同一色标），上三角为不确定性",
            fontsize=8,
            y=0.975,
        )
        self.figure.text(0.01, 0.018, note, fontsize=6, color="#64748b")
        self.sync_canvas_size()
        # 矩阵图不跟视口：维数一多，格子必须保持可读的物理尺寸，靠滚动区看大图。
        self._auto_fit = False
        self.draw_idle()
        return omitted

    @staticmethod
    def _surface_subset(
        data: dict, labels: list[str], importance: dict[str, float]
    ) -> tuple[dict, list[str], int]:
        """参数多时抽取最重要的五维，并重新映射成自洽的配对矩阵。"""
        params = list(data.get("params") or [])
        indices = list(range(len(params)))
        if len(indices) > 5:
            indices.sort(key=lambda index: importance.get(params[index], 0.0), reverse=True)
            indices = sorted(indices[:5])
        if len(indices) == len(params):
            return data, labels, 0
        old_pairs = data.get("pairs") or {}
        pairs: dict[str, dict] = {}
        for new_i, old_i in enumerate(indices):
            for new_j, old_j in enumerate(indices[new_i + 1 :], new_i + 1):
                first, second = sorted((old_i, old_j))
                pair = old_pairs.get(f"{first}_{second}")
                if not pair:
                    continue
                flipped = old_i > old_j
                pairs[f"{new_i}_{new_j}"] = {
                    **pair,
                    "mean": (
                        np.asarray(pair["mean"]).T if flipped else np.asarray(pair["mean"])
                    ).tolist(),
                    "std": (
                        np.asarray(pair["std"]).T if flipped else np.asarray(pair["std"])
                    ).tolist(),
                }
        points = np.asarray(data.get("points") or [], dtype=float)
        return (
            {
                **data,
                "params": [params[index] for index in indices],
                "bounds": [data["bounds"][index] for index in indices],
                "best": [data["best"][index] for index in indices],
                "points": points[:, indices].tolist() if points.size else [],
                "pairs": pairs,
            },
            [labels[index] for index in indices],
            len(params) - len(indices),
        )

    @staticmethod
    def _short_label(label: str, limit: int = 11) -> str:
        """矩阵单元格只留足以区分参数的短名称，完整名称仍在 UI 下拉框中。"""
        compact = label.replace("设定", "").replace(" ", "")
        return compact if len(compact) <= limit else f"{compact[:limit - 1]}…"

    @classmethod
    def _diagonal_label(cls, label: str) -> str:
        compact = label.replace("设定", "").replace("设置", "").strip()
        compact = compact.replace("（", "(").replace("）", ")")
        if "(" in compact:
            name, unit = compact.rsplit("(", 1)
            unit = unit.rstrip(")")
            return f"{cls._short_label(name, 9)}\n({unit})"
        return cls._short_label(compact, 10)

    @staticmethod
    def _tick_label(value: float, _position: int) -> str:
        if abs(value) >= 1000:
            return f"{value:,.0f}"
        if abs(value) >= 10:
            return f"{value:g}"
        return f"{value:.2g}"
