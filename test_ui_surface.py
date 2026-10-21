# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, r"D:\BCSAP")
from PySide6.QtWidgets import QApplication
from apps.desktop_client.pages.tuning import TuningPage

app = QApplication([])
page = TuningPage()

# 注入假 surface（4 参数，6 对，25x25）
n = 4
grid = 25
import random
pairs = {}
for i in range(n):
    for j in range(i+1, n):
        pairs[f"{i}_{j}"] = {
            "i": i, "j": j,
            "mean": [[[random.random() for _ in range(grid)] for _ in range(grid)]][0],
            "std": [[[random.random() for _ in range(grid)] for _ in range(grid)]][0],
        }
page._analysis = {
    "surface": {
        "available": True,
        "params": [f"hv.dw0{k}.v" for k in range(1,5)],
        "bounds": [[0,200],[0,2500],[0,2500],[0,5100]],
        "best": [100,1500,1500,3000],
        "points": [[100,1500,1500,3000],[80,1200,1300,2400]],
        "pairs": pairs,
    }
}
page._refresh_result_analysis_choices()
choice_combo = page.analysis_choice_combo
choices = [choice_combo.itemText(i) for i in range(choice_combo.count())]
print("分析视图选项:", choices)
assert "响应面矩阵" in choices
page._set_analysis_choice("surface")
page._render_result_analysis()
# grid 应有 n*n=16 个格子
count = page.surface_grid.count()
print("surface grid 格子数:", count)
assert count == n*n, count
print("PASS")
