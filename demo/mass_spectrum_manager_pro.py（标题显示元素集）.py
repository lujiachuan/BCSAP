# -*- coding: utf-8 -*-
"""
质谱图管理系统  Mass Spectrum Manager Pro
==========================================
专业版 — 针对大量质谱图的批量管理与处理。

核心功能：
  ★ 数据库持久化（SQLite，无需额外安装）
  ★ 元数据管理（样品名、日期、实验条件、备注、标签）
  ★ 分组 / 标签系统
  ★ 搜索 / 筛选
  ★ 批量处理（基线扣除、峰识别、归一化、导出）
  ★ 项目保存 / 加载
  ★ 单图查看 / 多图叠加对比 / 网格子图
  ★ 导出图片（PNG/SVG/PDF）、导出数据（CSV/Excel）

依赖：pip install matplotlib pandas scipy numpy
运行：python mass_spectrum_manager_pro.py
"""

import os
import sys
import json
import sqlite3
import datetime
import tkinter as tk
import tkinter.font
from tkinter import ttk, filedialog, messagebox, colorchooser, simpledialog
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
# Numba可选加速
try:
    from numba import njit, prange
    HAS_NUMBA = True
except ImportError:
    HAS_NUMBA = False
    def njit(*args, **kwargs):
        if args and callable(args[0]):
            return args[0]
        def decorator(func):
            return func
        return decorator
    prange = range
import numpy as np
import pandas as pd
from scipy.signal import find_peaks
from scipy.interpolate import UnivariateSpline

import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt
from matplotlib import font_manager

# 字体配置（可被用户设置覆盖）
FONT_CONFIG = {
    'ui_font': 'SimSun',
    'ui_size': 9,
    'chart_en_font': 'Times New Roman',
    'chart_cn_font': 'SimSun',
    'chart_size': 10,
}

def load_font_config():
    """从配置文件加载字体设置"""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'font_config.json')
    if getattr(sys, 'frozen', False):
        config_path = os.path.join(os.path.dirname(sys.executable), 'font_config.json')
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            FONT_CONFIG.update(cfg)
        except Exception:
            pass

def save_font_config():
    """保存字体设置到配置文件"""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'font_config.json')
    if getattr(sys, 'frozen', False):
        config_path = os.path.join(os.path.dirname(sys.executable), 'font_config.json')
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(FONT_CONFIG, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

load_font_config()


def setup_chinese_font():
    """健壮的中英文字体设置，使用FONT_CONFIG配置"""
    # 尝试注册Windows系统字体
    if sys.platform == 'win32':
        windir = os.environ.get('WINDIR', r'C:\Windows')
        candidates = [
            os.path.join(windir, 'Fonts', 'simsun.ttc'),
            os.path.join(windir, 'Fonts', 'simhei.ttf'),
            os.path.join(windir, 'Fonts', 'msyh.ttc'),
            os.path.join(windir, 'Fonts', 'times.ttf'),
            os.path.join(windir, 'Fonts', 'timesbd.ttf'),
            os.path.join(windir, 'Fonts', 'timesi.ttf'),
            os.path.join(windir, 'Fonts', 'arial.ttf'),
        ]
        for p in candidates:
            if os.path.exists(p):
                try:
                    font_manager.fontManager.addfont(p)
                except Exception:
                    pass

    available = {f.name for f in font_manager.fontManager.ttflist}
    cn_font = FONT_CONFIG['chart_cn_font'] if FONT_CONFIG['chart_cn_font'] in available else 'SimSun'
    en_font = FONT_CONFIG['chart_en_font'] if FONT_CONFIG['chart_en_font'] in available else 'Times New Roman'
    if cn_font not in available:
        cn_font = 'SimHei' if 'SimHei' in available else 'DejaVu Sans'
    if en_font not in available:
        en_font = 'DejaVu Sans'

    # 中文字体放第一位，确保中文正常显示（英文会自动用字体中的英文字形）
    plt.rcParams['font.sans-serif'] = [cn_font, en_font, 'Microsoft YaHei', 'SimHei', 'DejaVu Sans']
    plt.rcParams['font.family'] = 'sans-serif'
    plt.rcParams['font.size'] = FONT_CONFIG['chart_size']
    plt.rcParams['axes.unicode_minus'] = False
    plt.rcParams['mathtext.fontset'] = 'stix'  # STIX字体支持英文数字，与宋体配合较好
    # 强制注册字体名别名
    try:
        from matplotlib.font_manager import FontProperties
        plt.rcParams['font.cursive'] = [cn_font]
        plt.rcParams['font.fantasy'] = [cn_font]
        plt.rcParams['font.monospace'] = ['Consolas', 'Courier New', cn_font]
    except Exception:
        pass
    return cn_font, en_font

CN_FONT, EN_FONT = setup_chinese_font()
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.ticker import AutoMinorLocator

# 字体已由 setup_chinese_font() 设置
import warnings
warnings.filterwarnings('ignore', category=UserWarning, message='Glyph .* missing from font')

# ============================================================
# 全局配置
# ============================================================
APP_TITLE = "质谱图管理系统  Mass Spectrum Manager Pro"

APP_VERSION = "v2.0"
DB_FILE = "mass_spectrum_db.sqlite"
DEFAULT_COLORS = [
    '#1F4E79', '#C00000', '#2E7D32', '#E65100', '#6A1B9A',
    '#00838F', '#AD1457', '#33691E', '#4E342E', '#1565C0',
    '#D84315', '#00695C', '#4527A0', '#827717', '#B71C1C',
    '#00ACC1', '#F06292', '#7CB342', '#FF7043', '#5C6BC0',
]
# 初始浅色主题
BG = '#FFFFFF'          # 白色主背景
PANEL = '#F5F7FA'       # 浅灰面板
PANEL2 = '#E8ECF1'      # 稍亮面板（按钮/输入框）
PANEL3 = '#DDE3EC'      # 悬停/高亮面板
ACCENT = '#1565C0'      # 蓝色强调色
ACCENT2 = '#1976D2'     # 亮蓝辅助
ACCENT3 = '#00ACC1'     # 青色点缀
TEXT = '#212121'        # 深灰文字
TEXT_DIM = '#616161'    # 次要文字
MUTED = '#757575'       # 弱化文字
BORDER = '#E0E0E0'      # 边框色
BORDER_LIGHT = '#EEEEEE' # 亮边框
HOVER = '#E3F2FD'       # 悬停色
SELECTED = '#BBDEFB'    # 选中色
SUCCESS = '#4CAF50'     # 成功绿
WARNING = '#FF9800'     # 警告黄
ERROR = '#F44336'       # 错误红


# ============================================================
# 数据库层
# ============================================================
class SpectrumDB:
    def __init__(self, db_path=DB_FILE):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        c = self.conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS spectra (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            filepath TEXT,
            sample_name TEXT,
            date TEXT,
            operator TEXT,
            conditions TEXT,
            ar_flow TEXT,
            he_flow TEXT,
            liquid_n2 INTEGER,
            power TEXT,
            condensation_distance TEXT,
            pressure TEXT,
            tags TEXT,
            notes TEXT,
            color TEXT,
            x_min REAL, x_max REAL,
            y_min REAL, y_max REAL,
            n_points INTEGER,
            x_data BLOB,
            y_raw BLOB,
            baseline BLOB,
            peaks TEXT,
            created_at TEXT,
            updated_at TEXT
        )''')
        # 数据库迁移：为旧库添加新字段
        existing_cols = {row['name'] for row in c.execute("PRAGMA table_info(spectra)").fetchall()}
        new_cols = [
            ('ar_flow', 'TEXT'), ('he_flow', 'TEXT'), ('liquid_n2', 'INTEGER'),
            ('power', 'TEXT'), ('condensation_distance', 'TEXT'), ('pressure', 'TEXT'),
            ('mass_formula', 'TEXT'),
            ('element_set', 'TEXT'), ('temperature', 'TEXT'), ('acceleration_voltage', 'TEXT'),
            ('hp_annotations', 'TEXT'),
        ]
        for col_name, col_type in new_cols:
            if col_name not in existing_cols:
                c.execute(f'ALTER TABLE spectra ADD COLUMN {col_name} {col_type}')
        c.execute('''CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            color TEXT,
            created_at TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS spectrum_groups (
            spectrum_id INTEGER,
            group_id INTEGER,
            PRIMARY KEY (spectrum_id, group_id)
        )''')
        self.conn.commit()

    def add_spectrum(self, spec, commit=True):
        c = self.conn.cursor()
        now = datetime.datetime.now().isoformat()
        c.execute('''INSERT INTO spectra (name, filepath, sample_name, date, operator,
            conditions, ar_flow, he_flow, liquid_n2, power, condensation_distance, pressure,
            mass_formula, element_set, temperature, acceleration_voltage, hp_annotations,
            tags, notes, color, x_min, x_max, y_min, y_max, n_points,
            x_data, y_raw, baseline, peaks, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (spec.name, spec.filepath, spec.sample_name, spec.date, spec.operator,
             spec.conditions,
             getattr(spec, 'ar_flow', ''), getattr(spec, 'he_flow', ''),
             1 if getattr(spec, 'liquid_n2', False) else 0,
             getattr(spec, 'power', ''), getattr(spec, 'condensation_distance', ''),
             getattr(spec, 'pressure', ''),
             getattr(spec, 'mass_formula', ''),
             getattr(spec, 'element_set', ''), getattr(spec, 'temperature', ''),
             getattr(spec, 'acceleration_voltage', '40 kV'),
             getattr(spec, 'hp_annotations', ''),
             json.dumps(spec.tags, ensure_ascii=False), spec.notes,
             spec.color, spec.x_min, spec.x_max, spec.y_min, spec.y_max, len(spec.x),
             spec.x.tobytes(), spec.y_raw.tobytes(),
             spec.baseline.tobytes() if spec.baseline is not None else None,
             spec.peaks.to_json() if spec.peaks is not None else None,
             now, now))
        if commit:
            self.conn.commit()
        return c.lastrowid

    def update_spectrum(self, spec):
        now = datetime.datetime.now().isoformat()
        self.conn.execute('''UPDATE spectra SET name=?, sample_name=?, date=?, operator=?,
            conditions=?, ar_flow=?, he_flow=?, liquid_n2=?, power=?,
            condensation_distance=?, pressure=?, mass_formula=?,
            element_set=?, temperature=?, acceleration_voltage=?, hp_annotations=?,
            tags=?, notes=?, color=?,
            baseline=?, peaks=?, updated_at=?
            WHERE id=?''',
            (spec.name, spec.sample_name, spec.date, spec.operator, spec.conditions,
             getattr(spec, 'ar_flow', ''), getattr(spec, 'he_flow', ''),
             1 if getattr(spec, 'liquid_n2', False) else 0,
             getattr(spec, 'power', ''), getattr(spec, 'condensation_distance', ''),
             getattr(spec, 'pressure', ''),
             getattr(spec, 'mass_formula', ''),
             getattr(spec, 'element_set', ''), getattr(spec, 'temperature', ''),
             getattr(spec, 'acceleration_voltage', '40 kV'),
             getattr(spec, 'hp_annotations', ''),
             json.dumps(spec.tags, ensure_ascii=False), spec.notes, spec.color,
             spec.baseline.tobytes() if spec.baseline is not None else None,
             spec.peaks.to_json() if spec.peaks is not None else None,
             now, spec.db_id))
        self.conn.commit()

    def delete_spectrum(self, spec_id):
        self.conn.execute('DELETE FROM spectra WHERE id=?', (spec_id,))
        self.conn.execute('DELETE FROM spectrum_groups WHERE spectrum_id=?', (spec_id,))
        self.conn.commit()

    def get_all_spectra(self):
        rows = self.conn.execute('SELECT * FROM spectra ORDER BY updated_at DESC').fetchall()
        return [self._row_to_spec(r) for r in rows]

    def search_spectra(self, keyword='', tag='', group_id=None):
        query = 'SELECT * FROM spectra WHERE 1=1'
        params = []
        if keyword:
            query += ' AND (name LIKE ? OR sample_name LIKE ? OR notes LIKE ? OR conditions LIKE ?)'
            kw = f'%{keyword}%'
            params.extend([kw, kw, kw, kw])
        if tag:
            query += ' AND tags LIKE ?'
            params.append(f'%"{tag}"%')
        query += ' ORDER BY updated_at DESC'
        rows = self.conn.execute(query, params).fetchall()
        results = [self._row_to_spec(r) for r in rows]
        if group_id:
            group_spec_ids = {r['spectrum_id'] for r in
                self.conn.execute('SELECT spectrum_id FROM spectrum_groups WHERE group_id=?',
                                  (group_id,)).fetchall()}
            results = [s for s in results if s.db_id in group_spec_ids]
        return results

    def _row_to_spec(self, row):
        from types import SimpleNamespace
        s = SimpleNamespace()
        s.db_id = row['id']
        s.name = row['name']
        s.filepath = row['filepath']
        s.sample_name = row['sample_name'] or ''
        s.date = row['date'] or ''
        s.operator = row['operator'] or ''
        s.conditions = row['conditions'] or ''
        s.ar_flow = row['ar_flow'] if 'ar_flow' in row.keys() else ''
        s.he_flow = row['he_flow'] if 'he_flow' in row.keys() else ''
        s.liquid_n2 = bool(row['liquid_n2']) if 'liquid_n2' in row.keys() and row['liquid_n2'] is not None else False
        s.power = row['power'] if 'power' in row.keys() else ''
        s.condensation_distance = row['condensation_distance'] if 'condensation_distance' in row.keys() else ''
        s.pressure = row['pressure'] if 'pressure' in row.keys() else ''
        s.tags = json.loads(row['tags']) if row['tags'] else []
        s.notes = row['notes'] or ''
        s.color = row['color'] or '#1F4E79'
        s.mass_formula = row['mass_formula'] if 'mass_formula' in row.keys() and row['mass_formula'] else ''
        s.element_set = row['element_set'] if 'element_set' in row.keys() and row['element_set'] else ''
        s.temperature = row['temperature'] if 'temperature' in row.keys() and row['temperature'] else ''
        s.acceleration_voltage = row['acceleration_voltage'] if 'acceleration_voltage' in row.keys() and row['acceleration_voltage'] else '40 kV'
        s.hp_annotations = row['hp_annotations'] if 'hp_annotations' in row.keys() and row['hp_annotations'] else ''
        s.formula_matches = {}
        s.formula_peak_map = {}
        # 安全类型转换：旧数据库可能存了错误类型
        try:
            s.x_min = float(row['x_min'])
        except (TypeError, ValueError):
            s.x_min = 0.0
        try:
            s.x_max = float(row['x_max'])
        except (TypeError, ValueError):
            s.x_max = 0.0
        try:
            s.y_min = float(row['y_min'])
        except (TypeError, ValueError):
            s.y_min = 0.0
        try:
            s.y_max = float(row['y_max'])
        except (TypeError, ValueError):
            s.y_max = 0.0
        try:
            s.n_points = int(row['n_points'])
        except (TypeError, ValueError):
            s.n_points = 0
        s.x = np.frombuffer(row['x_data'], dtype=np.float64)
        s.y_raw = np.frombuffer(row['y_raw'], dtype=np.float64)
        # 如果 x_min 等异常，从数组重新计算
        if s.x_min == 0.0 and s.x_max == 0.0 and len(s.x) > 0:
            s.x_min = float(s.x.min())
            s.x_max = float(s.x.max())
        if s.y_min == 0.0 and s.y_max == 0.0 and len(s.y_raw) > 0:
            s.y_min = float(s.y_raw.min())
            s.y_max = float(s.y_raw.max())
        if s.n_points == 0:
            s.n_points = len(s.x)
        s.baseline = np.frombuffer(row['baseline'], dtype=np.float64) if row['baseline'] else None
        s.peaks = pd.DataFrame(json.loads(row['peaks'])) if row['peaks'] else None
        s.visible = True
        s.y = s.y_raw.copy()
        s.baseline_corrected = False
        s.normalized = False
        return s

    def add_group(self, name, color='#1F4E79'):
        try:
            c = self.conn.cursor()
            c.execute('INSERT INTO groups (name, color, created_at) VALUES (?,?,?)',
                      (name, color, datetime.datetime.now().isoformat()))
            self.conn.commit()
            return c.lastrowid
        except sqlite3.IntegrityError:
            return None

    def get_groups(self):
        return self.conn.execute('SELECT * FROM groups ORDER BY name').fetchall()

    def delete_group(self, group_id):
        self.conn.execute('DELETE FROM groups WHERE id=?', (group_id,))
        self.conn.execute('DELETE FROM spectrum_groups WHERE group_id=?', (group_id,))
        self.conn.commit()

    def assign_to_group(self, spec_id, group_id):
        self.conn.execute('INSERT OR IGNORE INTO spectrum_groups VALUES (?,?)', (spec_id, group_id))
        self.conn.commit()

    def remove_from_group(self, spec_id, group_id):
        self.conn.execute('DELETE FROM spectrum_groups WHERE spectrum_id=? AND group_id=?',
                          (spec_id, group_id))
        self.conn.commit()

    def find_spectrum_by_key(self, name, date, filepath):
        """按名称+日期+路径查找谱图，返回dict或None"""
        cur = self.conn.execute(
            "SELECT * FROM spectra WHERE name=? AND date=? AND filepath=?",
            (name, date, filepath))
        row = cur.fetchone()
        if row:
            return dict(zip([d[0] for d in cur.description], row))
        return None

    def insert_spectrum_data(self, data):
        """插入谱图数据（data为字典）"""
        cols = [c for c in data.keys() if c != 'id']
        placeholders = ','.join(['?'] * len(cols))
        values = [data.get(c) for c in cols]
        self.conn.execute(f"INSERT INTO spectra ({','.join(cols)}) VALUES ({placeholders})", values)
        self.conn.commit()

    def replace_spectrum(self, old_id, new_data):
        """用新数据替换旧谱图（保留id）"""
        cols = [c for c in new_data.keys() if c != 'id']
        set_clause = ','.join([f"{c}=?" for c in cols])
        values = [new_data.get(c) for c in cols] + [old_id]
        self.conn.execute(f"UPDATE spectra SET {set_clause} WHERE id=?", values)
        self.conn.commit()

    def merge_database(self, other_db_path):
        """合并另一个数据库的谱图到当前数据库，返回(新增数, 跳过数)"""
        if not os.path.exists(other_db_path):
            return 0, 0
        added = skipped = 0
        try:
            self.conn.execute(f"ATTACH DATABASE '{other_db_path}' AS other_db")
            # 获取当前数据库所有谱图，用于去重（名称+实验日期+路径）
            current = set()
            for row in self.conn.execute("SELECT name, date, filepath FROM spectra"):
                current.add((row[0], row[1] or '', row[2]))
            # 读取另一个数据库的谱图
            for row in self.conn.execute("SELECT * FROM other_db.spectra"):
                cols = [d[0] for d in self.conn.execute("SELECT * FROM spectra LIMIT 1").description]
                data = dict(zip(cols, row))
                key = (data.get('name'), data.get('date') or '', data.get('filepath'))
                if key in current:
                    skipped += 1
                    continue
                # 插入新谱图（排除id）
                cols_no_id = [c for c in cols if c != 'id']
                placeholders = ','.join(['?'] * len(cols_no_id))
                values = [data.get(c) for c in cols_no_id]
                self.conn.execute(f"INSERT INTO spectra ({','.join(cols_no_id)}) VALUES ({placeholders})", values)
                added += 1
            self.conn.commit()
            self.conn.execute("DETACH DATABASE other_db")
        except Exception as e:
            print(f"合并失败: {e}")
            try:
                self.conn.execute("DETACH DATABASE other_db")
            except Exception:
                pass
        return added, skipped

    def close(self):
        self.conn.close()


# ============================================================
# 数据处理函数
# ============================================================
def load_spectrum_file(filepath):
    try:
        # 先检查是否是扫谱格式（有#注释行）
        is_scan = False
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if line.startswith('#'):
                    is_scan = True
                    break
                elif line:
                    break

        if is_scan:
            # 扫谱格式：跳过#注释，读取数据
            x_list, y_list = [], []
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.replace(',', '\t').split()
                    if len(parts) >= 3:
                        # 三列：取第1列(current)和第3列(intensity)
                        try:
                            x_list.append(float(parts[0]))
                            y_list.append(float(parts[2]))
                        except ValueError:
                            continue
                    elif len(parts) == 2:
                        try:
                            x_list.append(float(parts[0]))
                            y_list.append(float(parts[1]))
                        except ValueError:
                            continue
            x = np.array(x_list, dtype=np.float64)
            y = np.array(y_list, dtype=np.float64)
        else:
            # 普通格式
            for sep in [r'\s+', ',', '\t', ';']:
                try:
                    df = pd.read_csv(filepath, sep=sep, engine='python')
                    if df.shape[1] >= 2:
                        break
                except Exception:
                    continue
            else:
                df = pd.read_csv(filepath, sep=r'\s+', header=None, engine='python')
            cols = df.columns[:2]
            x = pd.to_numeric(df[cols[0]], errors='coerce').values
            y = pd.to_numeric(df[cols[1]], errors='coerce').values

        mask = ~(np.isnan(x) | np.isnan(y))
        x, y = x[mask], y[mask]
        order = np.argsort(x)
        x, y = x[order], y[order]
        if len(x) < 10:
            return None, f"数据点过少（{len(x)}个）"
        return (x, y), None
    except Exception as e:
        return None, str(e)


def parse_scan_metadata(filepath):
    """从扫谱文件的#注释行中提取实验参数"""
    meta = {}
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line.startswith('#'):
                    if line:
                        break  # 数据开始，停止读取
                    continue
                line = line.lstrip('#').strip()
                # 匹配 "键: 值" 格式
                if ':' in line:
                    key, val = line.split(':', 1)
                    key = key.strip()
                    val = val.strip()
                    if '气压' in key or 'pressure' in key.lower():
                        meta['pressure'] = val.replace('Pa', '').strip()
                    elif '温度' in key or 'temp' in key.lower():
                        meta['temperature'] = val.replace('K', '').strip()
                    elif '冷凝距离' in key or 'condensation' in key.lower():
                        meta['condensation_distance'] = val.replace('cm', '').strip()
                    elif '功率' in key or 'power' in key.lower():
                        meta['power'] = val.replace('W', '').strip()
                    elif '元素集' in key or 'element' in key.lower():
                        meta['element_set'] = val
                    elif '加速电压' in key or 'voltage' in key.lower():
                        meta['acceleration_voltage'] = val
                    elif '靶材' in key:
                        meta['target_count'] = val
                    elif '探测器' in key or 'detector' in key.lower():
                        meta['detector'] = val
                    elif 'Mass公式' in key or 'mass' in key.lower():
                        meta['mass_formula'] = val
                    elif '电流范围' in key:
                        meta['current_range'] = val
    except Exception:
        pass
    return meta


def compute_baseline(x, y, n_segments=80, percentile=10):
    seg_size = max(1, len(x) // n_segments)
    bx, by = [], []
    for i in range(0, len(x), seg_size):
        seg = y[i:i+seg_size]
        if len(seg) > 0:
            bx.append(np.mean(x[i:i+seg_size]))
            by.append(np.percentile(seg, percentile))
    if len(bx) < 4:
        return np.full_like(y, np.median(y))
    try:
        spl = UnivariateSpline(bx, by, s=0.01 * (max(by) - min(by) + 1e-9))
        return spl(x)
    except Exception:
        return np.interp(x, bx, by)


@njit
def _numba_match_dist(peak_masses, formula_masses, tol):
    """Numba加速的距离计算，返回匹配索引"""
    n_peaks = len(peak_masses)
    n_form = len(formula_masses)
    # 预分配最大结果数
    max_matches = n_peaks * min(n_form, 100)
    result_peak = np.empty(max_matches, dtype=np.int32)
    result_form = np.empty(max_matches, dtype=np.int32)
    result_dist = np.empty(max_matches, dtype=np.float64)
    count = 0
    for i in prange(n_peaks):
        for j in range(n_form):
            d = abs(peak_masses[i] - formula_masses[j])
            if d <= tol:
                if count < max_matches:
                    result_peak[count] = i
                    result_form[count] = j
                    result_dist[count] = d
                    count += 1
    return result_peak[:count], result_form[:count], result_dist[:count]


def match_formulas_vectorized(peak_masses, formulas, tol):
    """
    向量化分子式匹配：用numpy广播替代双重循环，速度提升10-100倍。
    formulas: [(formula_str, mass, abund), ...]
    返回: {peak_mass: [(formula, calc_mass, err, abund), ...]}
    """
    if not formulas or len(peak_masses) == 0:
        return {}
    formula_masses = np.array([m for _, m, _ in formulas], dtype=np.float64)
    formula_list = [(f, m, a) for f, m, a in formulas]
    peak_arr = np.array(peak_masses, dtype=np.float64)
    result = {}

    if HAS_NUMBA and len(peak_arr) > 10 and len(formula_masses) > 50:
        # Numba加速路径（大数据量时更快）
        rp, rf, rd = _numba_match_dist(peak_arr, formula_masses, tol)
        temp = {}
        for k in range(len(rp)):
            i, j, d = int(rp[k]), int(rf[k]), float(rd[k])
            p = float(peak_arr[i])
            if p not in temp:
                temp[p] = []
            temp[p].append((formula_list[j][0], formula_list[j][1], d, formula_list[j][2]))
        for p, mt in temp.items():
            mt.sort(key=lambda x: x[2])
            result[p] = mt
    else:
        # NumPy向量化路径（小数据量时开销更小）
        chunk = 500
        for start in range(0, len(peak_arr), chunk):
            pm_chunk = peak_arr[start:start+chunk]
            dist = np.abs(pm_chunk[:, None] - formula_masses[None, :])
            for i, p in enumerate(pm_chunk):
                mask = dist[i] <= tol
                if mask.any():
                    idxs = np.where(mask)[0]
                    mt = [(formula_list[j][0], formula_list[j][1], dist[i, j], formula_list[j][2])
                          for j in idxs]
                    mt.sort(key=lambda x: x[2])
                    result[float(p)] = mt
    return result


def detect_peaks(x, y, height=None, distance=None, prominence=None, smooth=True):
    """
    质谱峰识别：先平滑再找峰，参数自适应。
    height: 绝对高度阈值，None 时自动用 y_max*0.003
    distance: 最小峰间距（点数），None 时自动用 max(3, len//500)
    prominence: 最小突出度，None 时自动用 y_max*0.002
    smooth: 是否先做 Savitzky-Golay 平滑
    """
    if len(y) < 10:
        return pd.DataFrame()

    y_max = y.max()
    if y_max <= 0:
        return pd.DataFrame()

    if height is None:
        height = y_max * 0.003
    if distance is None:
        distance = max(3, len(y) // 500)
    if prominence is None:
        prominence = y_max * 0.002

    # 先平滑（Savitzky-Golay，窗口自适应）
    if smooth:
        from scipy.signal import savgol_filter
        win = min(len(y) // 50 | 1, 15)  # 奇数窗口，最大15
        win = max(win, 5)
        if win % 2 == 0:
            win += 1
        try:
            y_smooth = savgol_filter(y, window_length=win, polyorder=2)
        except Exception:
            y_smooth = y
    else:
        y_smooth = y

    # 找峰
    peaks, props = find_peaks(y_smooth, height=height, distance=distance,
                              prominence=prominence)
    if len(peaks) == 0:
        # 降级：不用 prominence 再试一次
        peaks, props = find_peaks(y_smooth, height=height, distance=distance)
    if len(peaks) == 0:
        return pd.DataFrame()

    data = []
    for p in peaks:
        # 用原始数据的峰顶位置（在平滑峰附近找真实最大值）
        search_range = max(2, distance // 2)
        p_start = max(0, p - search_range)
        p_end = min(len(y) - 1, p + search_range)
        real_p = p_start + np.argmax(y[p_start:p_end + 1])

        half = y[real_p] / 2
        left = real_p
        while left > 0 and y[left] > half:
            left -= 1
        right = real_p
        while right < len(y) - 1 and y[right] > half:
            right += 1
        fwhm = x[right] - x[left] if right > left else 0
        area = (np.trapezoid(y[left:right+1], x[left:right+1])
        if hasattr(np, 'trapezoid') else np.trapz(y[left:right+1], x[left:right+1])) if right > left else 0
        data.append({'index': int(real_p), 'x': float(x[real_p]), 'height': float(y[real_p]),
                     'fwhm': float(fwhm), 'area': float(area),
                     'left': float(x[left]), 'right': float(x[right])})
    return pd.DataFrame(data)


def downsample(x, y, max_points=4000):
    """绘图降采样：数据点超过 max_points 时等距抽样"""
    if len(x) <= max_points:
        return x, y
    step = len(x) // max_points
    idx = np.arange(0, len(x), step)
    if idx[-1] != len(x) - 1:
        idx = np.append(idx, len(x) - 1)
    return x[idx], y[idx]


# ============================================================
# 元素同位素数据库 & 分子式匹配
# ============================================================
# 格式: 元素符号: [(质量, 丰度), ...]，按丰度降序
ELEMENT_ISOTOPES = {
    'H':  [(1.007825, 0.999885), (2.014102, 0.000115)],
    'He': [(4.002603, 0.999998)],
    'Li': [(7.016004, 0.9241), (6.015122, 0.0759)],
    'Be': [(9.012182, 1.0)],
    'B':  [(11.009305, 0.801), (10.012937, 0.199)],
    'C':  [(12.000000, 0.9893), (13.003355, 0.0107)],
    'N':  [(14.003074, 0.99636), (15.000109, 0.00364)],
    'O':  [(15.994915, 0.99757), (17.999160, 0.00205), (16.999132, 0.00038)],
    'F':  [(18.998403, 1.0)],
    'Ne': [(19.992440, 0.9048), (21.991385, 0.0925)],
    'Na': [(22.989769, 1.0)],
    'Mg': [(23.985042, 0.7899), (25.982593, 0.1101), (24.985837, 0.1000)],
    'Al': [(26.981539, 1.0)],
    'Si': [(27.976927, 0.92223), (28.976495, 0.04685), (29.973770, 0.03092)],
    'P':  [(30.973762, 1.0)],
    'S':  [(31.972071, 0.9493), (33.967867, 0.0429), (32.971458, 0.0076)],
    'Cl': [(34.968853, 0.7576), (36.965903, 0.2424)],
    'Ar': [(39.962383, 0.99604), (35.967546, 0.00337), (37.962732, 0.00063)],
    'K':  [(38.963707, 0.93258), (40.961826, 0.06730)],
    'Ca': [(39.962591, 0.96941), (41.958618, 0.00647), (42.958767, 0.00135)],
    'Ti': [(47.947947, 0.7372), (45.952632, 0.0825), (46.951763, 0.0744), (48.947871, 0.0541), (49.944792, 0.0518)],
    'V':  [(50.943964, 0.99750), (49.947163, 0.00250)],
    'Cr': [(51.940512, 0.83789), (52.940654, 0.09501), (49.946050, 0.04345), (53.938885, 0.02365)],
    'Mn': [(54.938050, 1.0)],
    'Fe': [(55.934942, 0.91754), (53.939615, 0.05845), (56.935400, 0.02119), (57.933280, 0.00282)],
    'Co': [(58.933200, 1.0)],
    'Ni': [(57.935348, 0.68077), (59.930791, 0.26223), (61.928349, 0.03635), (60.931060, 0.01140), (63.927970, 0.00926)],
    'Cu': [(62.929601, 0.6915), (64.927794, 0.3085)],
    'Zn': [(63.929147, 0.4863), (65.926037, 0.2790), (67.924848, 0.1875), (69.925325, 0.0062), (66.927131, 0.0410)],
    'Ga': [(68.925574, 0.60108), (70.924707, 0.39892)],
    'Ge': [(73.921178, 0.3628), (71.922076, 0.2754), (69.924250, 0.2084), (72.923459, 0.0773), (75.921403, 0.0761), (70.924954, 0.0)],
    'As': [(74.921596, 1.0)],
    'Se': [(79.916522, 0.4961), (77.917310, 0.2377), (81.916700, 0.0873), (73.922477, 0.0089), (75.919214, 0.0937), (76.919915, 0.0763), (80.917997, 0.0)],
    'Br': [(78.918338, 0.5069), (80.916291, 0.4931)],
    'Kr': [(83.911507, 0.5699), (85.910610, 0.1728), (82.914136, 0.1159), (79.916379, 0.0225), (81.913484, 0.1150), (77.920364, 0.0035), (84.912527, 0.0)],
    'Rb': [(84.911790, 0.7217), (86.909181, 0.2783)],
    'Sr': [(87.905612, 0.8258), (85.909260, 0.0986), (86.908877, 0.0700), (83.913425, 0.0056)],
    'Y':  [(88.905848, 1.0)],
    'Zr': [(89.904704, 0.5145), (90.905645, 0.1122), (93.906316, 0.1738), (91.905040, 0.1715), (95.908273, 0.0280)],
    'Nb': [(92.906378, 1.0)],
    'Mo': [(97.905408, 0.2413), (94.905842, 0.1587), (95.904681, 0.1667), (91.906811, 0.1484), (99.907478, 0.0963), (96.906022, 0.0959), (92.906811, 0.0)],
    'Ru': [(101.904349, 0.3155), (98.905939, 0.1262), (99.904220, 0.1250), (103.905433, 0.1867), (95.907598, 0.0554), (97.905287, 0.0188), (100.905582, 0.1723)],
    'Rh': [(102.905504, 1.0)],
    'Pd': [(105.903483, 0.2733), (107.903894, 0.2646), (104.905084, 0.2233), (109.905152, 0.1172), (101.905609, 0.0102), (106.905127, 0.1114)],
    'Ag': [(106.905093, 0.51839), (108.904756, 0.48161)],
    'Cd': [(113.903358, 0.2873), (111.902757, 0.1280), (110.904182, 0.1275), (115.904755, 0.0748), (112.904401, 0.1239), (116.907228, 0.0761), (109.903002, 0.0125), (117.904818, 0.0)],
    'In': [(114.903878, 0.9571), (112.904061, 0.0429)],
    'Sn': [(119.902197, 0.3258), (117.901609, 0.2422), (115.901744, 0.1454), (118.903311, 0.0859), (121.903440, 0.0463), (116.902954, 0.0768), (122.905722, 0.0429), (113.902782, 0.0066), (114.903346, 0.0034), (123.905275, 0.0579)],
    'Sb': [(120.903818, 0.5721), (122.904216, 0.4279)],
    'Te': [(129.906223, 0.3380), (127.904461, 0.2549), (125.903312, 0.1897), (126.905223, 0.1884), (123.902818, 0.0009), (124.904428, 0.0707), (128.905934, 0.0)],
    'I':  [(126.904473, 1.0)],
    'Xe': [(131.904155, 0.2689), (128.904780, 0.2644), (130.905082, 0.2118), (127.903531, 0.0192), (125.904274, 0.0009), (126.905182, 0.0)],
    'Cs': [(132.905452, 1.0)],
    'Ba': [(137.905247, 0.7170), (135.904576, 0.0785), (136.905827, 0.1123), (134.905686, 0.0659), (132.906009, 0.0010), (130.906931, 0.0010)],
    'La': [(138.906353, 0.99910), (137.907112, 0.00090)],
    'Ce': [(139.905439, 0.88450), (141.909244, 0.11114), (136.907789, 0.00185), (140.119996, 0.0)],
    'Pr': [(140.907653, 1.0)],
    'Nd': [(141.907723, 0.27152), (143.910087, 0.23798), (144.912573, 0.08295), (145.913116, 0.17189), (147.916893, 0.05638), (142.909814, 0.12174), (146.916097, 0.17189), (148.0)],
    'Sm': [(151.919732, 0.2675), (153.922209, 0.2275), (149.917276, 0.1382), (152.921230, 0.1068), (147.914823, 0.0307), (150.36), (154.924642, 0.1499), (146.914898, 0.0307)],
    'Eu': [(152.921230, 0.4781), (150.919850, 0.4781), (154.922890, 0.0)],
    'Gd': [(157.924103, 0.2484), (155.922123, 0.2047), (156.923960, 0.1565), (159.927054, 0.2186), (154.922622, 0.0218), (151.919791, 0.0020), (158.926389, 0.1480)],
    'Tb': [(158.925346, 1.0)],
    'Dy': [(163.929175, 0.2826), (161.926798, 0.2551), (160.926933, 0.1889), (162.928731, 0.2490), (157.924409, 0.0010), (159.925197, 0.0), (155.924283, 0.0)],
    'Ho': [(164.930322, 1.0)],
    'Er': [(165.930293, 0.3359), (167.932370, 0.2690), (169.935464, 0.1489), (166.932048, 0.2287), (161.928778, 0.0014), (163.929200, 0.0161), (168.934590, 0.0)],
    'Tm': [(168.934213, 1.0)],
    'Yb': [(173.938862, 0.3183), (171.936382, 0.2186), (172.938211, 0.1613), (175.942572, 0.1276), (169.934762, 0.0013), (170.936326, 0.1410), (174.941633, 0.0)],
    'Lu': [(174.940772, 0.9741), (175.942686, 0.0259)],
    'Hf': [(179.946550, 0.3510), (177.943698, 0.2730), (176.943220, 0.1860), (178.945816, 0.1363), (173.940040, 0.0016), (175.941408, 0.0521)],
    'Ta': [(180.947996, 0.99988), (179.947466, 0.00012)],
    'W':  [(183.950933, 0.3064), (185.954364, 0.2843), (182.950223, 0.1431), (181.948206, 0.2650), (179.946706, 0.0012)],
    'Re': [(186.955751, 0.6260), (184.952956, 0.3740)],
    'Os': [(191.961481, 0.4093), (189.958447, 0.1615), (190.960930, 0.2625), (192.964148, 0.1316), (186.955751, 0.0159), (187.955839, 0.0196), (188.958145, 0.0)],
    'Ir': [(192.962924, 0.6275), (190.960591, 0.3730)],
    'Pt': [(194.964774, 0.3383), (195.964935, 0.2524), (193.962664, 0.3297), (197.967876, 0.0716), (190.961666, 0.0001), (191.961035, 0.0079), (196.967323, 0.0)],
    'Au': [(196.966552, 1.0)],
    'Hg': [(201.970626, 0.2986), (199.968300, 0.2310), (200.970277, 0.1318), (197.966743, 0.0997), (203.973476, 0.0687), (195.965807, 0.0015), (196.966552, 0.0), (198.968259, 0.1687)],
    'Tl': [(204.974412, 0.7048), (202.972329, 0.2952)],
    'Pb': [(207.976636, 0.5240), (205.974449, 0.2410), (206.975881, 0.2210), (203.973029, 0.0140)],
    'Bi': [(208.980383, 1.0)],
}

# Unicode 上角标数字
_SUP = {'0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴',
        '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹'}


def _to_superscript(num):
    """数字转上角标字符串（保留兼容，不再用于绘图）"""
    return ''.join(_SUP.get(c, c) for c in str(int(round(num))))


def formula_to_mathtext(formula):
    """
    把纯文本分子式（如 107Ag2 63Cu）转为 matplotlib mathtext 格式。
    质量数 → 上角标，元素后数字 → 下角标。
    例：107Ag2 63Cu → $^{107}$Ag$_2$ $^{63}$Cu
    """
    import re
    # 匹配：数字(质量数) + 元素符号 + 可选数字(个数)
    pattern = r'(\d+)([A-Z][a-z]?)(\d*)'
    def _repl(m):
        mass = m.group(1)
        elem = m.group(2)
        cnt = m.group(3)
        if cnt:
            return f"$^{{{mass}}}${elem}$_{{{cnt}}}$"
        else:
            return f"$^{{{mass}}}${elem}"
    return re.sub(pattern, _repl, formula)


def sort_formula_by_mass(formula):
    """
    对分子式中的同位素组按元素质量从重到轻排序，同一元素按质量数从大到小。
    如 '75As 69Ga2' -> '69Ga2 75As'（Ga比As轻，所以重的As在后面？不，重的在前）
    实际：As(74.9) > Ga(69.7)，所以 As 在前 -> '75As 69Ga2'
    """
    import re as _re
    parts = formula.split()
    parsed = []
    for p in parts:
        m = _re.match(r'^(\d+)([A-Z][a-z]?)(\d*)$', p)
        if m:
            mass_num = int(m.group(1))
            elem = m.group(2)
            cnt = m.group(3)
            # 用最丰同位素质量作为元素原子量近似
            elem_mass = get_most_abundant_mass(elem) or mass_num
            parsed.append((elem_mass, mass_num, elem, cnt, p))
        else:
            parsed.append((0, 0, '', '', p))
    # 按元素原子量降序，同一元素按质量数降序
    parsed.sort(key=lambda x: (-x[0], -x[1]))
    return ' '.join(p[4] for p in parsed)


# 同位素组合预计算缓存
_isotope_combo_cache = {}
_isotope_combo_cache_loaded = False

def _load_isotope_cache():
    """启动时加载预计算的同位素组合缓存"""
    global _isotope_combo_cache_loaded, _isotope_combo_cache
    if _isotope_combo_cache_loaded:
        return
    cache_file = _get_cache_path()
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            for key, val in raw.items():
                _isotope_combo_cache[key] = [tuple(item) for item in val]
        except Exception:
            pass
    _isotope_combo_cache_loaded = True

def _get_cache_path():
    """获取缓存文件路径，兼容脚本和打包exe"""
    if getattr(sys, 'frozen', False):
        # PyInstaller打包后，用exe所在目录
        return os.path.join(os.path.dirname(sys.executable), 'isotope_cache.json')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'isotope_cache.json')

def _save_isotope_cache():
    """保存同位素组合缓存到JSON（非空才保存）"""
    if not _isotope_combo_cache:
        return
    cache_file = _get_cache_path()
    try:
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(_isotope_combo_cache, f, ensure_ascii=False)
    except Exception:
        pass

def _element_isotope_combos(element, count):
    """
    生成单个元素 count 个原子的所有同位素组合（带缓存）。
    返回 [(质量, 相对丰度, [(元素,质量数,个数),...]), ...]
    """
    cache_key = f"{element}_{count}"
    if cache_key in _isotope_combo_cache:
        return _isotope_combo_cache[cache_key]
    isotopes = ELEMENT_ISOTOPES.get(element, [])
    if not isotopes or count == 0:
        result = [(0.0, 1.0, [])]
        _isotope_combo_cache[cache_key] = result
        return result
    results = []
    n = len(isotopes)

    def _gen(idx, remaining, mass, abund, desc):
        if idx == n:
            if remaining == 0:
                results.append((mass, abund, desc))
            return
        iso_m, iso_a = isotopes[idx]
        for c in range(remaining + 1):
            new_mass = mass + iso_m * c
            new_abund = abund * (iso_a ** c)
            new_desc = desc + ([(element, int(round(iso_m)), c)] if c > 0 else [])
            _gen(idx + 1, remaining - c, new_mass, new_abund, new_desc)

    _gen(0, count, 0.0, 1.0, [])
    _isotope_combo_cache[cache_key] = results
    # 每计算一个新组合就保存，避免意外丢失
    if len(_isotope_combo_cache) % 5 == 0:
        _save_isotope_cache()
    return results


def generate_cluster_isotope_formulas(elements, max_counts, charge=1, min_abundance=0.005,
                                          progress_cb=None, max_results=500000):
    """
    生成所有团簇的同位素组合（递归+丰度剪枝，比itertools.product快10-100倍）。
    elements: 元素列表
    max_counts: {元素: 最大个数}
    min_abundance: 最小相对丰度过滤（默认0.5%）
    progress_cb: 进度回调函数(processed, total)
    max_results: 最大结果数，防止内存爆炸
    返回 [(分子式字符串, 质量, 丰度), ...]
    """
    if not elements:
        return []

    # 预计算每个元素的所有同位素组合（含个数0）
    elem_choices = []
    total_combos = 1
    for elem in elements:
        mx = max_counts.get(elem, 10)
        choices = []
        for cnt in range(mx + 1):
            choices.extend(_element_isotope_combos(elem, cnt))
        elem_choices.append(choices)
        total_combos *= len(choices)

    results = []
    _cancel = [False]

    def _recurse(idx, cur_mass, cur_abund, cur_desc):
        if _cancel[0] or len(results) >= max_results:
            return
        if cur_abund < min_abundance and cur_mass > 0:
            return  # 丰度剪枝：累积丰度已低于阈值，后续只会更低
        if idx == len(elements):
            if cur_mass > 0:
                formula_parts = []
                for elem, mass_num, cnt in cur_desc:
                    if cnt > 0:
                        formula_parts.append(f"{mass_num}{elem}{cnt}" if cnt > 1 else f"{mass_num}{elem}")
                formula = ' '.join(formula_parts)
                results.append((formula, cur_mass - 0.00054858 * charge, cur_abund))
            return
        for choice in elem_choices[idx]:
            new_mass = cur_mass + choice[0]
            new_abund = cur_abund * choice[1]
            new_desc = cur_desc + choice[2]
            _recurse(idx + 1, new_mass, new_abund, new_desc)

    _recurse(0, 0.0, 1.0, [])
    results.sort(key=lambda x: x[1])

    if progress_cb:
        progress_cb(len(results), total_combos)

    return results


def match_peaks_to_isotope_formulas(peaks_df, elements, max_counts, tolerance=0.5,
                                     charge=1, min_abundance=0.005):
    """
    将峰位与同位素分子式匹配（向量化加速）。
    返回 {峰位x: (分子式, 计算质量, 误差, 丰度)}（每个峰取最佳匹配）
    """
    formulas = generate_cluster_isotope_formulas(elements, max_counts, charge, min_abundance)
    if not formulas or peaks_df is None or len(peaks_df) == 0:
        return {}
    # 向量化匹配
    peak_x = peaks_df['x'].values
    formula_masses = np.array([m for _, m, _ in formulas])
    matches = {}
    chunk = 500
    for start in range(0, len(peak_x), chunk):
        px_chunk = peak_x[start:start+chunk]
        dist = np.abs(px_chunk[:, None] - formula_masses[None, :])
        for i, px in enumerate(px_chunk):
            mask = dist[i] <= tolerance
            if mask.any():
                idxs = np.where(mask)[0]
                best_j = idxs[np.argmin(dist[i, idxs])]
                matches[float(px)] = (formulas[best_j][0], formulas[best_j][1],
                                      dist[i, best_j], formulas[best_j][2])
    return matches


def parse_elements_from_name(name):
    """从样品名称中解析元素符号，返回元素列表"""
    import re
    # 匹配元素符号（大写开头，可选小写）
    pattern = r'([A-Z][a-z]?)'
    found = re.findall(pattern, name)
    # 过滤掉不在数据库中的
    return [e for e in found if e in ELEMENT_ISOTOPES]


def get_most_abundant_mass(element):
    """获取元素最丰同位素的质量"""
    if element in ELEMENT_ISOTOPES:
        return ELEMENT_ISOTOPES[element][0][0]
    return None


def generate_cluster_formulas(elements, max_counts, charge=1):
    """
    生成所有可能的团簇分子式及其最丰同位素质量。
    elements: 元素列表，如 ['Ag', 'Ar']
    max_counts: 每个元素的最大个数，如 {'Ag': 10, 'Ar': 3}
    返回: [(分子式字符串, 质量), ...]
    """
    from itertools import product
    if not elements:
        return []

    ranges = [range(0, max_counts.get(e, 5) + 1) for e in elements]
    results = []
    for counts in product(*ranges):
        if sum(counts) == 0:
            continue  # 跳过全零
        mass = 0.0
        formula_parts = []
        for elem, cnt in zip(elements, counts):
            if cnt > 0:
                m = get_most_abundant_mass(elem)
                if m is None:
                    continue
                mass += m * cnt
                formula_parts.append(f"{elem}{cnt}" if cnt > 1 else elem)
        if formula_parts:
            formula = ''.join(formula_parts)
            # 扣除电子质量（单电荷）
            mass -= 0.00054858 * charge
            results.append((formula, mass))
    return results


def match_peaks_to_formulas(peaks_df, elements, max_counts, tolerance=0.5, charge=1):
    """
    将峰位与分子式匹配。
    peaks_df: 峰数据DataFrame，需含 'x' 列
    elements: 元素列表
    max_counts: 元素个数限制 dict
    tolerance: 质量容差 (Da)
    返回: {峰位x: (分子式, 计算质量, 误差)}
    """
    formulas = generate_cluster_formulas(elements, max_counts, charge)
    if not formulas or peaks_df is None or len(peaks_df) == 0:
        return {}

    matches = {}
    for _, peak in peaks_df.iterrows():
        px = peak['x']
        best = None
        best_err = float('inf')
        for formula, mass in formulas:
            err = abs(px - mass)
            if err < best_err:
                best_err = err
                best = (formula, mass, err)
        if best and best_err <= tolerance:
            matches[px] = best
    return matches


def parse_date_from_path(filepath):
    """从文件路径的目录名中解析实验日期"""
    import re as _re
    dirname = os.path.dirname(os.path.abspath(filepath))
    dir_name = os.path.basename(dirname)
    parent_name = os.path.basename(os.path.dirname(dirname))
    text = f"{dir_name} {parent_name} {os.path.basename(filepath)}"

    # 匹配各种日期格式
    patterns = [
        (r'(\d{4})[-_./](\d{1,2})[-_./](\d{1,2})', 0),  # 2024-01-15
        (r'(\d{4})(\d{2})(\d{2})', 0),                   # 20240115
        (r'(\d{1,2})[-_./](\d{1,2})[-_./](\d{4})', 2),  # 01-15-2024
    ]
    for pat, year_idx in patterns:
        m = _re.search(pat, text)
        if m:
            try:
                if year_idx == 0:
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                else:
                    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                    return f"{y:04d}-{mo:02d}-{d:02d}"
            except Exception:
                pass
    # 匹配年月（如 2024-01）
    m = _re.search(r'(\d{4})[-_./](\d{1,2})', text)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 2000 <= y <= 2100 and 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}-01"
    # 用文件修改时间
    try:
        mtime = os.path.getmtime(filepath)
        return datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d')
    except Exception:
        return datetime.date.today().isoformat()


def parse_experiment_params(filename):
    """
    从文件名/谱图名称中自动解析实验参数。
    规则：
      - Ar后面三位数字 → Ar流量(sccm)，如 Ar050 → 50
      - He后面三位数字 → He流量(sccm)，如 He010 → 10
      - W前面的数字 → 功率(W)，如 120W → 120
      - 包含独立的Y → 已通入液氮
      - Pa前面五位数字 → 气压(Pa)，后两位为小数，如 00800Pa → 8.00
      - cm前面的数字 → 冷凝距离(cm)，如 180cm → 180
    """
    import re
    params = {
        'ar_flow': '', 'he_flow': '', 'liquid_n2': False,
        'power': '', 'condensation_distance': '', 'pressure': '',
    }
    name = os.path.splitext(os.path.basename(filename))[0]

    m = re.search(r'Ar(\d{3})', name, re.IGNORECASE)
    if m:
        params['ar_flow'] = str(int(m.group(1)))

    m = re.search(r'He(\d{3})', name, re.IGNORECASE)
    if m:
        params['he_flow'] = str(int(m.group(1)))

    m = re.search(r'(\d+(?:\.\d+)?)\s*W(?!a|att)', name, re.IGNORECASE)
    if m:
        params['power'] = m.group(1)

    m = re.search(r'(\d{5})\s*Pa', name, re.IGNORECASE)
    if m:
        digits = m.group(1)
        pressure = int(digits[:3]) + int(digits[3:]) / 100.0
        params['pressure'] = f"{pressure:.2f}"
    else:
        m = re.search(r'(\d+(?:\.\d+)?)\s*Pa', name, re.IGNORECASE)
        if m:
            params['pressure'] = m.group(1)

    m = re.search(r'(\d+(?:\.\d+)?)\s*cm', name, re.IGNORECASE)
    if m:
        params['condensation_distance'] = m.group(1)

    if re.search(r'(?:^|[^A-Za-z0-9])Y(?:[^A-Za-z0-9]|$)', name):
        params['liquid_n2'] = True
    elif re.search(r'[Nn]2|液氮|LN2', name):
        params['liquid_n2'] = True

    return params


# ============================================================
# 主应用
# ============================================================
class MassSpectrumAppPro:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_TITLE}  {APP_VERSION}")
        # 自适应屏幕大小
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = min(1400, int(sw * 0.92))
        h = min(880, int(sh * 0.88))
        x = (sw - w) // 2
        y = (sh - h) // 3
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.minsize(900, 600)
        self.root.configure(bg=BG)
        # 去掉窗口默认标题栏的重复（保留系统标题栏）

        self.db = SpectrumDB()
        _load_isotope_cache()
        self.spectra = []
        self.color_idx = 0
        self.current_group_id = None

        # 显示选项
        self.show_peaks = tk.BooleanVar(value=False)
        self.show_baseline = tk.BooleanVar(value=False)
        self.y_log = tk.BooleanVar(value=False)
        self.search_var = tk.StringVar()
        self.tag_var = tk.StringVar()

        self._build_ui()
        self._load_from_db()
        self._update_status("就绪。")

    # ----------------------------------------------------------
    # UI 构建
    # ----------------------------------------------------------
    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except Exception:
            pass
        style.configure('TFrame', background=BG)
        style.configure('Panel.TFrame', background=PANEL)
        style.configure('Panel2.TFrame', background=PANEL2)
        style.configure('TLabel', background=BG, foreground=TEXT, font=('SimSun', 9))
        style.configure('Panel.TLabel', background=PANEL, foreground=TEXT, font=('SimSun', 9))
        style.configure('Panel2.TLabel', background=PANEL2, foreground=TEXT, font=('SimSun', 9))
        style.configure('Title.TLabel', background=PANEL, foreground=ACCENT,
                        font=('SimSun', 11, 'bold'))
        style.configure('Muted.TLabel', background=PANEL, foreground=MUTED, font=('SimSun', 8))
        # 按钮样式 - 深青科技
        style.configure('TButton', font=('SimSun', 9), background=PANEL2, foreground=TEXT,
                        bordercolor=BORDER, focusthickness=0, padding=[10, 5], relief='flat')
        style.map('TButton',
          background=[('active', HOVER), ('pressed', ACCENT)],
          foreground=[('active', TEXT), ('pressed', TEXT)])
        style.configure('Accent.TButton', font=('SimSun', 9, 'bold'),
                        background=ACCENT, foreground='#0a1929',
                        bordercolor=ACCENT, padding=[12, 6], relief='flat')
        style.map('Accent.TButton',
          background=[('active', '#4de8c0'), ('pressed', '#3dd4ac')])
        style.configure('TCheckbutton', background=PANEL, foreground=TEXT, font=('SimSun', 9))
        # 树形视图 - 斑马纹效果
        style.configure('Treeview', font=('SimSun', 9), rowheight=32,
                        background=PANEL, foreground=TEXT, fieldbackground=PANEL,
                        bordercolor=BORDER)
        style.configure('Treeview.Heading', font=('SimSun', 9, 'bold'),
                        background=PANEL2, foreground=ACCENT, bordercolor=BORDER, padding=[8, 6])
        style.map('Treeview',
          background=[('selected', SELECTED)],
          foreground=[('selected', '#ffffff')])
        # 输入框
        style.configure('TEntry', fieldbackground=BG, foreground=TEXT, bordercolor=BORDER,
                        insertcolor=ACCENT)
        # 笔记本
        style.configure('TNotebook', background=PANEL, bordercolor=BORDER)
        style.configure('TNotebook.Tab', font=('SimSun', 9), padding=[14, 7],
                        background=PANEL, foreground=MUTED)
        style.map('TNotebook.Tab',
          background=[('selected', PANEL2)],
          foreground=[('selected', ACCENT)])
        # 标签框
        style.configure('TLabelframe', background=PANEL, bordercolor=BORDER)
        style.configure('TLabelframe.Label', background=PANEL, foreground=ACCENT,
                        font=('SimSun', 9, 'bold'))
        # 进度条
        style.configure('Horizontal.TProgressbar', background=ACCENT, troughcolor=PANEL2,
                        bordercolor=BORDER)
        # 单选框
        style.configure('TRadiobutton', background=PANEL, foreground=TEXT, font=('SimSun', 9))
        # 组合框
        style.configure('TCombobox', fieldbackground=BG, foreground=TEXT, background=PANEL2,
                        bordercolor=BORDER)
        # 滚动条
        style.configure('Vertical.TScrollbar', background=PANEL2, troughcolor=PANEL,
                        bordercolor=BORDER, arrowcolor=ACCENT)
        style.configure('Horizontal.TScrollbar', background=PANEL2, troughcolor=PANEL,
                        bordercolor=BORDER, arrowcolor=ACCENT)

        self._build_header()  # 顶部logo标题栏
        self._build_menu()
        self._build_toolbar()

        # 可拖动分割的主布局
        main = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # 左侧面板（谱图列表 + 分组 + 搜索）
        self.left = ttk.Frame(main, style='Panel.TFrame', width=420)
        self.left.pack_propagate(False)
        self._build_left_panel()
        main.add(self.left, weight=1)

        # 右侧（绘图区 + 元数据）
        self.right = ttk.Frame(main)
        self._build_right_panel()
        main.add(self.right, weight=4)

        # 状态栏
        self.status_var = tk.StringVar()
        ttk.Label(self.root, textvariable=self.status_var, anchor=tk.W,
          background=PANEL, foreground=MUTED, font=('Segoe UI', 8)
          ).pack(fill=tk.X, side=tk.BOTTOM)

    def _build_header(self):
        """顶部logo标题栏 - 现代简约风格"""
        header = tk.Frame(self.root, bg=BG, height=60)
        header.pack(fill=tk.X, side=tk.TOP)
        header.pack_propagate(False)

        # 左侧强调条
        accent_bar = tk.Frame(header, bg=ACCENT, width=3, height=60)
        accent_bar.pack(side=tk.LEFT)

        # Logo区域
        try:
            logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logo.png')
            if not os.path.exists(logo_path):
                logo_path = 'logo.png'
            self.logo_img = tk.PhotoImage(file=logo_path).subsample(7, 7)
            logo_label = tk.Label(header, image=self.logo_img, bg=BG)
            logo_label.pack(side=tk.LEFT, padx=12, pady=8)
        except Exception:
            pass

        # 标题区域
        title_frame = tk.Frame(header, bg=BG)
        title_frame.pack(side=tk.LEFT, pady=12)
        tk.Label(title_frame, text="南京原子制造研究所",
         font=('SimSun', 14, 'bold'), fg=TEXT, bg=BG).pack(anchor=tk.W)
        tk.Label(title_frame, text="Mass Spectrum Manager Pro  ·  质谱图管理系统",
         font=('SimSun', 8), fg=TEXT_DIM, bg=BG).pack(anchor=tk.W)

        # 右侧状态区
        right_frame = tk.Frame(header, bg=BG)
        right_frame.pack(side=tk.RIGHT, padx=20, pady=12)
        self.status_dot = tk.Label(right_frame, text="●", fg=SUCCESS, bg=BG, font=('SimSun', 10))
        self.status_dot.pack(side=tk.RIGHT, padx=6)
        tk.Label(right_frame, text="系统就绪", font=('SimSun', 9), fg=TEXT_DIM, bg=BG).pack(side=tk.RIGHT)

        # 底部分隔线
        tk.Frame(header, bg=BORDER, height=1).pack(side=tk.BOTTOM, fill=tk.X)

    def _build_menu(self):
        menubar = tk.Menu(self.root, bg=BG, fg=TEXT, activebackground=HOVER, activeforeground=TEXT)
        # 文件
        m_file = tk.Menu(menubar, tearoff=0, bg=BG, fg=TEXT, activebackground=HOVER, activeforeground=TEXT)
        m_file.add_command(label="导入文件...", command=self.import_files, accelerator="Ctrl+O")
        m_file.add_command(label="导入文件夹...", command=self.import_folder)
        m_file.add_separator()
        m_file.add_command(label="导出选中图片...", command=self.export_image)
        m_file.add_command(label="导出选中数据...", command=self.export_data)
        m_file.add_command(label="批量导出所有...", command=self.batch_export)
        m_file.add_separator()
        m_file.add_command(label="合并数据库...", command=self.merge_database)
        m_file.add_command(label="生成质谱库文件夹...", command=self.export_library, accelerator="Ctrl+L")
        m_file.add_separator()
        m_file.add_command(label="退出", command=self.root.quit)
        menubar.add_cascade(label="文件", menu=m_file)
        # 编辑
        m_edit = tk.Menu(menubar, tearoff=0, bg=BG, fg=TEXT, activebackground=HOVER, activeforeground=TEXT)
        m_edit.add_command(label="批量基线扣除", command=self.batch_baseline)
        m_edit.add_command(label="批量峰识别", command=self.batch_peak_detect)
        m_edit.add_command(label="批量归一化", command=self.batch_normalize)
        m_edit.add_command(label="批量还原", command=self.batch_reset)
        menubar.add_cascade(label="批量处理", menu=m_edit)
        # 视图
        m_view = tk.Menu(menubar, tearoff=0, bg=BG, fg=TEXT, activebackground=HOVER, activeforeground=TEXT)
        m_view.add_command(label="单图查看", command=self.plot_single)
        m_view.add_command(label="叠加对比", command=self.plot_overlay)
        m_view.add_command(label="网格子图", command=self.plot_grid)
        m_view.add_separator()
        m_view.add_command(label="字体设置...", command=self.open_font_settings)
        menubar.add_cascade(label="视图", menu=m_view)
        # 帮助
        m_help = tk.Menu(menubar, tearoff=0, bg=BG, fg=TEXT, activebackground=HOVER, activeforeground=TEXT)
        m_help.add_command(label="关于", command=lambda: messagebox.showinfo(
            "关于", f"{APP_TITLE}\n{APP_VERSION}\n\n专业质谱图管理软件"))
        menubar.add_cascade(label="帮助", menu=m_help)
        self.root.config(menu=menubar)
        self.root.bind('<Control-o>', lambda e: self.import_files())
        self.root.bind('<Control-l>', lambda e: self.export_library())

    def _build_toolbar(self):
        bar = ttk.Frame(self.root, style='Panel.TFrame')
        bar.pack(fill=tk.X, side=tk.TOP)
        btns = [
            ("[导入]", self.import_files, ''),
            ("[文件夹]", self.import_folder, ''),
            ("|", None, ''),
            ("[单图]", self.plot_single, ''),
            ("[叠加]", self.plot_overlay, ''),
            ("[网格]", self.plot_grid, ''),
            ("|", None, ''),
            ("[去基线]", self.batch_baseline, ''),
            ("[峰识别]", self.batch_peak_detect, ''),
            ("[归一化]", self.batch_normalize, ''),
            ("[还原]", self.batch_reset, ''),
            ("|", None, ''),
            ("💾 导出图", self.export_image, ''),
            ("📊 导出数据", self.export_data, ''),
            ("[分子式预计算]", self.open_formula_calculator, ''),
            ("[高精度标注]", self.open_high_precision_annotator, ''),
            ("[扫谱]", self.open_scan_spectrum, ''),
            ("[理论计算]", self.open_theoretical_ms, ''),
            ("📚 质谱库", self.export_library, ''),
            ("🗑 删除", self.remove_selected, ''),
        ]
        for text, cmd, _ in btns:
            if text == "|":
                ttk.Frame(bar, width=2, style='Panel.TFrame').pack(side=tk.LEFT, fill=tk.Y, padx=5, pady=6)
            else:
                ttk.Button(bar, text=text, command=cmd).pack(side=tk.LEFT, padx=2, pady=4)

    def _build_left_panel(self):
        # 搜索栏
        search_frame = ttk.Frame(self.left, style='Panel.TFrame')
        search_frame.pack(fill=tk.X, padx=8, pady=(8, 4))
        ttk.Label(search_frame, text="🔍", style='Panel.TLabel').pack(side=tk.LEFT)
        search_entry = ttk.Entry(search_frame, textvariable=self.search_var)
        search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        search_entry.bind('<KeyRelease>', lambda e: self._do_search())
        ttk.Button(search_frame, text="筛选", width=5, command=self._do_search).pack(side=tk.LEFT)

        # 标签筛选
        tag_frame = ttk.Frame(self.left, style='Panel.TFrame')
        tag_frame.pack(fill=tk.X, padx=8, pady=2)
        ttk.Label(tag_frame, text="标签:", style='Panel.TLabel').pack(side=tk.LEFT)
        self.tag_combo = ttk.Combobox(tag_frame, textvariable=self.tag_var, width=12, state='readonly')
        self.tag_combo.pack(side=tk.LEFT, padx=4)
        self.tag_combo.bind('<<ComboboxSelected>>', lambda e: self._do_search())

        # 分组列表
        group_frame = ttk.LabelFrame(self.left, text="分组", padding=4, style='Panel.TFrame')
        group_frame.pack(fill=tk.X, padx=8, pady=4)
        self.group_listbox = tk.Listbox(group_frame, height=4, font=('Segoe UI', 9),
                                        bg=BG, fg=TEXT, selectbackground=ACCENT,
                                        activestyle='none', relief='flat')
        self.group_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.group_listbox.bind('<<ListboxSelect>>', self._on_group_select)
        gb = ttk.Frame(group_frame, style='Panel.TFrame')
        gb.pack(side=tk.RIGHT, fill=tk.Y, padx=2)
        ttk.Button(gb, text="+", width=3, command=self.add_group).pack(pady=1)
        ttk.Button(gb, text="-", width=3, command=self.delete_group).pack(pady=1)

        # 谱图列表
        list_frame = ttk.LabelFrame(self.left, text="谱图列表", padding=4, style='Panel.TFrame')
        list_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        self.tree = ttk.Treeview(list_frame, columns=(
            'elem', 'power', 'ar', 'he', 'pressure', 'distance', 'temp', 'voltage'),
                                 show='tree headings', selectmode='extended')
        self.tree.heading('#0', text='名称', anchor='w')
        self.tree.column('#0', width=90, anchor='w')
        for col, text, w in [
            ('elem', '元素集', 55), ('power', '功率', 40),
            ('ar', 'Ar', 30), ('he', 'He', 30),
            ('pressure', '气压', 45), ('distance', '距离', 40),
            ('temp', '温度', 40), ('voltage', '电压', 45)]:
            self.tree.heading(col, text=text)
            self.tree.column(col, width=w, anchor='center')

        vsb = ttk.Scrollbar(list_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind('<<TreeviewSelect>>', self._on_select)
        self.tree.bind('<Double-1>', self._on_double_click)

        # 显示选项
        opt = ttk.LabelFrame(self.left, text="显示选项", padding=6, style='Panel.TFrame')
        opt.pack(fill=tk.X, padx=8, pady=4)
        ttk.Checkbutton(opt, text="标注峰位", variable=self.show_peaks,
                        command=self.refresh_plot).pack(anchor='w', pady=1)
        ttk.Checkbutton(opt, text="显示基线", variable=self.show_baseline,
                        command=self.refresh_plot).pack(anchor='w', pady=1)
        ttk.Checkbutton(opt, text="Y轴对数", variable=self.y_log,
                        command=self.refresh_plot).pack(anchor='w', pady=1)

        # 操作按钮
        btnf = ttk.Frame(self.left, style='Panel.TFrame')
        btnf.pack(fill=tk.X, padx=8, pady=(2, 4))
        for text, cmd in [("改颜色", self.change_color), ("重命名", self.rename_spectrum),
                          ("元数据", self.edit_metadata), ("加入分组", self.assign_group)]:
            ttk.Button(btnf, text=text, command=cmd).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
        btnf2 = ttk.Frame(self.left, style='Panel.TFrame')
        btnf2.pack(fill=tk.X, padx=8, pady=(0, 8))
        ttk.Button(btnf2, text="🔍 自动读取参数", command=self.auto_parse_params,
           style='Accent.TButton').pack(fill=tk.X, padx=1)

    def _build_right_panel(self):
        # 上下可拖动分割
        vpaned = ttk.PanedWindow(self.right, orient=tk.VERTICAL)
        vpaned.pack(fill=tk.BOTH, expand=True)

        # 上部：绘图区
        plot_frame = ttk.Frame(vpaned)
        self.fig = Figure(figsize=(8, 5), dpi=100, facecolor=BG)
        self.ax = self.fig.add_subplot(111)
        # 顶部工具栏：导航工具 + 坐标显示
        top_bar = ttk.Frame(plot_frame)
        top_bar.pack(fill=tk.X, side=tk.TOP)
        # 左侧：matplotlib导航工具栏
        tb_frame = ttk.Frame(top_bar)
        tb_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.mpl_toolbar = NavigationToolbar2Tk(self.canvas, tb_frame) if hasattr(self, 'canvas') else None
        # 右侧：鼠标坐标显示
        self.mouse_coord_var = tk.StringVar(value="X = -    Y = -")
        ttk.Label(top_bar, textvariable=self.mouse_coord_var,
          foreground=ACCENT, font=('Consolas', 10, 'bold'),
          anchor=tk.E).pack(side=tk.RIGHT, padx=10)
        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        # 延迟创建工具栏（需要canvas先创建）
        if self.mpl_toolbar is None:
            self.mpl_toolbar = NavigationToolbar2Tk(self.canvas, tb_frame)
        self.mpl_toolbar.update()
        # 双重绑定：mpl事件 + Tk原生事件，确保可靠触发
        self.canvas.mpl_connect('motion_notify_event', self._on_mouse_move)
        self.canvas.get_tk_widget().bind('<Motion>', self._on_mouse_move_tk)
        vpaned.add(plot_frame, weight=3)

        # 下部：公式转换 + Notebook
        bottom = ttk.Frame(vpaned)

        formula_frame = ttk.LabelFrame(bottom, text="Mass ↔ 电流 转换", padding=6)
        formula_frame.pack(fill=tk.X, padx=2, pady=(0, 2))

        ttk.Label(formula_frame, text="公式: Mass =").pack(side=tk.LEFT, padx=(4, 2))
        self.formula_var = tk.StringVar(value="-1.08608+0.02743*I+0.01722*I*I-1.9947*I*I*I*0.0000001")
        formula_entry = ttk.Entry(formula_frame, textvariable=self.formula_var, width=28)
        formula_entry.pack(side=tk.LEFT, padx=2)
        formula_entry.bind('<Return>', lambda e: self.apply_formula())
        ttk.Label(formula_frame, text="(I为电流A)", style='Muted.TLabel').pack(side=tk.LEFT, padx=2)
        ttk.Button(formula_frame, text="应用", command=self.apply_formula, width=6).pack(side=tk.LEFT, padx=4)

        self.xaxis_mode = tk.StringVar(value='current')
        ttk.Radiobutton(formula_frame, text="X轴: 电流(A)", variable=self.xaxis_mode,
                        value='current', command=self.refresh_plot).pack(side=tk.LEFT, padx=8)
        ttk.Radiobutton(formula_frame, text="X轴: Mass(m/z)", variable=self.xaxis_mode,
                        value='mass', command=self.refresh_plot).pack(side=tk.LEFT, padx=8)

        self.nb = ttk.Notebook(bottom)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=2, pady=(2, 0))

        meta_frame = ttk.Frame(self.nb, padding=8)
        self.nb.add(meta_frame, text='元数据')
        self._build_meta_panel(meta_frame)

        peak_frame = ttk.Frame(self.nb, padding=4)
        self.nb.add(peak_frame, text='峰列表')
        self._build_peak_panel(peak_frame)

        formula_nb_frame = ttk.Frame(self.nb, padding=8)
        self.nb.add(formula_nb_frame, text='分子式匹配')
        self._build_formula_panel(formula_nb_frame)

        vpaned.add(bottom, weight=1)

        self._init_axes()

    def _build_meta_panel(self, parent):
        """元数据面板（可滚动，五栏布局：前4栏字段 + 第5栏宽栏=前3栏宽度）"""
        canvas = tk.Canvas(parent, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient='vertical', command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.create_window((0, 0), window=inner, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        def _on_mw(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')
        canvas.bind_all('<MouseWheel>', _on_mw)
        p = inner

        self.meta_vars = {}

        # 7列grid：col0-3为四栏字段，col4-6为第5栏（跨3列，宽度=前3栏）
        for i in range(7):
            p.columnconfigure(i, weight=1)

        cols = []
        for i in range(4):
            col = ttk.Frame(p)
            col.grid(row=0, column=i, sticky='nsew', padx=(0 if i == 0 else 4, 4))
            cols.append(col)

        # 第5栏：跨col4-6，宽度=前3栏
        col5 = ttk.Frame(p)
        col5.grid(row=0, column=4, columnspan=3, sticky='nsew', padx=(4, 0))

        def _add_field(col, label, key, width=10):
            row = ttk.Frame(col)
            row.pack(fill=tk.X, pady=2)
            ttk.Label(row, text=label + ":", width=9).pack(side=tk.LEFT)
            var = tk.StringVar()
            entry = ttk.Entry(row, textvariable=var, width=width)
            entry.pack(side=tk.LEFT, padx=2)
            entry.bind('<Return>', lambda e: self.save_metadata())
            self.meta_vars[key] = var

        # 栏1：基本信息
        ttk.Label(cols[0], text="基本信息", style='Title.TLabel').pack(anchor='w', pady=(0, 4))
        _add_field(cols[0], '谱图名称', 'name', 12)
        _add_field(cols[0], '样品名称', 'sample_name', 12)
        _add_field(cols[0], '实验日期', 'date', 10)
        _add_field(cols[0], '操作人员', 'operator', 10)

        # 栏2：样品参数
        ttk.Label(cols[1], text="样品参数", style='Title.TLabel').pack(anchor='w', pady=(0, 4))
        _add_field(cols[1], '元素集', 'element_set', 12)
        _add_field(cols[1], '温度', 'temperature', 10)
        _add_field(cols[1], '加速电压', 'acceleration_voltage', 10)

        # 栏3：气源与功率
        ttk.Label(cols[2], text="气源与功率", style='Title.TLabel').pack(anchor='w', pady=(0, 4))
        _add_field(cols[2], 'Ar流量', 'ar_flow', 8)
        _add_field(cols[2], 'He流量', 'he_flow', 8)
        _add_field(cols[2], '功率', 'power', 8)

        # 栏4：腔体条件
        ttk.Label(cols[3], text="腔体条件", style='Title.TLabel').pack(anchor='w', pady=(0, 4))
        _add_field(cols[3], '冷凝距离', 'condensation_distance', 8)
        _add_field(cols[3], '气压', 'pressure', 8)
        self.liquid_n2_var = tk.BooleanVar()
        ttk.Checkbutton(cols[3], text="加液氮", variable=self.liquid_n2_var).pack(anchor='w', pady=2)

        # 栏5：其他条件、标签、备注（宽栏）
        ttk.Label(col5, text="其他信息", style='Title.TLabel').pack(anchor='w', pady=(0, 4))

        ttk.Label(col5, text="其他条件:").pack(anchor='w')
        self.conditions_text = tk.Text(col5, height=2, font=('Segoe UI', 9),
                                       bg=BG, relief='solid', borderwidth=1)
        self.conditions_text.pack(fill=tk.X, pady=(0, 4))

        tag_row = ttk.Frame(col5)
        tag_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(tag_row, text="标签:").pack(side=tk.LEFT)
        self.tags_var = tk.StringVar()
        tags_entry = ttk.Entry(tag_row, textvariable=self.tags_var, width=25)
        tags_entry.pack(side=tk.LEFT, padx=4)
        tags_entry.bind('<Return>', lambda e: self.save_metadata())
        ttk.Label(tag_row, text="(逗号分隔)", style='Muted.TLabel').pack(side=tk.LEFT)

        ttk.Label(col5, text="备注:").pack(anchor='w')
        self.notes_text = tk.Text(col5, height=2, font=('Segoe UI', 9),
                                  bg=BG, relief='solid', borderwidth=1)
        self.notes_text.pack(fill=tk.X, pady=(0, 4))

        # 底部：保存按钮（跨所有列）
        bottom = ttk.Frame(p)
        bottom.grid(row=1, column=0, columnspan=7, sticky='ew', pady=(8, 0))
        ttk.Button(bottom, text="保存元数据", command=self.save_metadata,
           style='Accent.TButton').pack(anchor='w', padx=4)

    def _build_peak_panel(self, parent):
        self.peak_tree = ttk.Treeview(parent, columns=('x', 'height', 'fwhm', 'area'),
                                      show='headings', height=5)
        for col, text, w in [('x', '峰位', 100), ('height', '峰高', 100),
                             ('fwhm', '半高宽', 100), ('area', '面积', 120)]:
            self.peak_tree.heading(col, text=text)
            self.peak_tree.column(col, width=w, anchor='center')
        self.peak_tree.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

    def _build_formula_panel(self, parent):
        """分子式匹配面板：左右分栏，左半参数设置，右半匹配结果"""
        # 左右分栏
        left = ttk.Frame(parent)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))
        right = ttk.Frame(parent)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0))

        # 左半：可滚动参数区
        canvas = tk.Canvas(left, highlightthickness=0)
        scrollbar = ttk.Scrollbar(left, orient='vertical', command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.create_window((0, 0), window=inner, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        def _on_mw(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')
        canvas.bind_all('<MouseWheel>', _on_mw)
        p = inner

        # 元素输入
        row1 = ttk.Frame(p)
        row1.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(row1, text='元素:', width=8).pack(side=tk.LEFT)
        self.formula_elements_var = tk.StringVar(value='')
        elem_entry = ttk.Entry(row1, textvariable=self.formula_elements_var, width=18)
        elem_entry.pack(side=tk.LEFT, padx=2)
        elem_entry.bind('<Return>', lambda e: self._confirm_elements())
        ttk.Button(row1, text='确认', command=self._confirm_elements, width=5).pack(side=tk.LEFT, padx=2)
        ttk.Button(row1, text='从样品名解析', command=self._auto_parse_elements, width=12).pack(side=tk.LEFT, padx=2)
        ttk.Label(p, text='多个元素用逗号分隔，如 Ag,Ar,Cu', foreground=MUTED, font=('', 8)).pack(anchor=tk.W, pady=(0, 4))

        # 元素个数限制（动态区域）
        ttk.Label(p, text='元素个数上限:', font=('', 9, 'bold')).pack(anchor=tk.W, pady=(4, 2))
        self.formula_counts_frame = ttk.Frame(p)
        self.formula_counts_frame.pack(fill=tk.X, pady=(0, 4))
        self.formula_count_vars = {}

        # 参数设置
        param_frame = ttk.Frame(p)
        param_frame.pack(fill=tk.X, pady=(4, 4))
        ttk.Label(param_frame, text='容差(Da):').pack(side=tk.LEFT)
        self.formula_tol_var = tk.StringVar(value='0.5')
        tol_entry = ttk.Entry(param_frame, textvariable=self.formula_tol_var, width=6)
        tol_entry.pack(side=tk.LEFT, padx=2)
        tol_entry.bind('<Return>', lambda e: self._match_and_label_formulas())
        ttk.Label(param_frame, text='电荷:').pack(side=tk.LEFT, padx=(8, 0))
        self.formula_charge_var = tk.StringVar(value='1')
        charge_entry = ttk.Entry(param_frame, textvariable=self.formula_charge_var, width=4)
        charge_entry.pack(side=tk.LEFT, padx=2)
        charge_entry.bind('<Return>', lambda e: self._match_and_label_formulas())
        ttk.Label(param_frame, text='最小丰度:').pack(side=tk.LEFT, padx=(8, 0))
        self.formula_minabund_var = tk.StringVar(value='0.5')
        abund_entry = ttk.Entry(param_frame, textvariable=self.formula_minabund_var, width=5)
        abund_entry.pack(side=tk.LEFT, padx=2)
        abund_entry.bind('<Return>', lambda e: self._match_and_label_formulas())
        ttk.Label(param_frame, text='%', font=('', 8)).pack(side=tk.LEFT)

        # 匹配按钮
        btn_frame = ttk.Frame(p)
        btn_frame.pack(fill=tk.X, pady=(4, 4))
        ttk.Button(btn_frame, text='匹配并标注同位素分子式', command=self._match_and_label_formulas,
           style='Accent.TButton').pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text='清除标注', command=self._clear_formula_labels).pack(side=tk.LEFT, padx=2)

        # 右半：匹配结果
        ttk.Label(right, text='匹配结果:', font=('', 9, 'bold')).pack(anchor=tk.W, pady=(0, 2))
        result_frame = ttk.Frame(right)
        result_frame.pack(fill=tk.BOTH, expand=True)
        self.formula_result_tree = ttk.Treeview(result_frame, columns=('peak', 'formula', 'calc_mass', 'error', 'abund'),
                                                 show='headings')
        for col, text, w in [('peak', '峰位', 65), ('formula', '同位素分子式', 115),
                             ('calc_mass', '计算质量', 75), ('error', '误差', 55), ('abund', '丰度', 50)]:
            self.formula_result_tree.heading(col, text=text)
            self.formula_result_tree.column(col, width=w, anchor='center')
        result_sb = ttk.Scrollbar(result_frame, orient='vertical', command=self.formula_result_tree.yview)
        self.formula_result_tree.configure(yscrollcommand=result_sb.set)
        self.formula_result_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        result_sb.pack(side=tk.RIGHT, fill=tk.Y)

        self._current_formula_matches = {}

    def _confirm_elements(self):
        """确认元素输入，刷新下方元素个数上限输入框"""
        elem_str = self.formula_elements_var.get().strip()
        if not elem_str:
            self._rebuild_count_inputs([])
            return
        elements = [e.strip() for e in elem_str.replace('，', ',').split(',') if e.strip()]
        valid = [e for e in elements if e in ELEMENT_ISOTOPES]
        invalid = [e for e in elements if e not in ELEMENT_ISOTOPES]
        if invalid:
            messagebox.showwarning('提示', f'以下元素未识别：{", ".join(invalid)}\n已忽略，有效元素：{", ".join(valid)}')
        self._rebuild_count_inputs(valid)

    def _auto_parse_elements(self):
        """从当前选中谱图的样品名解析元素"""
        specs = self._get_selected_spectra()
        if not specs:
            specs = [s for s in self.spectra if s.visible]
        if not specs:
            return
        name = specs[0].sample_name or specs[0].name
        elements = parse_elements_from_name(name)
        if elements:
            # 去重保序
            seen = set()
            unique = []
            for e in elements:
                if e not in seen:
                    seen.add(e)
                    unique.append(e)
            self.formula_elements_var.set(','.join(unique))
            self._rebuild_count_inputs(unique)
        else:
            self.formula_elements_var.set('')
            self._rebuild_count_inputs([])

    def _rebuild_count_inputs(self, elements):
        """根据元素列表重建个数输入框"""
        for w in self.formula_counts_frame.winfo_children():
            w.destroy()
        self.formula_count_vars = {}
        if not elements:
            ttk.Label(self.formula_counts_frame, text='（无元素，请输入或从样品名解析）',
                      foreground=MUTED).pack(anchor=tk.W)
            return
        for i, elem in enumerate(elements):
            frame = ttk.Frame(self.formula_counts_frame)
            frame.grid(row=i // 3, column=i % 3, sticky=tk.W, padx=4, pady=2)
            ttk.Label(frame, text=f'{elem}:', width=4).pack(side=tk.LEFT)
            var = tk.StringVar(value='8')
            self.formula_count_vars[elem] = var
            sb = ttk.Spinbox(frame, from_=0, to=999, textvariable=var, width=5)
            sb.pack(side=tk.LEFT)
            sb.bind('<Return>', lambda e: self._match_and_label_formulas())

    def _match_and_label_formulas(self):
        """执行分子式匹配并在图上标注（自动寻峰+保存到当前谱图）"""
        specs = self._get_selected_spectra()
        if not specs:
            specs = [s for s in self.spectra if s.visible]
        if not specs:
            messagebox.showinfo('提示', '请先选择或导入谱图')
            return
        spec = specs[0]

        # 自动寻峰（如果没有峰）
        if spec.peaks is None or len(spec.peaks) == 0:
            spec.peaks = detect_peaks(spec.x, spec.y)
            self.db.update_spectrum(spec)
            self._load_peaks(spec)
            if len(spec.peaks) == 0:
                messagebox.showinfo('提示', '自动寻峰未找到峰，请检查数据或调整基线')
                return

        # 解析元素
        elem_str = self.formula_elements_var.get().strip()
        if not elem_str:
            messagebox.showinfo('提示', '请输入元素或从样品名解析')
            return
        elements = [e.strip() for e in elem_str.replace('，', ',').split(',') if e.strip()]
        elements = [e for e in elements if e in ELEMENT_ISOTOPES]
        if not elements:
            messagebox.showerror('错误', '未识别到有效元素')
            return

        # 确保个数输入框存在
        if not self.formula_count_vars:
            self._rebuild_count_inputs(elements)

        max_counts = {}
        for e in elements:
            try:
                max_counts[e] = int(self.formula_count_vars.get(e, tk.StringVar(value='10')).get())
            except (ValueError, tk.TclError):
                max_counts[e] = 10

        try:
            tol = float(self.formula_tol_var.get())
        except ValueError:
            tol = 0.5
        try:
            charge = int(self.formula_charge_var.get())
        except ValueError:
            charge = 1
        try:
            min_abund = float(self.formula_minabund_var.get()) / 100.0
        except (ValueError, AttributeError):
            min_abund = 0.005

        # 执行匹配（峰位先转质量，使用同位素完整组合）
        peak_x = spec.peaks['x'].values
        peak_h = spec.peaks['height'].values
        peak_mass = self._current_to_mass(peak_x, spec)
        match_df = pd.DataFrame({'x': peak_mass, 'height': peak_h})
        raw_matches = match_peaks_to_isotope_formulas(
            match_df, elements, max_counts, tol, charge, min_abundance=min_abund)

        # 保存到当前谱图（独立存储）
        spec.formula_matches = {}
        spec.formula_peak_map = {}
        for i, pm in enumerate(peak_mass):
            if pm in raw_matches:
                formula, calc_mass, err, abund = raw_matches[pm]
                spec.formula_matches[pm] = (formula, calc_mass, err, peak_h[i], abund)
                spec.formula_peak_map[pm] = peak_x[i]

        # 同步到全局变量用于显示
        self._current_formula_matches = spec.formula_matches
        self._formula_peak_map = spec.formula_peak_map
        self._refresh_formula_result_list()

        # 重绘并标注
        self.refresh_plot()
        self._update_status(f'同位素分子式匹配完成：{len(spec.formula_matches)} 个峰匹配成功（已保存到当前谱图）')

    def _refresh_formula_result_list(self):
        """刷新分子式匹配结果列表"""
        for item in self.formula_result_tree.get_children():
            self.formula_result_tree.delete(item)
        for px, (formula, calc_mass, err, height, abund) in sorted(self._current_formula_matches.items()):
            self.formula_result_tree.insert('', 'end', values=(
                f'{px:.2f}', sort_formula_by_mass(formula), f'{calc_mass:.3f}', f'{err:.3f}', f'{abund*100:.1f}%'))

    def _clear_formula_labels(self):
        """清除当前谱图的分子式标注"""
        specs = self._get_selected_spectra()
        if specs:
            specs[0].formula_matches = {}
            specs[0].formula_peak_map = {}
        self._current_formula_matches = {}
        self._formula_peak_map = {}
        self._refresh_formula_result_list()
        self.refresh_plot()

    def _init_axes(self):
        self.fig.clear()
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel('X', fontsize=11, fontweight='bold')
        self.ax.set_ylabel('Intensity', fontsize=11, fontweight='bold')
        self.ax.set_title('请导入质谱数据', fontsize=13, fontweight='bold', color=MUTED)
        self.ax.grid(True, alpha=0.3, linestyle='--')
        self.ax.tick_params(direction='in', top=True, right=True)
        self.canvas.draw()

    # ----------------------------------------------------------
    # 数据库加载/保存
    # ----------------------------------------------------------
    def _load_from_db(self):
        self.spectra = self.db.get_all_spectra()
        self._refresh_groups()
        self._refresh_tree()
        self._update_status(f"数据库加载完成：共 {len(self.spectra)} 个谱图")

    # ----------------------------------------------------------
    # 文件导入
    # ----------------------------------------------------------
    def import_files(self):
        files = filedialog.askopenfilenames(
            title="选择质谱数据文件",
            filetypes=[("数据文件", "*.csv *.txt *.dat"), ("CSV", "*.csv"),
                       ("TXT", "*.txt"), ("所有文件", "*.*")])
        if files:
            self._load_files(files)

    def import_folder(self):
        folder = filedialog.askdirectory(title="选择包含质谱数据的文件夹（含子文件夹递归扫描）")
        if folder:
            files = []
            for root, dirs, filenames in os.walk(folder):
                for f in sorted(filenames):
                    if f.lower().endswith(('.csv', '.txt', '.dat')):
                        files.append(os.path.join(root, f))
            if files:
                self._update_status(f"扫描到 {len(files)} 个数据文件，正在导入...")
                self.root.update()
                self._load_files(files)
            else:
                messagebox.showinfo("提示", "该文件夹（含子文件夹）下未找到数据文件。")

    def _load_files(self, filepaths):
        loaded, errors = 0, []
        new_specs = []
        # 多线程并行读取文件（IO密集型）
        def _read_one(fp):
            data, err = load_spectrum_file(fp)
            return fp, data, err
        results = []
        with ThreadPoolExecutor(max_workers=min(8, len(filepaths))) as ex:
            for fp, data, err in ex.map(_read_one, filepaths):
                results.append((fp, data, err))
        for fp, data, err in results:
            if data is None:
                errors.append(f"{os.path.basename(fp)}: {err}")
                continue
            x, y = data
            from types import SimpleNamespace
            spec = SimpleNamespace(
                db_id=None, name=os.path.splitext(os.path.basename(fp))[0],
                filepath=fp, sample_name='', date=parse_date_from_path(fp),
                operator='', conditions='', ar_flow='', he_flow='', liquid_n2=False,
                power='', condensation_distance='', pressure='',
                mass_formula='-1.08608+0.02743*I+0.01722*I*I-1.9947*I*I*I*0.0000001',
                element_set='', temperature='', acceleration_voltage='40 kV',
                hp_annotations='',
                formula_matches={}, formula_peak_map={},
                tags=[], notes='',
                color=DEFAULT_COLORS[self.color_idx % len(DEFAULT_COLORS)],
                x=x, y_raw=y, y=y.copy(), baseline=None, peaks=None,
                visible=True, baseline_corrected=False, normalized=False,
                x_min=x.min(), x_max=x.max(), y_min=y.min(), y_max=y.max(),
                n_points=len(x))
            auto_params = parse_experiment_params(fp)
            for k, v in auto_params.items():
                setattr(spec, k, v)
            # 扫谱文件注释行中的参数
            scan_meta = parse_scan_metadata(fp)
            for k, v in scan_meta.items():
                if v:
                    setattr(spec, k, v)
            self.color_idx += 1
            # 批量插入：先不 commit
            spec.db_id = self.db.add_spectrum(spec, commit=False)
            new_specs.append(spec)
            loaded += 1

        # 统一提交事务
        if loaded > 0:
            self.db.conn.commit()
            for spec in reversed(new_specs):
                self.spectra.insert(0, spec)

        self._refresh_tree()
        self._refresh_tags()
        if loaded > 0:
            children = self.tree.get_children()
            if children:
                self.tree.selection_set(children[0])
                self.plot_single()
        self._update_status(f"导入完成：成功 {loaded} 个，失败 {len(errors)} 个")
        if errors:
            messagebox.showwarning("部分文件导入失败", "\n".join(errors[:10]))

    # ----------------------------------------------------------
    # 列表/分组操作
    # ----------------------------------------------------------
    def _refresh_tree(self):
        # 记录当前选中的谱图ID，刷新后恢复选中
        selected_ids = set()
        for iid in self.tree.selection():
            tags = self.tree.item(iid, 'tags')
            for t in tags:
                if t.isdigit():
                    selected_ids.add(int(t))
        self.tree.delete(*self.tree.get_children())
        for s in self.spectra:
            tag = 'visible' if s.visible else 'hidden'
            iid = self.tree.insert('', 'end', text=s.name, values=(
                getattr(s, 'element_set', '') or '-',
                getattr(s, 'power', '') or '-',
                getattr(s, 'ar_flow', '') or '-',
                getattr(s, 'he_flow', '') or '-',
                getattr(s, 'pressure', '') or '-',
                getattr(s, 'condensation_distance', '') or '-',
                getattr(s, 'temperature', '') or '-',
                getattr(s, 'acceleration_voltage', '40 kV') or '-',
            ), tags=(tag, str(s.db_id)))
            self.tree.tag_configure(str(s.db_id), foreground=s.color)
            self.tree.tag_configure('hidden', foreground='#CCC')
            if s.db_id in selected_ids:
                self.tree.selection_add(iid)

    def _refresh_groups(self):
        self.group_listbox.delete(0, tk.END)
        self.group_listbox.insert(tk.END, "全部谱图")
        self.groups = self.db.get_groups()
        for g in self.groups:
            self.group_listbox.insert(tk.END, g['name'])
        self.group_listbox.selection_set(0)

    def _refresh_tags(self):
        all_tags = set()
        for s in self.spectra:
            all_tags.update(s.tags)
        self.tag_combo['values'] = [''] + sorted(all_tags)

    def _get_spec_by_id(self, sid):
        return next((s for s in self.spectra if s.db_id == sid), None)

    def _get_selected_spectra(self):
        sel = self.tree.selection()
        ids = set()
        for item in sel:
            for t in self.tree.item(item, 'tags'):
                try:
                    ids.add(int(t))
                except ValueError:
                    pass
        return [s for s in self.spectra if s.db_id in ids]

    def _on_select(self, event=None):
        specs = self._get_selected_spectra()
        if len(specs) == 1:
            self._load_meta(specs[0])
            self._load_peaks(specs[0])
            # 加载该谱图独立的分子式匹配结果
            self._current_formula_matches = getattr(specs[0], 'formula_matches', {}) or {}
            self._formula_peak_map = getattr(specs[0], 'formula_peak_map', {}) or {}
            self._refresh_formula_result_list()
            self.plot_single()
        elif len(specs) > 1:
            self.plot_overlay()

    def _on_double_click(self, event=None):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        for t in self.tree.item(item, 'tags'):
            try:
                sid = int(t)
                spec = self._get_spec_by_id(sid)
                if spec:
                    spec.visible = not spec.visible
                    self._refresh_tree()
                    self.refresh_plot()
                break
            except ValueError:
                continue

    def _on_group_select(self, event=None):
        idx = self.group_listbox.curselection()
        if not idx:
            return
        if idx[0] == 0:
            self.current_group_id = None
            self.spectra = self.db.get_all_spectra()
        else:
            g = self.groups[idx[0] - 1]
            self.current_group_id = g['id']
            self.spectra = self.db.search_spectra(group_id=g['id'])
        self._refresh_tree()
        self._update_status(f"当前分组：{self.group_listbox.get(idx[0])}（{len(self.spectra)} 个）")

    def _do_search(self):
        kw = self.search_var.get().strip()
        tag = self.tag_var.get().strip()
        self.spectra = self.db.search_spectra(keyword=kw, tag=tag, group_id=self.current_group_id)
        self._refresh_tree()
        self._update_status(f"搜索完成：{len(self.spectra)} 个结果")

    def add_group(self):
        name = simpledialog.askstring("新建分组", "输入分组名称：")
        if name:
            color = colorchooser.askcolor(title="分组颜色")[1] or '#1F4E79'
            gid = self.db.add_group(name, color)
            if gid:
                self._refresh_groups()
                self._update_status(f"已创建分组：{name}")
            else:
                messagebox.showwarning("提示", "分组名称已存在。")

    def delete_group(self):
        idx = self.group_listbox.curselection()
        if not idx or idx[0] == 0:
            return
        g = self.groups[idx[0] - 1]
        if messagebox.askyesno("确认", f"删除分组「{g['name']}」？（不删除谱图本身）"):
            self.db.delete_group(g['id'])
            self.current_group_id = None
            self._refresh_groups()
            self.spectra = self.db.get_all_spectra()
            self._refresh_tree()

    def assign_group(self):
        specs = self._get_selected_spectra()
        if not specs:
            return
        if not self.groups:
            messagebox.showinfo("提示", "请先创建分组。")
            return
        names = [g['name'] for g in self.groups]
        choice = simpledialog.askstring("加入分组",
                                        f"选择分组（输入序号 1-{len(names)}）:\n" +
                                        "\n".join(f"{i+1}. {n}" for i, n in enumerate(names)))
        if choice and choice.isdigit() and 1 <= int(choice) <= len(names):
            g = self.groups[int(choice) - 1]
            for s in specs:
                self.db.assign_to_group(s.db_id, g['id'])
            self._update_status(f"已将 {len(specs)} 个谱图加入分组「{g['name']}」")

    def remove_selected(self):
        specs = self._get_selected_spectra()
        if not specs:
            return
        if not messagebox.askyesno("确认", f"删除选中的 {len(specs)} 个谱图？（从数据库永久删除）"):
            return
        for s in specs:
            self.db.delete_spectrum(s.db_id)
        self._load_from_db()
        self._init_axes()

    def change_color(self):
        specs = self._get_selected_spectra()
        if not specs:
            return
        color = colorchooser.askcolor(color=specs[0].color)[1]
        if color:
            for s in specs:
                s.color = color
                self.db.update_spectrum(s)
            self._refresh_tree()
            self.refresh_plot()

    def rename_spectrum(self):
        specs = self._get_selected_spectra()
        if len(specs) != 1:
            messagebox.showinfo("提示", "请选择一个谱图。")
            return
        new_name = simpledialog.askstring("重命名", "新名称：", initialvalue=specs[0].name)
        if new_name:
            specs[0].name = new_name
            self.db.update_spectrum(specs[0])
            self._refresh_tree()
            self.refresh_plot()

    def auto_parse_params(self):
        """从选中谱图的名称中自动解析实验参数并填入元数据"""
        specs = self._get_selected_spectra()
        if not specs:
            messagebox.showinfo("提示", "请先选择谱图。")
            return
        count = 0
        for s in specs:
            params = parse_experiment_params(s.name)
            for k, v in params.items():
                setattr(s, k, v)
            self.db.update_spectrum(s)
            count += 1
        # 如果只选中一个，刷新元数据面板
        if len(specs) == 1:
            self._load_meta(specs[0])
        self._update_status(f"自动读取参数完成：{count} 个谱图")
        messagebox.showinfo("完成", f"已从名称自动解析 {count} 个谱图的实验参数。\n"
                            "可在下方「元数据」选项卡查看和修改。")

    def apply_formula(self):
        """应用 Mass-电流转换公式并切换到 Mass X 轴（保存到当前选中谱图）"""
        formula = self.formula_var.get().strip()
        if not formula:
            messagebox.showwarning("提示", "请输入转换公式，例如：0.0012 * I**2")
            return
        # 验证公式
        try:
            test_I = np.array([100.0])
            eval(formula, {'__builtins__': {}}, {'I': test_I, 'np': np})
        except Exception as e:
            messagebox.showerror("公式错误", f"公式无法计算：\n{e}\n\n"
                                "示例：\n  0.0012 * I**2\n  I**2 / 850.0\n  0.001 * I**2 + 0.05")
            return
        # 保存到当前选中谱图（独立公式）
        specs = self._get_selected_spectra()
        if specs:
            specs[0].mass_formula = formula
            self.db.update_spectrum(specs[0])
        self.xaxis_mode.set('mass')
        self.refresh_plot()
        self._update_status(f"已应用公式到「{specs[0].name if specs else '当前谱图'}」: Mass = {formula}")

    def _current_to_mass(self, current_array, spec=None):
        """用公式将电流数组转换为 Mass。spec 不为空时使用该谱图独立公式"""
        if spec is not None:
            formula = getattr(spec, 'mass_formula', '') or self.formula_var.get().strip()
        else:
            formula = self.formula_var.get().strip()
        if not formula:
            return current_array
        try:
            I = np.asarray(current_array, dtype=float)
            result = eval(formula, {'__builtins__': {}}, {'I': I, 'np': np})
            return np.asarray(result, dtype=float)
        except Exception:
            return current_array

    # ----------------------------------------------------------
    # 元数据
    # ----------------------------------------------------------
    def _load_meta(self, spec):
        self.meta_vars['name'].set(spec.name)
        self.meta_vars['sample_name'].set(spec.sample_name)
        self.meta_vars['date'].set(spec.date)
        self.meta_vars['operator'].set(spec.operator)
        self.meta_vars['element_set'].set(getattr(spec, 'element_set', '') or '')
        self.meta_vars['ar_flow'].set(getattr(spec, 'ar_flow', ''))
        self.meta_vars['he_flow'].set(getattr(spec, 'he_flow', ''))
        self.meta_vars['power'].set(getattr(spec, 'power', ''))
        self.meta_vars['condensation_distance'].set(getattr(spec, 'condensation_distance', ''))
        self.meta_vars['pressure'].set(getattr(spec, 'pressure', ''))
        self.meta_vars['temperature'].set(getattr(spec, 'temperature', '') or '')
        self.meta_vars['acceleration_voltage'].set(getattr(spec, 'acceleration_voltage', '') or '40 kV')
        self.liquid_n2_var.set(getattr(spec, 'liquid_n2', False))
        self.conditions_text.delete('1.0', tk.END)
        self.conditions_text.insert('1.0', spec.conditions)
        self.tags_var.set(', '.join(spec.tags))
        self.notes_text.delete('1.0', tk.END)
        self.notes_text.insert('1.0', spec.notes)
        # 加载该谱图独立的公式
        spec_formula = getattr(spec, 'mass_formula', '') or ''
        if spec_formula:
            self.formula_var.set(spec_formula)
        else:
            self.formula_var.set("-1.08608+0.02743*I+0.01722*I*I-1.9947*I*I*I*0.0000001")

    def save_metadata(self):
        specs = self._get_selected_spectra()
        if len(specs) != 1:
            messagebox.showinfo("提示", "请选择一个谱图编辑元数据。")
            return
        s = specs[0]
        s.name = self.meta_vars['name'].get()
        s.sample_name = self.meta_vars['sample_name'].get()
        s.date = self.meta_vars['date'].get()
        s.operator = self.meta_vars['operator'].get()
        s.element_set = self.meta_vars['element_set'].get()
        s.ar_flow = self.meta_vars['ar_flow'].get()
        s.he_flow = self.meta_vars['he_flow'].get()
        s.power = self.meta_vars['power'].get()
        s.condensation_distance = self.meta_vars['condensation_distance'].get()
        s.pressure = self.meta_vars['pressure'].get()
        s.temperature = self.meta_vars['temperature'].get()
        s.acceleration_voltage = self.meta_vars['acceleration_voltage'].get()
        s.liquid_n2 = self.liquid_n2_var.get()
        s.conditions = self.conditions_text.get('1.0', tk.END).strip()
        s.tags = [t.strip() for t in self.tags_var.get().split(',') if t.strip()]
        s.notes = self.notes_text.get('1.0', tk.END).strip()
        self.db.update_spectrum(s)
        self._refresh_tree()
        self._refresh_tags()
        self._update_status(f"已保存元数据：{s.name}")

    def _load_peaks(self, spec):
        self.peak_tree.delete(*self.peak_tree.get_children())
        if spec.peaks is not None and len(spec.peaks) > 0:
            for _, row in spec.peaks.iterrows():
                self.peak_tree.insert('', 'end', values=(
                    f"{row['x']:.4f}", f"{row['height']:.4f}",
                    f"{row['fwhm']:.4f}", f"{row['area']:.4f}"))

    def edit_metadata(self):
        specs = self._get_selected_spectra()
        if len(specs) == 1:
            self.nb.select(0)

    # ----------------------------------------------------------
    # 批量处理
    # ----------------------------------------------------------
    def batch_baseline(self):
        specs = self._get_selected_spectra() or self.spectra
        if not specs:
            return
        for s in specs:
            s.baseline = compute_baseline(s.x, s.y_raw)
            s.y = s.y_raw - s.baseline
            s.baseline_corrected = True
            s.peaks = None
            self.db.update_spectrum(s)
        self.refresh_plot()
        self._update_status(f"批量基线扣除完成：{len(specs)} 个")

    def batch_peak_detect(self):
        specs = self._get_selected_spectra() or self.spectra
        if not specs:
            return
        self._update_status(f"批量峰识别中（{len(specs)}个谱图，多核并行）...")
        self.root.update_idletasks()
        def _detect_one(s):
            s.peaks = detect_peaks(s.x, s.y)
            return s, len(s.peaks)
        total = 0
        with ThreadPoolExecutor(max_workers=min(8, len(specs))) as ex:
            futures = {ex.submit(_detect_one, s): s for s in specs}
            for fut in as_completed(futures):
                s, n = fut.result()
                total += n
                self.db.update_spectrum(s)
        self.show_peaks.set(True)
        self.refresh_plot()
        specs_sel = self._get_selected_spectra()
        if len(specs_sel) == 1:
            self._load_peaks(specs_sel[0])
        self._update_status(f"批量峰识别完成：{len(specs)} 个谱图共 {total} 个峰")

    def batch_normalize(self):
        specs = self._get_selected_spectra() or self.spectra
        if not specs:
            return
        for s in specs:
            y_max = s.y.max()
            if y_max > 0:
                base = s.y_raw if not s.baseline_corrected else s.y_raw - (s.baseline or 0)
                s.y = base / y_max
                s.normalized = True
        self.refresh_plot()
        self._update_status(f"批量归一化完成：{len(specs)} 个")

    def batch_reset(self):
        specs = self._get_selected_spectra() or self.spectra
        if not specs:
            return
        for s in specs:
            s.y = s.y_raw.copy()
            s.baseline = None
            s.peaks = None
            s.normalized = False
            s.baseline_corrected = False
            self.db.update_spectrum(s)
        self.refresh_plot()
        self._update_status(f"批量还原完成：{len(specs)} 个")

    # ----------------------------------------------------------
    # 绘图
    # ----------------------------------------------------------
    def plot_single(self):
        specs = self._get_selected_spectra()
        if not specs and self.spectra:
            specs = [self.spectra[0]]
        if not specs:
            return
        self._draw([specs[0]], 'single')

    def plot_overlay(self):
        specs = self._get_selected_spectra()
        if len(specs) < 2:
            specs = [s for s in self.spectra if s.visible]
        if not specs:
            return
        self._draw(specs, 'overlay')

    def plot_grid(self):
        specs = self._get_selected_spectra()
        if len(specs) < 2:
            specs = [s for s in self.spectra if s.visible][:9]
        if not specs:
            return
        n = len(specs)
        cols = min(3, n)
        rows = (n + cols - 1) // cols
        use_mass = (self.xaxis_mode.get() == 'mass')
        self.fig.clear()
        for i, s in enumerate(specs):
            ax = self.fig.add_subplot(rows, cols, i + 1)
            x_data = self._current_to_mass(s.x, s) if use_mass else s.x
            x_plot, y_plot = downsample(x_data, s.y, 2000)
            ax.plot(x_plot, y_plot, color=s.color, linewidth=0.7)
            ax.set_title(s.name, fontsize=8, fontweight='bold', color=TEXT)
            ax.set_xlabel('Mass (m/z)' if use_mass else 'Magnetic field current/A', fontsize=7, color=TEXT)
            ax.tick_params(direction='in', labelsize=7)
            ax.grid(True, alpha=0.2)
            if self.y_log.get():
                ax.set_yscale('symlog', linthresh=max(0.001, abs(s.y).max() * 0.001))
        self.fig.tight_layout()
        self.canvas.draw_idle()
        self._update_status(f"网格子图：{n} 个谱图")

    def refresh_plot(self):
        specs = self._get_selected_spectra()
        if not specs:
            specs = [s for s in self.spectra if s.visible]
        if not specs:
            self._init_axes()
            return
        if len(specs) == 1:
            self._draw(specs, 'single')
        else:
            self._draw(specs, 'overlay')

    def _draw(self, specs, mode):
        # 清除所有子图（从网格模式切回时清除残留子图），重建单坐标系
        self.fig.clear()
        self.ax = self.fig.add_subplot(111)
        use_mass = (self.xaxis_mode.get() == 'mass')

        for s in specs:
            if not s.visible:
                continue
            label = s.name
            if s.normalized:
                label += " (norm)"
            if s.baseline_corrected:
                label += " (bcorr)"

            x_data = self._current_to_mass(s.x, s) if use_mass else s.x
            x_plot, y_plot = downsample(x_data, s.y, 4000)
            self.ax.plot(x_plot, y_plot, color=s.color, linewidth=0.8, label=label, alpha=0.9)

            if self.show_baseline.get() and s.baseline is not None:
                x_base = self._current_to_mass(s.x, s) if use_mass else s.x
                xb_plot, yb_plot = downsample(x_base, s.baseline, 4000)
                self.ax.plot(xb_plot, yb_plot, color=s.color, linewidth=0.5,
                             linestyle='--', alpha=0.4)

            if self.show_peaks.get() and s.peaks is not None and len(s.peaks) > 0:
                top = s.peaks.nlargest(min(15, len(s.peaks)), 'height')
                for _, row in top.iterrows():
                    px = self._current_to_mass(np.array([row['x']]), s)[0] if use_mass else row['x']
                    unit = 'm/z' if use_mass else 'A'
                    self.ax.annotate(f"{px:.2f}", xy=(px, row['height']),
                                    xytext=(0, 10), textcoords='offset points',
                                    fontsize=7, ha='center', color=s.color,
                                    arrowprops=dict(arrowstyle='->', color=s.color, lw=0.5))

            # 同位素分子式标注（从当前谱图读取独立匹配结果，自适应防重叠）
            spec_matches = getattr(s, 'formula_matches', {}) or {}
            if spec_matches and mode == 'single':
                ann_list = []
                for peak_mass, (formula, calc_mass, err, peak_h, abund) in spec_matches.items():
                    if use_mass:
                        fx = peak_mass
                    else:
                        fx = getattr(s, 'formula_peak_map', {}).get(peak_mass, peak_mass)
                    # 纯文本分子式转 mathtext 上下标（如 107Ag2 → $^{107}$Ag$_2$）
                    formula_mt = formula_to_mathtext(sort_formula_by_mass(formula))
                    ann_list.append((fx, peak_h, formula_mt))
                # 按峰高取前15个，再按x排序
                ann_list.sort(key=lambda t: t[1], reverse=True)
                ann_list = ann_list[:15]
                ann_list.sort(key=lambda t: t[0])

                if ann_list:
                    x_left, x_right = self.ax.get_xlim()
                    x_range = x_right - x_left
                    def _text_width_x(text):
                        return len(text) * x_range / 55.0

                    # 阶梯式防重叠偏移
                    levels = [18, 40, 62, 84, 106]
                    offsets = []
                    last_x = None
                    last_level = 0
                    for fx, fy, text in ann_list:
                        tw = _text_width_x(text)
                        if last_x is not None and (fx - last_x) < tw * 0.7:
                            last_level = (last_level + 1) % len(levels)
                        else:
                            last_level = 0
                        offsets.append(levels[last_level])
                        last_x = fx

                    for i, ((fx, fy, text), off) in enumerate(zip(ann_list, offsets)):
                        # 交替左右排布，避免拥挤
                        if i % 2 == 0:
                            ha, xoff = 'left', 5
                        else:
                            ha, xoff = 'right', -5
                        self.ax.annotate(text, xy=(fx, fy),
                                        xytext=(xoff, off), textcoords='offset points',
                                        fontsize=8.5, ha=ha, va='bottom', color='#C00000',
                                        fontweight='bold')

        if use_mass:
            xlabel = 'Mass (m/z)'
        else:
            xlabel = 'Magnetic field current/A'
        self.ax.set_xlabel(xlabel, fontsize=11, fontweight='bold', color=TEXT)
        ylabel = 'Normalized Intensity' if any(s.normalized for s in specs) else 'Intensity (nA)'
        self.ax.set_ylabel(ylabel, fontsize=11, fontweight='bold', color=TEXT)
        # 标题：单图显示元素集，叠加显示数量
        if mode == 'single':
            elem_set = getattr(specs[0], 'element_set', '') or specs[0].name
            self.ax.set_title(elem_set, fontsize=12, fontweight='bold', color=TEXT)
        else:
            self.ax.set_title(f'叠加对比 ({len(specs)} 个)', fontsize=12, fontweight='bold', color=TEXT)
        if self.y_log.get():
            self.ax.set_yscale('symlog', linthresh=max(0.001, abs(specs[0].y).max() * 0.001))
        self.ax.grid(True, alpha=0.3, linestyle='--')
        self.ax.tick_params(direction='in', top=True, right=True)
        self.ax.xaxis.set_minor_locator(AutoMinorLocator(5))
        if len(specs) > 1:
            self.ax.legend(fontsize=8, loc='best', framealpha=0.9)
        else:
            # 单图模式：左上角显示实验条件（竖向排列）
            s = specs[0]
            cond_lines = []
            if getattr(s, 'ar_flow', ''):
                cond_lines.append(f"Ar {s.ar_flow} sccm")
            if getattr(s, 'he_flow', ''):
                cond_lines.append(f"He {s.he_flow} sccm")
            if getattr(s, 'power', ''):
                cond_lines.append(f"{s.power} W")
            if getattr(s, 'pressure', ''):
                cond_lines.append(f"{s.pressure} Pa")
            if getattr(s, 'temperature', ''):
                cond_lines.append(f"{s.temperature} K")
            if getattr(s, 'condensation_distance', ''):
                cond_lines.append(f"{s.condensation_distance} cm")
            if cond_lines:
                cond_text = "\n".join(cond_lines)
                self.ax.text(0.015, 0.97, cond_text, transform=self.ax.transAxes,
                            fontsize=9, verticalalignment='top', horizontalalignment='left',
                            bbox=dict(boxstyle='round,pad=0.3', facecolor='white', alpha=0.8, edgecolor='#cccccc'),
                            color=TEXT, family='monospace')
        self.fig.tight_layout()
        self.canvas.draw_idle()

    # ----------------------------------------------------------
    # 导出
    # ----------------------------------------------------------
    def export_image(self):
        if not self.spectra:
            return
        path = filedialog.asksaveasfilename(
            title="导出图片", defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("SVG", "*.svg"), ("PDF", "*.pdf")])
        if path:
            self.fig.savefig(path, dpi=300, bbox_inches='tight', facecolor=BG)
            self._update_status(f"图片已导出: {path}")

    def export_data(self):
        specs = self._get_selected_spectra() or self.spectra
        if not specs:
            return
        path = filedialog.asksaveasfilename(
            title="导出数据", defaultextension=".xlsx",
            filetypes=[("Excel", "*.xlsx"), ("CSV", "*.csv")])
        if not path:
            return
        try:
            if path.endswith('.xlsx'):
                with pd.ExcelWriter(path, engine='openpyxl') as writer:
                    all_data = {}
                    for s in specs:
                        all_data[f'{s.name}_x'] = s.x
                        all_data[f'{s.name}_y'] = s.y
                    max_len = max(len(v) for v in all_data.values())
                    for k in all_data:
                        all_data[k] = np.pad(all_data[k], (0, max_len - len(all_data[k])),
                                             constant_values=np.nan)
                    pd.DataFrame(all_data).to_excel(writer, sheet_name='Data', index=False)
                    peak_rows = []
                    for s in specs:
                        if s.peaks is not None and len(s.peaks) > 0:
                            for _, row in s.peaks.iterrows():
                                peak_rows.append({'谱图': s.name, '峰位': row['x'],
                                                  '峰高': row['height'], '半高宽': row['fwhm'],
                                                  '面积': row['area']})
                    if peak_rows:
                        pd.DataFrame(peak_rows).to_excel(writer, sheet_name='Peaks', index=False)
            else:
                all_data = {}
                for s in specs:
                    all_data[f'{s.name}_x'] = s.x
                    all_data[f'{s.name}_y'] = s.y
                max_len = max(len(v) for v in all_data.values())
                for k in all_data:
                    all_data[k] = np.pad(all_data[k], (0, max_len - len(all_data[k])),
                                         constant_values=np.nan)
                pd.DataFrame(all_data).to_csv(path, index=False)
            self._update_status(f"数据已导出: {path}")
            messagebox.showinfo("成功", f"数据已保存到:\n{path}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def batch_export(self):
        if not self.spectra:
            return
        folder = filedialog.askdirectory(title="选择导出文件夹")
        if not folder:
            return
        count = 0
        for s in self.spectra:
            try:
                fig, ax = plt.subplots(figsize=(10, 5), facecolor=BG)
                ax.plot(s.x, s.y, color=s.color, linewidth=0.8)
                ax.set_title(s.name, fontweight='bold')
                ax.set_xlabel('Magnetic field current/A')
                ax.set_ylabel('Intensity')
                ax.grid(True, alpha=0.3)
                ax.tick_params(direction='in', top=True, right=True)
                fig.tight_layout()
                safe_name = "".join(c for c in s.name if c.isalnum() or c in '-_ ')
                fig.savefig(os.path.join(folder, f"{safe_name}.png"), dpi=200,
                            bbox_inches='tight', facecolor=BG)
                plt.close(fig)
                count += 1
            except Exception as e:
                print(f"导出 {s.name} 失败: {e}")
        self._update_status(f"批量导出完成：{count}/{len(self.spectra)} 张图片")
        messagebox.showinfo("完成", f"已导出 {count} 张图片到:\n{folder}")

    def export_library(self):
        """生成质谱库文件夹：含图片、原始数据、HTML索引、元数据汇总表"""
        if not self.spectra:
            messagebox.showinfo("提示", "请先导入质谱数据。")
            return
        folder = filedialog.askdirectory(title="选择质谱库保存位置")
        if not folder:
            return

        lib_name = f"质谱库_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        lib_path = os.path.join(folder, lib_name)
        spectra_dir = os.path.join(lib_path, 'spectra')
        data_dir = os.path.join(lib_path, 'data')
        os.makedirs(spectra_dir, exist_ok=True)
        os.makedirs(data_dir, exist_ok=True)

        count = 0
        metadata_rows = []
        html_cards = []

        for idx, s in enumerate(self.spectra):
            try:
                safe_name = "".join(c for c in s.name if c.isalnum() or c in '-_ ') or f'spectrum_{idx}'
                img_path = os.path.join(spectra_dir, f"{safe_name}.png")
                data_path = os.path.join(data_dir, f"{safe_name}.csv")

                # 生成谱图
                fig, ax = plt.subplots(figsize=(10, 5), facecolor=BG)
                ax.plot(s.x, s.y, color=s.color, linewidth=0.8)
                ax.set_title(s.name, fontweight='bold', fontsize=12)
                ax.set_xlabel('Magnetic field current/A')
                ax.set_ylabel('Intensity (nA)')
                ax.grid(True, alpha=0.3)
                ax.tick_params(direction='in', top=True, right=True)
                # 标注前5个峰
                if s.peaks is not None and len(s.peaks) > 0:
                    top = s.peaks.nlargest(min(5, len(s.peaks)), 'height')
                    for _, row in top.iterrows():
                        ax.annotate(f"{row['x']:.2f}", xy=(row['x'], row['height']),
                                   xytext=(0, 8), textcoords='offset points',
                                   fontsize=7, ha='center', color=s.color,
                                   arrowprops=dict(arrowstyle='->', color=s.color, lw=0.5))
                fig.tight_layout()
                fig.savefig(img_path, dpi=150, bbox_inches='tight', facecolor=BG)
                plt.close(fig)

                # 保存原始数据
                pd.DataFrame({'x': s.x, 'y': s.y}).to_csv(data_path, index=False)

                # 元数据
                liquid_n2_str = '是' if getattr(s, 'liquid_n2', False) else '否'
                meta = {
                    '序号': idx + 1,
                    '谱图名称': s.name,
                    '样品名称': s.sample_name,
                    '实验日期': s.date,
                    '操作人员': s.operator,
                    'Ar流量(sccm)': getattr(s, 'ar_flow', ''),
                    'He流量(sccm)': getattr(s, 'he_flow', ''),
                    '加液氮': liquid_n2_str,
                    '功率(W)': getattr(s, 'power', ''),
                    '冷凝距离(mm)': getattr(s, 'condensation_distance', ''),
                    '气压(Pa)': getattr(s, 'pressure', ''),
                    '其他条件': s.conditions,
                    '标签': ', '.join(s.tags),
                    '数据点数': s.n_points,
                    'X范围': f"{s.x_min:.2f}~{s.x_max:.2f}",
                    'Y最大值': f"{s.y_max:.4f}",
                    '峰数量': len(s.peaks) if s.peaks is not None else 0,
                    '备注': s.notes,
                }
                metadata_rows.append(meta)

                # HTML卡片
                peaks_html = ""
                if s.peaks is not None and len(s.peaks) > 0:
                    top3 = s.peaks.nlargest(min(3, len(s.peaks)), 'height')
                    peaks_html = "<br><b>主要峰:</b> " + ", ".join(
                        f"{r['x']:.2f}({r['height']:.3f})" for _, r in top3.iterrows())

                html_cards.append(f'''
                <div class="card">
                    <img src="spectra/{safe_name}.png" alt="{s.name}">
                    <div class="card-info">
                        <h3>{idx+1}. {s.name}</h3>
                        <p><b>样品:</b> {s.sample_name} | <b>实验日期:</b> {s.date} | <b>操作:</b> {s.operator}</p>
                        <p><b>Ar:</b> {getattr(s,'ar_flow','')} sccm | <b>He:</b> {getattr(s,'he_flow','')} sccm | <b>功率:</b> {getattr(s,'power','')} W</p>
                        <p><b>液氮:</b> {liquid_n2_str} | <b>冷凝距离:</b> {getattr(s,'condensation_distance','')} mm | <b>气压:</b> {getattr(s,'pressure','')} Pa</p>
                        <p><b>点数:</b> {s.n_points} | <b>峰数:</b> {len(s.peaks) if s.peaks is not None else 0}{peaks_html}</p>
                        <p class="notes"><b>备注:</b> {s.notes}</p>
                    </div>
                </div>''')

                count += 1
            except Exception as e:
                print(f"导出 {s.name} 失败: {e}")

        # 生成元数据汇总Excel
        try:
            pd.DataFrame(metadata_rows).to_excel(
                os.path.join(lib_path, '质谱库元数据汇总.xlsx'), index=False)
        except Exception:
            pd.DataFrame(metadata_rows).to_csv(
                os.path.join(lib_path, '质谱库元数据汇总.csv'), index=False, encoding='utf-8-sig')

        # 生成HTML索引
        html_content = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>质谱库 - {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}</title>
<style>
body {{ font-family: "Microsoft YaHei", Arial, sans-serif; margin: 0; padding: 20px; background: #f5f7fa; }}
h1 {{ color: #1565C0; border-bottom: 2px solid #1565C0; padding-bottom: 10px; }}
.stats {{ background: #fff; padding: 15px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
.card {{ background: #fff; border-radius: 8px; margin-bottom: 20px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
.card img {{ width: 100%; display: block; }}
.card-info {{ padding: 15px; }}
.card-info h3 {{ margin: 0 0 8px 0; color: #1565C0; }}
.card-info p {{ margin: 4px 0; font-size: 13px; color: #333; }}
.notes {{ color: #666; font-style: italic; }}
</style>
</head>
<body>
<h1>质谱图库</h1>
<div class="stats">
    <p><b>生成时间:</b> {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    <p><b>谱图总数:</b> {count} 张</p>
    <p><b>文件夹结构:</b> spectra/ (谱图PNG) | data/ (原始数据CSV) | 质谱库元数据汇总.xlsx</p>
</div>
{''.join(html_cards)}
</body>
</html>'''
        with open(os.path.join(lib_path, 'index.html'), 'w', encoding='utf-8') as f:
            f.write(html_content)

        # README
        readme = f"""质谱图库
==========
生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
谱图数量: {count} 张

文件夹结构:
  index.html              - 浏览器打开可浏览所有谱图和元数据
  质谱库元数据汇总.xlsx    - 所有谱图元数据汇总表
  spectra/                - 所有谱图PNG图片
  data/                   - 所有谱图原始数据CSV

使用方法:
  双击 index.html 在浏览器中查看质谱库。
"""
        with open(os.path.join(lib_path, 'README.txt'), 'w', encoding='utf-8') as f:
            f.write(readme)

        self._update_status(f"质谱库生成完成：{count} 个谱图")
        messagebox.showinfo("完成",
            f"质谱库已生成到:\n{lib_path}\n\n"
            f"包含 {count} 个谱图\n"
            f"双击 index.html 可在浏览器中浏览")

    # ----------------------------------------------------------
    def open_high_precision_annotator(self):
        """打开高精度质谱标注子窗口"""
        specs = self._get_selected_spectra()
        if not specs and self.spectra:
            specs = [self.spectra[0]]
        if not specs:
            messagebox.showinfo("提示", "请先导入并选择一个质谱图。")
            return
        HighPrecisionAnnotator(self.root, specs[0], self)

    def merge_database(self):
        """合并另一个数据库，遇到重复时弹出对比对话框"""
        path = filedialog.askopenfilename(
            title="选择要合并的数据库文件",
            filetypes=[("SQLite数据库", "*.sqlite *.db"), ("所有文件", "*.*")])
        if not path:
            return
        if os.path.abspath(path) == os.path.abspath(self.db.db_path):
            messagebox.showwarning("提示", "不能合并当前正在使用的数据库")
            return

        # 读取另一个数据库的所有谱图
        import sqlite3 as _sqlite3
        try:
            other_conn = _sqlite3.connect(path)
            other_conn.row_factory = _sqlite3.Row
            rows = other_conn.execute("SELECT * FROM spectra").fetchall()
            other_conn.close()
        except Exception as e:
            messagebox.showerror("错误", f"无法打开数据库: {e}")
            return

        added = replaced = skipped = 0
        for row in rows:
            new_data = dict(row)
            # 检查是否重复（名称+日期+路径）
            key = (new_data.get('name'), new_data.get('date') or '', new_data.get('filepath'))
            existing = self.db.find_spectrum_by_key(key[0], key[1], key[2])
            if existing:
                # 弹出对比对话框
                dlg = MergeConflictDialog(self.root, existing, new_data)
                if dlg.result == 'cancel':
                    break
                elif dlg.result == 'replace':
                    self.db.replace_spectrum(existing['id'], new_data)
                    replaced += 1
                elif dlg.result == 'keep_both':
                    # 新谱图重命名
                    new_data['name'] = new_data.get('name', '') + '_新'
                    self.db.insert_spectrum_data(new_data)
                    added += 1
                else:  # skip
                    skipped += 1
            else:
                self.db.insert_spectrum_data(new_data)
                added += 1

        self._load_from_db()
        messagebox.showinfo("合并完成",
                           f"新增 {added} 个，替换 {replaced} 个，跳过 {skipped} 个")

    def open_font_settings(self):
        FontSettingsDialog(self.root, self)

    def _apply_ui_font(self):
        """应用界面字体到所有控件"""
        ui_font = FONT_CONFIG['ui_font']
        ui_size = FONT_CONFIG['ui_size']
        try:
            default_font = tk.font.nametofont("TkDefaultFont")
            default_font.configure(family=ui_font, size=ui_size)
            text_font = tk.font.nametofont("TkTextFont")
            text_font.configure(family=ui_font, size=ui_size)
        except Exception:
            pass
        # 更新ttk样式
        try:
            style = ttk.Style()
            style.configure('TLabel', font=(ui_font, ui_size))
            style.configure('Panel.TLabel', font=(ui_font, ui_size))
            style.configure('Title.TLabel', font=(ui_font, ui_size+1, 'bold'))
            style.configure('Muted.TLabel', font=(ui_font, ui_size-1))
            style.configure('TButton', font=(ui_font, ui_size))
            style.configure('Accent.TButton', font=(ui_font, ui_size, 'bold'))
            style.configure('TCheckbutton', font=(ui_font, ui_size))
            style.configure('Treeview', font=(ui_font, ui_size))
            style.configure('Treeview.Heading', font=(ui_font, ui_size, 'bold'))
            style.configure('TNotebook.Tab', font=(ui_font, ui_size))
        except Exception:
            pass

    def open_scan_spectrum(self):
        ScanSpectrumDialog(self.root, self)

    def open_theoretical_ms(self):
        TheoreticalMSDialog(self.root, self)

    def open_formula_calculator(self):
        FormulaCalculator(self.root, self)

    def _on_mouse_move(self, event):
        """鼠标在图上移动时显示坐标（matplotlib事件）"""
        if event.inaxes and event.xdata is not None and event.ydata is not None:
            use_mass = (self.xaxis_mode.get() == 'mass')
            x_unit = 'm/z' if use_mass else 'A'
            self.mouse_coord_var.set(f"鼠标坐标: X = {event.xdata:.4f} {x_unit}    Y = {event.ydata:.4f}")
        else:
            self.mouse_coord_var.set("鼠标坐标: X = -    Y = -")

    def _on_mouse_move_tk(self, event):
        """鼠标在图上移动时显示坐标（Tk原生事件备份）"""
        try:
            w, h = self.canvas.get_width_height()
            # Tk坐标原点在左上角，matplotlib在左下角
            inv = self.ax.transData.inverted()
            xd, yd = inv.transform((event.x, h - event.y))
            use_mass = (self.xaxis_mode.get() == 'mass')
            x_unit = 'm/z' if use_mass else 'A'
            self.mouse_coord_var.set(f"鼠标坐标: X = {xd:.4f} {x_unit}    Y = {yd:.4f}")
        except Exception:
            pass

    def _update_status(self, msg):
        self.status_var.set(f"  {msg}    |    共 {len(self.spectra)} 个谱图")

    def on_closing(self):
        _save_isotope_cache()
        self.db.close()
        self.root.destroy()

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.root.mainloop()


# ============================================================





class TheoreticalMSDialog:
    """理论质谱计算：基于同位素丰度模拟团簇质谱图"""
    def __init__(self, parent, main_app):
        self.main_app = main_app
        self.win = tk.Toplevel(parent)
        self.win.title("理论质谱计算 - 同位素分布模拟")
        self.win.geometry("1000x700")
        self.win.minsize(800, 600)
        self.result_x = []
        self.result_y = []
        self._build_ui()

    def _build_ui(self):
        # 参数设置区
        param_frame = ttk.LabelFrame(self.win, text="计算参数", padding=10)
        param_frame.pack(fill=tk.X, padx=10, pady=8)

        # 第一行：元素、主元素、最大原子数
        row1 = ttk.Frame(param_frame)
        row1.pack(fill=tk.X, pady=3)
        ttk.Label(row1, text="元素集:", width=10).pack(side=tk.LEFT)
        self.elements_var = tk.StringVar(value="Ag,O,H")
        ttk.Entry(row1, textvariable=self.elements_var, width=20).pack(side=tk.LEFT, padx=2)

        ttk.Label(row1, text="主元素:", width=8).pack(side=tk.LEFT, padx=(15,0))
        self.main_elem_var = tk.StringVar(value="Ag")
        ttk.Entry(row1, textvariable=self.main_elem_var, width=8).pack(side=tk.LEFT, padx=2)

        ttk.Label(row1, text="最大原子数:", width=10).pack(side=tk.LEFT, padx=(15,0))
        self.max_n_var = tk.StringVar(value="10")
        ttk.Entry(row1, textvariable=self.max_n_var, width=8).pack(side=tk.LEFT, padx=2)

        # 第二行：分辨率、强度模式、电荷
        row2 = ttk.Frame(param_frame)
        row2.pack(fill=tk.X, pady=3)
        ttk.Label(row2, text="分辨率:", width=10).pack(side=tk.LEFT)
        self.resolution_var = tk.StringVar(value="1000")
        ttk.Combobox(row2, textvariable=self.resolution_var, width=10,
                     values=["500", "1000", "2000", "5000", "10000", "20000"]).pack(side=tk.LEFT, padx=2)

        ttk.Label(row2, text="强度模式:", width=10).pack(side=tk.LEFT, padx=(15,0))
        self.intensity_mode = tk.StringVar(value="uniform")
        ttk.Radiobutton(row2, text="统一最高峰", variable=self.intensity_mode, value="uniform").pack(side=tk.LEFT, padx=3)
        ttk.Radiobutton(row2, text="按n递减", variable=self.intensity_mode, value="decrease").pack(side=tk.LEFT, padx=3)

        ttk.Label(row2, text="电荷z:", width=8).pack(side=tk.LEFT, padx=(15,0))
        self.charge_var = tk.StringVar(value="1")
        ttk.Entry(row2, textvariable=self.charge_var, width=6).pack(side=tk.LEFT, padx=2)

        # 第三行：其他元素最大个数（简化）
        row3 = ttk.Frame(param_frame)
        row3.pack(fill=tk.X, pady=3)
        ttk.Label(row3, text="其他元素最大数:", width=14).pack(side=tk.LEFT)
        self.other_max_var = tk.StringVar(value="2")
        ttk.Entry(row3, textvariable=self.other_max_var, width=8).pack(side=tk.LEFT, padx=2)
        ttk.Label(row3, text="(每个非主元素的最大原子数)", foreground=MUTED).pack(side=tk.LEFT, padx=5)

        # 按钮
        btn_frame = ttk.Frame(param_frame)
        btn_frame.pack(fill=tk.X, pady=(8,0))
        ttk.Button(btn_frame, text="计算理论质谱", command=self._calculate).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="导出数据", command=self._export).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="导入到主界面", command=self._import_to_main).pack(side=tk.LEFT, padx=2)

        # 图表区
        chart_frame = ttk.Frame(self.win)
        chart_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.fig = Figure(figsize=(8, 4), dpi=100, facecolor=BG)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, chart_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        # 导航工具栏（放大/缩小/平移/保存）
        toolbar_frame = ttk.Frame(chart_frame)
        toolbar_frame.pack(fill=tk.X, side=tk.BOTTOM)
        self.toolbar = NavigationToolbar2Tk(self.canvas, toolbar_frame)
        self.toolbar.config(background=PANEL)
        self.toolbar.update()
        self._init_plot()

        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.win, textvariable=self.status_var, foreground=ACCENT).pack(pady=5)

    def _init_plot(self):
        self.ax.clear()
        self.ax.set_xlabel("Mass / z", color=TEXT, fontsize=11)
        self.ax.set_ylabel("Relative Intensity", color=TEXT, fontsize=11)
        self.ax.set_title("理论质谱图", color=ACCENT, fontsize=12)
        self.ax.tick_params(colors=TEXT)
        self.ax.grid(True, alpha=0.3)
        self.canvas.draw()

    def _get_isotopes(self, element):
        """获取元素的同位素列表 [(mass, abundance), ...]"""
        for sym, isotopes in ELEMENT_ISOTOPES.items():
            if sym.lower() == element.lower():
                return isotopes
        return None

    def _calculate(self):
        """计算理论质谱"""
        try:
            elements = [e.strip() for e in self.elements_var.get().split(',') if e.strip()]
            main_elem = self.main_elem_var.get().strip()
            max_n = int(self.max_n_var.get())
            other_max = int(self.other_max_var.get())
            resolution = float(self.resolution_var.get())
            charge = int(self.charge_var.get())
            intensity_mode = self.intensity_mode.get()
        except ValueError:
            messagebox.showerror("错误", "请输入有效的参数")
            return

        if main_elem not in elements:
            messagebox.showerror("错误", "主元素必须在元素集中")
            return

        # 检查元素是否有同位素数据
        for e in elements:
            if self._get_isotopes(e) is None:
                messagebox.showerror("错误", f"元素 {e} 没有同位素数据")
                return

        self.status_var.set("计算中...")
        self.win.update()

        from itertools import product as iter_product
        other_elements = [e for e in elements if e != main_elem]

        # 预计算每个元素的同位素数组（避免重复读取）
        isotope_cache = {}
        for e in elements:
            isotopes = self._get_isotopes(e)
            if isotopes:
                isotope_cache[e] = (np.array([m for m, _ in isotopes]),
                                   np.array([p for _, p in isotopes]))

        # 收集所有峰 (mass/z, intensity)
        all_peaks = {}  # mass -> total intensity

        for n in range(1, max_n + 1):
            # 生成其他元素的个数组合
            other_counts_list = [range(0, other_max + 1)] * len(other_elements)
            for other_counts in iter_product(*other_counts_list):
                # 构建团簇组成
                composition = {main_elem: n}
                for e, c in zip(other_elements, other_counts):
                    if c > 0:
                        composition[e] = c

                # 计算这个团簇的所有同位素组合
                cluster_peaks = self._calc_cluster_isotopes(composition)
                if not cluster_peaks:
                    continue

                # 团簇强度因子
                if intensity_mode == "uniform":
                    cluster_factor = 1.0
                else:  # decrease
                    cluster_factor = 1.0 / n

                # 找最高峰，归一化（用numpy）
                probs = np.array([p for _, p in cluster_peaks])
                masses = np.array([m for m, _ in cluster_peaks])
                max_prob = probs.max()
                if max_prob <= 0:
                    continue
                mz = masses / charge
                # 按分辨率合并
                bin_size = mz / resolution
                keys = np.round(mz / bin_size) * bin_size
                intensities = (probs / max_prob) * cluster_factor * 100
                for key, inten in zip(keys, intensities):
                    all_peaks[key] = all_peaks.get(key, 0) + inten

        if not all_peaks:
            messagebox.showinfo("提示", "没有计算出峰")
            self.status_var.set("无结果")
            return

        # 转换为峰列表
        peak_masses = np.array(sorted(all_peaks.keys()))
        peak_intensities = np.array([all_peaks[m] for m in peak_masses])

        # 全局归一化到100
        max_int = peak_intensities.max()
        if max_int > 0:
            peak_intensities = peak_intensities / max_int * 100

        # 高斯展宽：R = m / FWHM，FWHM = m / R
        # sigma = FWHM / (2*sqrt(2*ln2)) ≈ FWHM / 2.3548
        if len(peak_masses) > 0:
            m_min = peak_masses.min()
            m_max = peak_masses.max()
            # 计算最大sigma（在最大质量处）
            max_fwhm = m_max / resolution
            max_sigma = max_fwhm / 2.3548
            # 扩展质量范围
            x_min = max(0, m_min - 5 * max_sigma)
            x_max = m_max + 5 * max_sigma
            # 生成采样点（足够密，确保峰形平滑）
            n_points = min(20000, max(1000, int((x_max - x_min) / (max_fwhm / 20))))
            x_grid = np.linspace(x_min, x_max, n_points)
            y_grid = np.zeros_like(x_grid)

            # 叠加每个峰的高斯分布
            for pm, pi in zip(peak_masses, peak_intensities):
                fwhm = pm / resolution
                sigma = fwhm / 2.3548
                if sigma <= 0:
                    continue
                # 只在峰附近计算（±4sigma）
                mask = (x_grid >= pm - 4*sigma) & (x_grid <= pm + 4*sigma)
                if mask.sum() > 0:
                    y_grid[mask] += pi * np.exp(-0.5 * ((x_grid[mask] - pm) / sigma) ** 2)

            # 归一化
            if y_grid.max() > 0:
                y_grid = y_grid / y_grid.max() * 100

            self.result_x = x_grid
            self.result_y = y_grid
        else:
            self.result_x = peak_masses
            self.result_y = peak_intensities

        # 绘图
        self._plot_result()
        self.status_var.set(f"计算完成：{len(peak_masses)} 个同位素峰，R={resolution:.0f}，质量范围 {peak_masses[0]:.1f} - {peak_masses[-1]:.1f}")

    def _calc_cluster_isotopes(self, composition):
        """计算一个团簇组成的所有同位素峰（卷积加速版），返回 [(mass, probability), ...]"""
        # 用多项式卷积计算同位素分布
        # 每个元素的同位素分布 = 质量数组 + 概率数组
        # n个原子 = 分布的n次自卷积
        # 多元素 = 各元素分布的卷积

        result_masses = np.array([0.0])
        result_probs = np.array([1.0])

        for elem, count in composition.items():
            isotopes = self._get_isotopes(elem)
            if not isotopes or count <= 0:
                continue
            elem_masses = np.array([m for m, _ in isotopes], dtype=np.float64)
            elem_probs = np.array([p for _, p in isotopes], dtype=np.float64)

            # 计算count个该元素的分布（快速幂卷积）
            elem_n_masses = elem_masses.copy()
            elem_n_probs = elem_probs.copy()
            remaining = count - 1
            current_masses = elem_masses.copy()
            current_probs = elem_probs.copy()

            while remaining > 0:
                if remaining % 2 == 1:
                    elem_n_masses, elem_n_probs = self._convolve_distributions(
                        elem_n_masses, elem_n_probs, current_masses, current_probs)
                current_masses, current_probs = self._convolve_distributions(
                    current_masses, current_probs, current_masses, current_probs)
                remaining //= 2

            # 与结果卷积
            result_masses, result_probs = self._convolve_distributions(
                result_masses, result_probs, elem_n_masses, elem_n_probs)

        # 过滤极小概率
        mask = result_probs > 1e-15
        return list(zip(result_masses[mask], result_probs[mask]))

    def _convolve_distributions(self, m1, p1, m2, p2):
        """卷积两个质量-概率分布，自动合并相同质量"""
        if len(m1) == 0 or len(m2) == 0:
            return np.array([]), np.array([])
        # 笛卡尔积（向量化）
        masses = m1[:, None] + m2[None, :]
        probs = p1[:, None] * p2[None, :]
        masses = masses.ravel()
        probs = probs.ravel()
        # 按质量分箱合并（精度0.001）
        bins = np.round(masses * 1000).astype(np.int64)
        unique_bins = np.unique(bins)
        merged_masses = np.zeros(len(unique_bins))
        merged_probs = np.zeros(len(unique_bins))
        for i, b in enumerate(unique_bins):
            mask = bins == b
            merged_masses[i] = masses[mask][0]
            merged_probs[i] = probs[mask].sum()
        return merged_masses, merged_probs

    def _plot_result(self):
        self.ax.clear()
        # 用竖线表示峰
        for x, y in zip(self.result_x, self.result_y):
            self.ax.plot([x, x], [0, y], color=ACCENT, linewidth=0.8, alpha=0.8)
        self.ax.plot(self.result_x, self.result_y, '.', color=ACCENT2, markersize=3)
        self.ax.set_xlabel("Mass / z", color=TEXT, fontsize=11)
        self.ax.set_ylabel("Relative Intensity (%)", color=TEXT, fontsize=11)
        elems = self.elements_var.get()
        main = self.main_elem_var.get()
        max_n = self.max_n_var.get()
        res = self.resolution_var.get()
        self.ax.set_title(f"理论质谱: {elems} (主元素{main}, n≤{max_n}, R={res})",
                          color=TEXT, fontsize=11)
        self.ax.tick_params(colors=TEXT)
        self.ax.grid(True, alpha=0.3)
        self.ax.set_ylim(bottom=0)
        self.canvas.draw()

    def _export(self):
        if len(self.result_x) == 0:
            messagebox.showinfo("提示", "请先计算")
            return
        path = filedialog.asksaveasfilename(defaultextension=".txt",
                                            filetypes=[("文本文件", "*.txt"), ("CSV", "*.csv")])
        if not path:
            return
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"# 理论质谱计算\n")
            f.write(f"# 元素集: {self.elements_var.get()}\n")
            f.write(f"# 主元素: {self.main_elem_var.get()}, 最大n: {self.max_n_var.get()}\n")
            f.write(f"# 分辨率: {self.resolution_var.get()}, 电荷: {self.charge_var.get()}\n")
            f.write(f"# Mass/z\tIntensity\n")
            for x, y in zip(self.result_x, self.result_y):
                f.write(f"{x:.4f}\t{y:.4f}\n")
        self.status_var.set(f"已导出: {path}")

    def _import_to_main(self):
        if len(self.result_x) == 0:
            messagebox.showinfo("提示", "请先计算")
            return
        # 生成谱图名称
        name = f"理论_{self.elements_var.get().replace(',','')}_n{self.max_n_var.get()}_R{self.resolution_var.get()}"
        from types import SimpleNamespace
        spec = SimpleNamespace(
            db_id=None, name=name, filepath='', sample_name='理论计算',
            date=datetime.date.today().isoformat(), operator='', conditions='理论计算',
            ar_flow='', he_flow='', liquid_n2=False, power='',
            condensation_distance='', pressure='',
            mass_formula='-1.08608+0.02743*I+0.01722*I*I-1.9947*I*I*I*0.0000001',
            element_set=self.elements_var.get(), temperature='', acceleration_voltage='40 kV',
            hp_annotations='', formula_matches={}, formula_peak_map={},
            tags=['理论'], notes=f'理论计算: {self.elements_var.get()}, n≤{self.max_n_var.get()}, R={self.resolution_var.get()}',
            color=DEFAULT_COLORS[self.main_app.color_idx % len(DEFAULT_COLORS)],
            x=self.result_x.copy(), y_raw=self.result_y.copy(), y=self.result_y.copy(),
            baseline=None, peaks=None, visible=True, baseline_corrected=False, normalized=False,
            x_min=self.result_x.min(), x_max=self.result_x.max(),
            y_min=self.result_y.min(), y_max=self.result_y.max(),
            n_points=len(self.result_x))
        self.main_app.color_idx += 1
        spec.db_id = self.main_app.db.add_spectrum(spec)
        self.main_app.spectra.insert(0, spec)
        self.main_app._refresh_tree()
        self.main_app._refresh_tags()
        self.main_app._update_status(f"已导入理论谱图: {name}")
        messagebox.showinfo("成功", f"理论谱图已导入主界面\n名称: {name}")



class ScanSpectrumDialog:
    """扫谱界面：磁场电流扫描，法拉第杯/MCP采集，Mass-电流转换"""
    def __init__(self, parent, main_app):
        self.main_app = main_app
        self.win = tk.Toplevel(parent)
        self.win.title("扫谱 - 磁场电流扫描")
        self.win.geometry("1000x700")
        self.win.minsize(800, 600)
        self.scanning = False
        self.scan_data = {'current': [], 'intensity': []}
        self._build_ui()

    def _build_ui(self):
        # 顶部参数区
        param_frame = ttk.LabelFrame(self.win, text="扫描参数", padding=10)
        param_frame.pack(fill=tk.X, padx=10, pady=8)

        # 第一行：磁场电流范围
        row1 = ttk.Frame(param_frame)
        row1.pack(fill=tk.X, pady=3)
        ttk.Label(row1, text="磁场电流(A):", width=12).pack(side=tk.LEFT)
        self.cur_start = tk.StringVar(value="0.5")
        self.cur_end = tk.StringVar(value="5.0")
        self.cur_step = tk.StringVar(value="0.01")
        ttk.Entry(row1, textvariable=self.cur_start, width=8).pack(side=tk.LEFT, padx=2)
        ttk.Label(row1, text="~").pack(side=tk.LEFT)
        ttk.Entry(row1, textvariable=self.cur_end, width=8).pack(side=tk.LEFT, padx=2)
        ttk.Label(row1, text="步进:").pack(side=tk.LEFT, padx=(8,2))
        ttk.Entry(row1, textvariable=self.cur_step, width=8).pack(side=tk.LEFT, padx=2)
        ttk.Label(row1, text="A").pack(side=tk.LEFT)

        # 第二行：探测器选择
        row2 = ttk.Frame(param_frame)
        row2.pack(fill=tk.X, pady=3)
        ttk.Label(row2, text="探测器:", width=12).pack(side=tk.LEFT)
        self.detector = tk.StringVar(value="Faraday")
        ttk.Radiobutton(row2, text="法拉第杯 (Faraday Cup)", variable=self.detector, value="Faraday").pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(row2, text="MCP", variable=self.detector, value="MCP").pack(side=tk.LEFT, padx=5)

        # 实验参数行1：冷凝腔气压、温度、冷凝距离
        row_exp1 = ttk.Frame(param_frame)
        row_exp1.pack(fill=tk.X, pady=3)
        ttk.Label(row_exp1, text="实验参数:", width=12).pack(side=tk.LEFT)
        ttk.Label(row_exp1, text="气压(Pa):").pack(side=tk.LEFT)
        self.exp_pressure = tk.StringVar(value="")
        ttk.Entry(row_exp1, textvariable=self.exp_pressure, width=8).pack(side=tk.LEFT, padx=2)
        ttk.Label(row_exp1, text="温度(K):").pack(side=tk.LEFT, padx=(8,0))
        self.exp_temp = tk.StringVar(value="")
        ttk.Entry(row_exp1, textvariable=self.exp_temp, width=8).pack(side=tk.LEFT, padx=2)
        ttk.Label(row_exp1, text="冷凝距离(cm):").pack(side=tk.LEFT, padx=(8,0))
        self.exp_cond_dist = tk.StringVar(value="")
        ttk.Entry(row_exp1, textvariable=self.exp_cond_dist, width=8).pack(side=tk.LEFT, padx=2)
        ttk.Label(row_exp1, text="功率(W):").pack(side=tk.LEFT, padx=(8,0))
        self.exp_power = tk.StringVar(value="")
        ttk.Entry(row_exp1, textvariable=self.exp_power, width=8).pack(side=tk.LEFT, padx=2)

        # 实验参数行2：元素集、靶材个数、加速电压
        row_exp2 = ttk.Frame(param_frame)
        row_exp2.pack(fill=tk.X, pady=3)
        ttk.Label(row_exp2, text="", width=12).pack(side=tk.LEFT)
        ttk.Label(row_exp2, text="元素集:").pack(side=tk.LEFT)
        self.exp_elements = tk.StringVar(value="")
        ttk.Entry(row_exp2, textvariable=self.exp_elements, width=15).pack(side=tk.LEFT, padx=2)
        ttk.Label(row_exp2, text="靶材个数:").pack(side=tk.LEFT, padx=(8,0))
        self.exp_targets = tk.StringVar(value="")
        ttk.Entry(row_exp2, textvariable=self.exp_targets, width=6).pack(side=tk.LEFT, padx=2)
        ttk.Label(row_exp2, text="加速电压(kV):").pack(side=tk.LEFT, padx=(8,0))
        self.exp_voltage = tk.StringVar(value="40")
        ttk.Entry(row_exp2, textvariable=self.exp_voltage, width=8).pack(side=tk.LEFT, padx=2)

        # 第三行：Mass-电流公式
        row3 = ttk.Frame(param_frame)
        row3.pack(fill=tk.X, pady=3)
        ttk.Label(row3, text="Mass公式:", width=12).pack(side=tk.LEFT)
        self.mass_formula = tk.StringVar(value="-1.08608+0.02743*I+0.01722*I*I-1.9947e-7*I*I*I")
        ttk.Entry(row3, textvariable=self.mass_formula, width=60).pack(side=tk.LEFT, padx=2, fill=tk.X, expand=True)
        ttk.Button(row3, text="应用", command=self._apply_formula).pack(side=tk.LEFT, padx=2)

        # 第四行：x轴显示模式 + 控制按钮
        row4 = ttk.Frame(param_frame)
        row4.pack(fill=tk.X, pady=(8,0))
        ttk.Label(row4, text="X轴显示:").pack(side=tk.LEFT)
        self.x_mode = tk.StringVar(value="current")
        ttk.Radiobutton(row4, text="磁场电流/A", variable=self.x_mode, value="current", command=self._redraw).pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(row4, text="Mass/z", variable=self.x_mode, value="mass", command=self._redraw).pack(side=tk.LEFT, padx=5)

        ttk.Button(row4, text="开始扫描", command=self._start_scan).pack(side=tk.RIGHT, padx=2)
        ttk.Button(row4, text="停止", command=self._stop_scan, state=tk.DISABLED).pack(side=tk.RIGHT, padx=2)
        ttk.Button(row4, text="清空", command=self._clear).pack(side=tk.RIGHT, padx=2)
        ttk.Button(row4, text="保存数据", command=self._save_data).pack(side=tk.RIGHT, padx=2)

        # 图表区
        chart_frame = ttk.Frame(self.win)
        chart_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.fig = Figure(figsize=(8, 4), dpi=100, facecolor=BG)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, chart_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        # 导航工具栏
        toolbar_frame = ttk.Frame(chart_frame)
        toolbar_frame.pack(fill=tk.X, side=tk.BOTTOM)
        self.toolbar = NavigationToolbar2Tk(self.canvas, toolbar_frame)
        self.toolbar.config(background=PANEL)
        self.toolbar.update()
        self._init_plot()

        # 底部状态栏
        status_frame = ttk.Frame(self.win)
        status_frame.pack(fill=tk.X, padx=10, pady=5)
        self.progress = ttk.Progressbar(status_frame, mode='determinate')
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(status_frame, textvariable=self.status_var, foreground='#0066CC').pack(side=tk.LEFT, padx=5)
        self.cur_val = tk.StringVar(value="I=-")
        self.mass_val = tk.StringVar(value="M=-")
        self.int_val = tk.StringVar(value="信号=-")
        ttk.Label(status_frame, textvariable=self.cur_val, font=('SimSun', 10, 'bold')).pack(side=tk.LEFT, padx=8)
        ttk.Label(status_frame, textvariable=self.mass_val, font=('SimSun', 10, 'bold')).pack(side=tk.LEFT, padx=8)
        ttk.Label(status_frame, textvariable=self.int_val, font=('SimSun', 10, 'bold'), foreground='#C00000').pack(side=tk.LEFT, padx=8)

    def _init_plot(self):
        self.ax.clear()
        self.ax.set_xlabel("磁场电流 / A", color=TEXT, fontsize=11)
        self.ax.set_ylabel("束流强度", color=TEXT, fontsize=11)
        self.ax.set_title("扫谱曲线", color=TEXT, fontsize=12)
        self.ax.tick_params(colors=TEXT)
        self.ax.grid(True, alpha=0.3)
        self.line, = self.ax.plot([], [], 'b-', linewidth=0.8)
        self.canvas.draw()

    def _apply_formula(self):
        """验证并应用Mass公式"""
        formula = self.mass_formula.get().strip()
        try:
            I = 1.0
            eval(formula, {'__builtins__': {}}, {'I': I})
            messagebox.showinfo("成功", "公式有效，已应用")
            self._redraw()
        except Exception as e:
            messagebox.showerror("公式错误", f"公式无效: {e}")

    def _calc_mass(self, current):
        """根据公式计算Mass"""
        try:
            return eval(self.mass_formula.get(), {'__builtins__': {}}, {'I': current})
        except Exception:
            return current

    def _redraw(self):
        """重绘图表"""
        if not self.scan_data['current']:
            self._init_plot()
            return
        self.ax.clear()
        if self.x_mode.get() == 'mass':
            x_data = [self._calc_mass(i) for i in self.scan_data['current']]
            self.ax.set_xlabel("Mass / z", fontsize=11)
        else:
            x_data = self.scan_data['current']
            self.ax.set_xlabel("磁场电流 / A", color=TEXT, fontsize=11)
        det = "法拉第杯" if self.detector.get() == "Faraday" else "MCP"
        self.ax.set_ylabel(f"束流强度 ({det})", color=TEXT, fontsize=11)
        self.ax.set_title(f"扫谱曲线 - {det}", color=TEXT, fontsize=12)
        self.ax.tick_params(colors=TEXT)
        self.ax.grid(True, alpha=0.3)
        self.ax.plot(x_data, self.scan_data['intensity'], 'b-', linewidth=0.8)
        self.canvas.draw()

    def _start_scan(self):
        try:
            start = float(self.cur_start.get())
            end = float(self.cur_end.get())
            step = float(self.cur_step.get())
        except ValueError:
            messagebox.showerror("错误", "请输入有效的电流参数")
            return
        if step <= 0 or start >= end:
            messagebox.showerror("错误", "参数无效：起始<结束，步进>0")
            return

        self.scanning = True
        self.scan_data = {'current': [], 'intensity': []}
        self._init_plot()
        total = int((end - start) / step) + 1
        self.progress['maximum'] = total
        self.progress['value'] = 0

        # 禁用按钮
        for w in self.win.winfo_children():
            pass
        self._scan_point(start, end, step, 0, total)

    def _scan_point(self, cur, end, step, idx, total):
        if not self.scanning or cur > end + 1e-9:
            self._finish_scan()
            return
        # 模拟采集信号（实际应用中这里读取仪器数据）
        import random, math
        det = self.detector.get()
        if det == "Faraday":
            # 法拉第杯：模拟有峰的信号
            base = 1e-9
            peak = 5e-8 * math.exp(-((cur - 2.5)**2) / 0.1)
            noise = random.gauss(0, 1e-10)
            intensity = base + peak + noise
        else:
            # MCP：增益更高，噪声更大
            base = 1e-12
            peak = 1e-6 * math.exp(-((cur - 2.5)**2) / 0.05)
            noise = random.gauss(0, 1e-8)
            intensity = base + peak + noise

        self.scan_data['current'].append(cur)
        self.scan_data['intensity'].append(intensity)

        # 更新状态
        self.cur_val.set(f"I={cur:.4f}A")
        self.mass_val.set(f"M={self._calc_mass(cur):.2f}")
        self.int_val.set(f"信号={intensity:.3e}")
        self.progress['value'] = idx + 1
        self.status_var.set(f"扫描中... {idx+1}/{total}")

        # 实时更新图表（每10个点更新一次，避免太卡）
        if idx % 10 == 0 or idx == total - 1:
            self._redraw()

        # 下一个点
        self.win.after(20, lambda: self._scan_point(cur + step, end, step, idx + 1, total))

    def _stop_scan(self):
        self.scanning = False
        self.status_var.set("已停止")
        self._redraw()

    def _finish_scan(self):
        self.scanning = False
        self.status_var.set("扫描完成")
        self._redraw()
        messagebox.showinfo("完成", f"扫描完成，共 {len(self.scan_data['current'])} 个数据点")

    def _clear(self):
        if self.scanning:
            return
        self.scan_data = {'current': [], 'intensity': []}
        self._init_plot()
        self.status_var.set("已清空")
        self.progress['value'] = 0

    def _save_data(self):
        if not self.scan_data['current']:
            messagebox.showinfo("提示", "没有数据可保存")
            return
        # 生成包含实验参数的默认文件名
        parts = ["scan"]
        if self.exp_elements.get().strip():
            parts.append(self.exp_elements.get().strip().replace(',', '').replace(' ', ''))
        if self.exp_pressure.get().strip():
            parts.append(f"{self.exp_pressure.get().strip()}Pa")
        if self.exp_temp.get().strip():
            parts.append(f"{self.exp_temp.get().strip()}K")
        if self.exp_cond_dist.get().strip():
            parts.append(f"{self.exp_cond_dist.get().strip()}cm")
        if self.exp_power.get().strip():
            parts.append(f"{self.exp_power.get().strip()}W")
        if self.exp_voltage.get().strip():
            parts.append(f"{self.exp_voltage.get().strip()}kV")
        parts.append(self.detector.get())
        default_name = "_".join(parts) + ".txt"
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("CSV文件", "*.csv")],
            initialfile=default_name)
        if not path:
            return
        with open(path, 'w', encoding='utf-8') as f:
            det = "Faraday Cup" if self.detector.get() == "Faraday" else "MCP"
            f.write(f"# 扫谱数据\n")
            f.write(f"# 探测器: {det}\n")
            f.write(f"# Mass公式: {self.mass_formula.get()}\n")
            f.write(f"# 电流范围: {self.cur_start.get()}-{self.cur_end.get()} A, 步进 {self.cur_step.get()} A\n")
            f.write(f"# 冷凝腔气压: {self.exp_pressure.get()} Pa\n")
            f.write(f"# 温度: {self.exp_temp.get()} K\n")
            f.write(f"# 冷凝距离: {self.exp_cond_dist.get()} cm\n")
            f.write(f"# 功率: {self.exp_power.get()} W\n")
            f.write(f"# 元素集: {self.exp_elements.get()}\n")
            f.write(f"# 靶材个数: {self.exp_targets.get()}\n")
            f.write(f"# 加速电压: {self.exp_voltage.get()} kV\n")
            f.write(f"# Current(A)\tMass\tIntensity\n")
            for cur, inten in zip(self.scan_data['current'], self.scan_data['intensity']):
                f.write(f"{cur:.6f}\t{self._calc_mass(cur):.4f}\t{inten:.6e}\n")
        self.status_var.set(f"已保存: {path}")


class MergeConflictDialog:
    """合并冲突对比对话框"""
    def __init__(self, parent, old_data, new_data):
        self.result = None  # 'replace', 'skip', 'keep_both'
        self.win = tk.Toplevel(parent)
        self.win.title("发现重复谱图")
        self.win.geometry("700x450")
        self.win.transient(parent)
        self.win.grab_set()
        self._build_ui(old_data, new_data)
        self.win.wait_window()

    def _build_ui(self, old, new):
        ttk.Label(self.win, text="发现重复谱图，请选择处理方式：",
          font=('SimSun', 11, 'bold')).pack(pady=8)

        main = ttk.Frame(self.win)
        main.pack(fill=tk.BOTH, expand=True, padx=10)

        # 左右对比
        left = ttk.LabelFrame(main, text="当前数据库（旧）", padding=8)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))
        right = ttk.LabelFrame(main, text="待合并（新）", padding=8)
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))

        self._fill_info(left, old)
        self._fill_info(right, new)

        # 按钮
        btn_frame = ttk.Frame(self.win)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)
        ttk.Button(btn_frame, text="替换（用新的覆盖旧的）",
           command=lambda: self._set_result('replace')).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="保留旧的（跳过新的）",
           command=lambda: self._set_result('skip')).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="两个都保留（新的重命名）",
           command=lambda: self._set_result('keep_both')).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="取消合并",
           command=lambda: self._set_result('cancel')).pack(side=tk.RIGHT, padx=5)

    def _fill_info(self, frame, data):
        fields = [
            ('名称', data.get('name', '')),
            ('实验日期', data.get('date', '')),
            ('样品名', data.get('sample_name', '')),
            ('操作人员', data.get('operator', '')),
            ('点数', str(data.get('n_points', ''))),
            ('范围', f"{data.get('x_min', '')} - {data.get('x_max', '')}"),
            ('Ar流量', data.get('ar_flow', '')),
            ('He流量', data.get('he_flow', '')),
            ('功率', data.get('power', '')),
            ('气压', data.get('pressure', '')),
            ('冷凝距离', data.get('condensation_distance', '')),
            ('备注', (data.get('notes', '') or '')[:50]),
        ]
        for i, (label, val) in enumerate(fields):
            ttk.Label(frame, text=f"{label}:", font=('SimSun', 9, 'bold')).grid(row=i, column=0, sticky=tk.W, pady=1)
            ttk.Label(frame, text=str(val) if val else '-', wraplength=250).grid(row=i, column=1, sticky=tk.W, pady=1)

    def _set_result(self, result):
        self.result = result
        self.win.destroy()



class FontSettingsDialog:
    """字体设置对话框"""
    def __init__(self, parent, app):
        self.app = app
        self.win = tk.Toplevel(parent)
        self.win.title("字体设置")
        self.win.geometry("450x380")
        self.win.transient(parent)
        self.win.grab_set()
        self._build_ui()

    def _build_ui(self):
        frame = ttk.Frame(self.win, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="界面字体:", font=('SimSun', 10, 'bold')).grid(row=0, column=0, sticky=tk.W, pady=(0,5))
        self.ui_font_var = tk.StringVar(value=FONT_CONFIG['ui_font'])
        ui_fonts = ['SimSun', 'SimHei', 'Microsoft YaHei', 'KaiTi', 'FangSong', 'Arial', 'Times New Roman', 'Segoe UI']
        ttk.Combobox(frame, textvariable=self.ui_font_var, values=ui_fonts, width=25).grid(row=0, column=1, sticky=tk.W, pady=(0,5))

        ttk.Label(frame, text="界面字号:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.ui_size_var = tk.StringVar(value=str(FONT_CONFIG['ui_size']))
        ttk.Combobox(frame, textvariable=self.ui_size_var, values=['8','9','10','11','12','14'], width=25).grid(row=1, column=1, sticky=tk.W, pady=5)

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(row=2, column=0, columnspan=2, sticky=tk.EW, pady=10)

        ttk.Label(frame, text="图表英文字体:", font=('SimSun', 10, 'bold')).grid(row=3, column=0, sticky=tk.W, pady=5)
        self.en_font_var = tk.StringVar(value=FONT_CONFIG['chart_en_font'])
        en_fonts = ['Times New Roman', 'Arial', 'Helvetica', 'Courier New', 'SimSun', 'DejaVu Sans']
        ttk.Combobox(frame, textvariable=self.en_font_var, values=en_fonts, width=25).grid(row=3, column=1, sticky=tk.W, pady=5)

        ttk.Label(frame, text="图表中文字体:").grid(row=4, column=0, sticky=tk.W, pady=5)
        self.cn_font_var = tk.StringVar(value=FONT_CONFIG['chart_cn_font'])
        cn_fonts = ['SimSun', 'SimHei', 'Microsoft YaHei', 'KaiTi', 'FangSong', 'Arial Unicode MS']
        ttk.Combobox(frame, textvariable=self.cn_font_var, values=cn_fonts, width=25).grid(row=4, column=1, sticky=tk.W, pady=5)

        ttk.Label(frame, text="图表字号:").grid(row=5, column=0, sticky=tk.W, pady=5)
        self.chart_size_var = tk.StringVar(value=str(FONT_CONFIG['chart_size']))
        ttk.Combobox(frame, textvariable=self.chart_size_var, values=['8','9','10','11','12','14'], width=25).grid(row=5, column=1, sticky=tk.W, pady=5)

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(row=6, column=0, columnspan=2, sticky=tk.EW, pady=10)

        ttk.Label(frame, text="预览: 质谱图 Mass Spectrum 123.456", foreground='#555').grid(row=7, column=0, columnspan=2, sticky=tk.W, pady=5)

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=8, column=0, columnspan=2, sticky=tk.EW, pady=10)
        ttk.Button(btn_frame, text="应用并保存", command=self._apply).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="恢复默认", command=self._reset).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="取消", command=self.win.destroy).pack(side=tk.RIGHT, padx=5)

    def _apply(self):
        FONT_CONFIG['ui_font'] = self.ui_font_var.get()
        FONT_CONFIG['ui_size'] = int(self.ui_size_var.get())
        FONT_CONFIG['chart_en_font'] = self.en_font_var.get()
        FONT_CONFIG['chart_cn_font'] = self.cn_font_var.get()
        FONT_CONFIG['chart_size'] = int(self.chart_size_var.get())
        save_font_config()
        # 重新设置matplotlib字体
        setup_chinese_font()
        # 应用到界面
        self.app._apply_ui_font()
        # 重绘图表
        self.app.refresh_plot()
        messagebox.showinfo("提示", "字体设置已应用，部分界面需重启后完全生效")
        self.win.destroy()

    def _reset(self):
        self.ui_font_var.set('SimSun')
        self.ui_size_var.set('9')
        self.en_font_var.set('Times New Roman')
        self.cn_font_var.set('SimSun')
        self.chart_size_var.set('10')



class FormulaCalculator:
    """分子式预计算工具：输入元素和个数，批量计算所有同位素组合的精确质量"""
    def __init__(self, parent, main_app):
        self.main_app = main_app
        self.win = tk.Toplevel(parent)
        self.win.title("分子式预计算")
        self.win.geometry("1000x700")
        self.win.minsize(700, 500)
        self.results = []
        self.count_vars = {}
        self._build_ui()

    def _build_ui(self):
        # 顶部参数区
        param_frame = ttk.LabelFrame(self.win, text="计算参数", padding=8)
        param_frame.pack(fill=tk.X, padx=8, pady=6)

        row1 = ttk.Frame(param_frame)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="元素(逗号分隔):").pack(side=tk.LEFT, padx=(4, 4))
        self.elem_var = tk.StringVar(value="Ga,As,O,H")
        elem_entry = ttk.Entry(row1, textvariable=self.elem_var, width=25)
        elem_entry.pack(side=tk.LEFT, padx=2)
        ttk.Button(row1, text="确认元素", command=self._on_confirm_elements).pack(side=tk.LEFT, padx=4)

        ttk.Label(row1, text="电荷:").pack(side=tk.LEFT, padx=(16, 2))
        self.charge_var = tk.StringVar(value="1")
        ttk.Entry(row1, textvariable=self.charge_var, width=5).pack(side=tk.LEFT, padx=2)

        ttk.Label(row1, text="最小丰度(%):").pack(side=tk.LEFT, padx=(16, 2))
        self.minabund_var = tk.StringVar(value="0.5")
        ttk.Entry(row1, textvariable=self.minabund_var, width=6).pack(side=tk.LEFT, padx=2)

        ttk.Button(row1, text="开始计算", command=self._calculate).pack(side=tk.LEFT, padx=16)
        ttk.Button(row1, text="导出JSON", command=self._export_json).pack(side=tk.LEFT, padx=2)
        ttk.Button(row1, text="发送到高精度标注", command=self._send_to_annotator).pack(side=tk.LEFT, padx=2)

        # 个数上限区
        self.counts_frame = ttk.LabelFrame(param_frame, text="各元素个数上限", padding=4)
        self.counts_frame.pack(fill=tk.X, pady=(4, 0))
        self._on_confirm_elements()

        # 搜索过滤
        filter_frame = ttk.Frame(param_frame)
        filter_frame.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(filter_frame, text="搜索:").pack(side=tk.LEFT, padx=(4, 2))
        self.search_var = tk.StringVar()
        self.search_var.trace_add('write', lambda *a: self._refresh_table())
        ttk.Entry(filter_frame, textvariable=self.search_var, width=20).pack(side=tk.LEFT, padx=2)
        ttk.Label(filter_frame, text="质量范围:").pack(side=tk.LEFT, padx=(16, 2))
        self.mass_min_var = tk.StringVar()
        ttk.Entry(filter_frame, textvariable=self.mass_min_var, width=8).pack(side=tk.LEFT, padx=2)
        ttk.Label(filter_frame, text="-").pack(side=tk.LEFT)
        self.mass_max_var = tk.StringVar()
        ttk.Entry(filter_frame, textvariable=self.mass_max_var, width=8).pack(side=tk.LEFT, padx=2)
        ttk.Button(filter_frame, text="应用", command=self._refresh_table).pack(side=tk.LEFT, padx=4)

        # 结果表格
        table_frame = ttk.Frame(self.win)
        table_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        cols = ('formula', 'mass', 'abund', 'composition')
        self.tree = ttk.Treeview(table_frame, columns=cols, show='headings', selectmode='browse')
        self.tree.heading('formula', text='分子式')
        self.tree.heading('mass', text='精确质量')
        self.tree.heading('abund', text='相对丰度(%)')
        self.tree.heading('composition', text='元素组成')
        self.tree.column('formula', width=200, anchor=tk.W)
        self.tree.column('mass', width=120, anchor=tk.E)
        self.tree.column('abund', width=100, anchor=tk.E)
        self.tree.column('composition', width=300, anchor=tk.W)

        vsb = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        # 点击列标题排序
        for col in cols:
            self.tree.heading(col, command=lambda c=col: self._sort_by(c))

        # 底部状态栏
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.win, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W).pack(fill=tk.X, side=tk.BOTTOM)

    def _on_confirm_elements(self):
        elements = [e.strip() for e in self.elem_var.get().replace('，', ',').split(',') if e.strip()]
        elements = [e for e in elements if e in ELEMENT_ISOTOPES]
        # 清除旧的
        for w in self.counts_frame.winfo_children():
            w.destroy()
        self.count_vars = {}
        for i, elem in enumerate(elements):
            ttk.Label(self.counts_frame, text=f"{elem}:").grid(row=0, column=i*2, padx=(8, 2), pady=2)
            var = tk.StringVar(value='6')
            self.count_vars[elem] = var
            ttk.Entry(self.counts_frame, textvariable=var, width=5).grid(row=0, column=i*2+1, padx=2, pady=2)

    def _calculate(self):
        elements = [e.strip() for e in self.elem_var.get().replace('，', ',').split(',') if e.strip()]
        elements = [e for e in elements if e in ELEMENT_ISOTOPES]
        if not elements:
            messagebox.showwarning("提示", "请输入有效的元素符号")
            return
        max_counts = {e: int(v.get()) for e, v in self.count_vars.items()}
        charge = int(self.charge_var.get())
        min_abund = float(self.minabund_var.get()) / 100.0

        # 预估组合数，过大时提示
        estimated = 1
        for elem in elements:
            n_iso = len(ELEMENT_ISOTOPES.get(elem, []))
            mx = max_counts.get(elem, 6)
            # 组合数近似 C(mx+n_iso, n_iso)
            from math import comb
            estimated *= comb(mx + n_iso, n_iso)
        if estimated > 2000000:
            msg = f"预估组合数约 {estimated:,}，计算可能较慢。是否继续？\n建议降低个数上限或最小丰度阈值。"
            if not messagebox.askyesno("确认", msg):
                return

        self.status_var.set("计算中...（递归剪枝中）")
        self.win.update_idletasks()

        # 后台线程计算，避免UI卡死
        def _worker():
            try:
                results = generate_cluster_isotope_formulas(
                    elements, max_counts, charge, min_abund,
                    max_results=500000)
                self.win.after(0, lambda: self._on_calc_done(results))
            except Exception as e:
                self.win.after(0, lambda: self._on_calc_error(str(e)))

        import threading
        threading.Thread(target=_worker, daemon=True).start()

    def _on_calc_done(self, results):
        self.results = results
        self._refresh_table()
        if len(results) >= 500000:
            self.status_var.set(f"计算完成：{len(results):,} 个组合（已达上限，建议提高丰度阈值）")
        else:
            self.status_var.set(f"计算完成：共 {len(results):,} 个同位素组合")

    def _on_calc_error(self, err):
        self.status_var.set(f"计算错误: {err}")
        messagebox.showerror("计算错误", err)

    def _refresh_table(self):
        self.tree.delete(*self.tree.get_children())
        keyword = self.search_var.get().strip().lower()
        m_min = float(self.mass_min_var.get()) if self.mass_min_var.get().strip() else None
        m_max = float(self.mass_max_var.get()) if self.mass_max_var.get().strip() else None

        count = 0
        for formula, mass, abund in self.results:
            if keyword and keyword not in formula.lower():
                continue
            if m_min is not None and mass < m_min:
                continue
            if m_max is not None and mass > m_max:
                continue
            # 解析元素组成
            comp = self._parse_composition(formula)
            self.tree.insert('', 'end', values=(formula, f'{mass:.6f}', f'{abund*100:.3f}', comp))
            count += 1
        self.status_var.set(f"显示 {count} / {len(self.results)} 个组合")

    def _parse_composition(self, formula):
        """从分子式字符串解析元素组成，如 '69Ga2 75As' -> 'Ga2 As1'"""
        import re as _re
        comp = {}
        for part in formula.split():
            m = _re.match(r'^(\d+)([A-Z][a-z]?)(\d*)$', part)
            if m:
                elem = m.group(2)
                cnt = int(m.group(3)) if m.group(3) else 1
                comp[elem] = comp.get(elem, 0) + cnt
        return ' '.join(f'{e}{c}' for e, c in sorted(comp.items()))

    def _sort_by(self, col):
        items = [(self.tree.set(k, col), k) for k in self.tree.get_children('')]
        try:
            items.sort(key=lambda x: float(x[0]))
        except ValueError:
            items.sort(key=lambda x: x[0])
        for idx, (_, k) in enumerate(items):
            self.tree.move(k, '', idx)

    def _export_json(self):
        if not self.results:
            messagebox.showinfo("提示", "请先计算")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON文件", "*.json")],
            initialfile="formula_library.json")
        if not path:
            return
        import json as _json
        elements = [e.strip() for e in self.elem_var.get().replace('，', ',').split(',') if e.strip()]
        data = {
            'elements': elements,
            'max_counts': {e: int(v.get()) for e, v in self.count_vars.items()},
            'charge': int(self.charge_var.get()),
            'min_abundance': float(self.minabund_var.get()) / 100.0,
            'count': len(self.results),
            'formulas': [{'formula': f, 'mass': m, 'abundance': a} for f, m, a in self.results]
        }
        with open(path, 'w', encoding='utf-8') as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
        self.status_var.set(f"已导出到: {path}")

    def _send_to_annotator(self):
        specs = self.main_app._get_selected_spectra()
        if not specs:
            messagebox.showinfo("提示", "请先在主界面选择一个质谱图")
            return
        if not self.results:
            messagebox.showinfo("提示", "请先计算分子式")
            return
        # 打开高精度标注并传递参数
        annotator = HighPrecisionAnnotator(self.win, specs[0], self.main_app)
        annotator.elem_var.set(self.elem_var.get())
        elements = [e.strip() for e in self.elem_var.get().replace('，', ',').split(',') if e.strip()]
        elements = [e for e in elements if e in ELEMENT_ISOTOPES]
        annotator._rebuild_counts(elements)
        for e, v in self.count_vars.items():
            if e in annotator.count_vars:
                annotator.count_vars[e].set(v.get())
        annotator.charge_var.set(self.charge_var.get())
        annotator.minabund_var.set(self.minabund_var.get())
        self.status_var.set("已发送参数到高精度标注窗口")



class HighPrecisionAnnotator:
    """质谱高精度标注：按30u分段，每段独立寻峰+标注，纵向排列成长图"""

    def __init__(self, parent, spectrum, main_app):
        self.spec = spectrum
        self.main_app = main_app
        self.win = tk.Toplevel(parent)
        self.win.title(f"质谱高精度标注 - {spectrum.name}")
        self.win.geometry("1200x800")
        self.win.minsize(900, 500)
        self.segments = []
        self._current_fig = None
        self.hp_xaxis_mode = tk.StringVar(value='mass')
        self.selected_formulas = None  # 用户挑取的分子式列表，None表示全部
        self.per_peak_n = 2  # 每峰标注分子式数量
        self._build_ui()
        if self._load_from_db():
            self._draw_segments()
            self.status_var.set(f"已加载保存的标注：{len(self.segments)}段")
        else:
            self._draw_overview()

    def _build_ui(self):
        ctrl = ttk.LabelFrame(self.win, text="标注控制", padding=8)
        ctrl.pack(fill=tk.X, padx=6, pady=(6, 3))
        row1 = ttk.Frame(ctrl); row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="元素:", width=8).pack(side=tk.LEFT)
        self.elem_var = tk.StringVar(value='')
        e = ttk.Entry(row1, textvariable=self.elem_var, width=20)
        e.pack(side=tk.LEFT, padx=2); e.bind('<Return>', lambda ev: self._confirm_elements())
        ttk.Button(row1, text="确认", command=self._confirm_elements, width=5).pack(side=tk.LEFT, padx=2)
        ttk.Button(row1, text="从样品名解析", command=self._auto_parse, width=12).pack(side=tk.LEFT, padx=2)
        ttk.Label(row1, text="分段宽度(u):").pack(side=tk.LEFT, padx=(12, 0))
        self.seg_width_var = tk.StringVar(value='30')
        sw = ttk.Entry(row1, textvariable=self.seg_width_var, width=5)
        sw.pack(side=tk.LEFT, padx=2); sw.bind('<Return>', lambda ev: self._run_annotation())
        ttk.Radiobutton(row1, text="电流(A)", variable=self.hp_xaxis_mode,
                        value='current', command=self._redraw).pack(side=tk.LEFT, padx=(12, 2))
        ttk.Radiobutton(row1, text="Mass(m/z)", variable=self.hp_xaxis_mode,
                        value='mass', command=self._redraw).pack(side=tk.LEFT, padx=2)
        row2 = ttk.Frame(ctrl); row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="容差(Da):").pack(side=tk.LEFT)
        self.tol_var = tk.StringVar(value='0.3')
        t = ttk.Entry(row2, textvariable=self.tol_var, width=6)
        t.pack(side=tk.LEFT, padx=2); t.bind('<Return>', lambda ev: self._run_annotation())
        ttk.Label(row2, text="电荷:").pack(side=tk.LEFT, padx=(8, 0))
        self.charge_var = tk.StringVar(value='1')
        c = ttk.Entry(row2, textvariable=self.charge_var, width=4)
        c.pack(side=tk.LEFT, padx=2); c.bind('<Return>', lambda ev: self._run_annotation())
        ttk.Label(row2, text="最小丰度:").pack(side=tk.LEFT, padx=(8, 0))
        self.minabund_var = tk.StringVar(value='0.5')
        a = ttk.Entry(row2, textvariable=self.minabund_var, width=5)
        a.pack(side=tk.LEFT, padx=2); a.bind('<Return>', lambda ev: self._run_annotation())
        ttk.Label(row2, text="%", font=('', 8)).pack(side=tk.LEFT)
        ttk.Label(row2, text="每段前N峰:").pack(side=tk.LEFT, padx=(8, 0))
        self.topn_var = tk.StringVar(value='15')
        tn = ttk.Entry(row2, textvariable=self.topn_var, width=5)
        tn.pack(side=tk.LEFT, padx=2); tn.bind('<Return>', lambda ev: self._run_annotation())
        ttk.Label(row2, text="每峰标注:").pack(side=tk.LEFT, padx=(8, 0))
        self.per_peak_var = tk.StringVar(value='2')
        ttk.Radiobutton(row2, text="2个", variable=self.per_peak_var, value='2').pack(side=tk.LEFT, padx=2)
        ttk.Radiobutton(row2, text="3个", variable=self.per_peak_var, value='3').pack(side=tk.LEFT, padx=2)
        row3 = ttk.Frame(ctrl); row3.pack(fill=tk.X, pady=2)
        ttk.Label(row3, text="个数上限:", width=8).pack(side=tk.LEFT)
        self.counts_frame = ttk.Frame(row3)
        self.counts_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.count_vars = {}
        row4 = ttk.Frame(ctrl); row4.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(row4, text="分段自动标注", command=self._run_annotation,
           style='Accent.TButton').pack(side=tk.LEFT, padx=2)
        ttk.Button(row4, text="挑取分子式", command=self._open_formula_picker).pack(side=tk.LEFT, padx=2)
        self.picked_label = ttk.Label(row4, text="", foreground=ACCENT, font=('', 8))
        self.picked_label.pack(side=tk.LEFT, padx=4)
        ttk.Button(row4, text="清除标注", command=self._clear_annotations).pack(side=tk.LEFT, padx=2)
        ttk.Button(row4, text="导出长图", command=self._export_image).pack(side=tk.LEFT, padx=2)
        self.hp_coord_var = tk.StringVar(value="X: -    Y: -")
        ttk.Label(row4, textvariable=self.hp_coord_var, foreground=ACCENT,
          font=('Consolas', 9)).pack(side=tk.RIGHT, padx=12)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(row4, textvariable=self.status_var, foreground=MUTED).pack(side=tk.RIGHT, padx=8)
        plot_outer = ttk.Frame(self.win)
        plot_outer.pack(fill=tk.BOTH, expand=True, padx=6, pady=(3, 6))
        self.plot_canvas = tk.Canvas(plot_outer, highlightthickness=0)
        plot_sb = ttk.Scrollbar(plot_outer, orient='vertical', command=self.plot_canvas.yview)
        self.plot_inner = ttk.Frame(self.plot_canvas)
        self.plot_inner.bind('<Configure>', lambda ev: self.plot_canvas.configure(
            scrollregion=self.plot_canvas.bbox('all')))
        self.plot_canvas.create_window((0, 0), window=self.plot_inner, anchor='nw')
        self.plot_canvas.configure(yscrollcommand=plot_sb.set)
        self.plot_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        plot_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.plot_canvas.bind_all('<MouseWheel>',
            lambda ev: self.plot_canvas.yview_scroll(int(-1*(ev.delta/120)), 'units'))

    def _confirm_elements(self):
        s = self.elem_var.get().strip()
        if not s: self._rebuild_counts([]); return
        elems = [e.strip() for e in s.replace('，', ',').split(',') if e.strip()]
        valid = [e for e in elems if e in ELEMENT_ISOTOPES]
        invalid = [e for e in elems if e not in ELEMENT_ISOTOPES]
        if invalid:
            messagebox.showwarning('提示', f'未识别：{", ".join(invalid)}\n有效：{", ".join(valid)}')
        self._rebuild_counts(valid)

    def _auto_parse(self):
        name = self.spec.sample_name or self.spec.name
        elems = parse_elements_from_name(name)
        if elems:
            seen = set(); unique = []
            for e in elems:
                if e not in seen: seen.add(e); unique.append(e)
            self.elem_var.set(','.join(unique))
            self._rebuild_counts(unique)
        else:
            messagebox.showinfo('提示', '未能从样品名解析出元素。')

    def _rebuild_counts(self, elements):
        for w in self.counts_frame.winfo_children(): w.destroy()
        self.count_vars = {}
        if not elements:
            ttk.Label(self.counts_frame, text='（无元素）', foreground=MUTED).pack(anchor=tk.W)
            return
        for i, elem in enumerate(elements):
            f = ttk.Frame(self.counts_frame)
            f.grid(row=i//5, column=i%5, sticky=tk.W, padx=6, pady=1)
            ttk.Label(f, text=f'{elem}:', width=4).pack(side=tk.LEFT)
            v = tk.StringVar(value='6')
            self.count_vars[elem] = v
            sb = ttk.Spinbox(f, from_=0, to=999, textvariable=v, width=4)
            sb.pack(side=tk.LEFT); sb.bind('<Return>', lambda ev: self._run_annotation())

    def _clear_annotations(self):
        self.segments = []
        self.spec.hp_annotations = ''
        self.main_app.db.update_spectrum(self.spec)
        self._draw_overview()
        self.status_var.set("已清除标注（数据库同步清除）")

    def _open_formula_picker(self):
        """打开分子式挑取窗口（带勾选框）"""
        elements = [e.strip() for e in self.elem_var.get().replace('，', ',').split(',') if e.strip()]
        elements = [e for e in elements if e in ELEMENT_ISOTOPES]
        if not elements:
            messagebox.showinfo("提示", "请先输入元素并确认")
            return
        max_counts = {e: int(v.get()) for e, v in self.count_vars.items()}
        charge = int(self.charge_var.get())
        min_abund = float(self.minabund_var.get()) / 100.0

        self.status_var.set("正在计算分子式列表...")
        self.win.update_idletasks()
        all_formulas = generate_cluster_isotope_formulas(elements, max_counts, charge, min_abund)
        if not all_formulas:
            messagebox.showinfo("提示", "没有生成任何分子式，请调整参数")
            return

        picker = tk.Toplevel(self.win)
        picker.title(f"挑取分子式（共 {len(all_formulas)} 个）")
        picker.geometry("800x650")
        picker.transient(self.win)
        picker.grab_set()

        # 搜索和筛选
        search_frame = ttk.Frame(picker)
        search_frame.pack(fill=tk.X, padx=8, pady=6)
        ttk.Label(search_frame, text="搜索:").pack(side=tk.LEFT)
        search_var = tk.StringVar()
        ttk.Entry(search_frame, textvariable=search_var, width=20).pack(side=tk.LEFT, padx=4)
        ttk.Label(search_frame, text="质量:").pack(side=tk.LEFT, padx=(12, 2))
        mmin_var = tk.StringVar()
        ttk.Entry(search_frame, textvariable=mmin_var, width=7).pack(side=tk.LEFT)
        ttk.Label(search_frame, text="-").pack(side=tk.LEFT)
        mmax_var = tk.StringVar()
        ttk.Entry(search_frame, textvariable=mmax_var, width=7).pack(side=tk.LEFT)
        ttk.Label(search_frame, text="自动预选:").pack(side=tk.LEFT, padx=(12, 2))
        auto_n_var = tk.StringVar(value='50')
        ttk.Entry(search_frame, textvariable=auto_n_var, width=5).pack(side=tk.LEFT)
        ttk.Label(search_frame, text="个").pack(side=tk.LEFT)

        # Treeview带勾选框
        tree_frame = ttk.Frame(picker)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        cols = ('check', 'formula', 'mass', 'abund')
        tree = ttk.Treeview(tree_frame, columns=cols, show='headings', selectmode='browse')
        tree.heading('check', text='选')
        tree.heading('formula', text='分子式')
        tree.heading('mass', text='精确质量')
        tree.heading('abund', text='丰度(%)')
        tree.column('check', width=40, anchor=tk.CENTER)
        tree.column('formula', width=250, anchor=tk.W)
        tree.column('mass', width=100, anchor=tk.E)
        tree.column('abund', width=80, anchor=tk.E)
        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        # 存储所有数据
        formula_data = [(f, m, a) for f, m, a in all_formulas]
        checked = set()
        if self.selected_formulas:
            checked = set(self.selected_formulas)

        def _refresh():
            tree.delete(*tree.get_children())
            kw = search_var.get().strip().lower()
            mmin = float(mmin_var.get()) if mmin_var.get().strip() else None
            mmax = float(mmax_var.get()) if mmax_var.get().strip() else None
            for f, m, a in formula_data:
                if kw and kw not in f.lower(): continue
                if mmin is not None and m < mmin: continue
                if mmax is not None and m > mmax: continue
                mark = '[√]' if f in checked else '[ ]'
                tree.insert('', 'end', values=(mark, f, f'{m:.4f}', f'{a*100:.2f}'), tags=(f,))

        def _toggle_check(event):
            item = tree.identify_row(event.y)
            col = tree.identify_column(event.x)
            if not item or col != '#1':
                return
            vals = tree.item(item, 'values')
            f = vals[1]
            if f in checked:
                checked.remove(f)
                tree.item(item, values=('[ ]',) + tuple(vals[1:]))
            else:
                checked.add(f)
                tree.item(item, values=('[√]',) + tuple(vals[1:]))

        tree.bind('<Button-1>', _toggle_check)

        def _auto_select():
            """自动预选：O/H优先，丰度低优先"""
            n = int(auto_n_var.get())
            # 排序：O/H优先，丰度低优先
            def _key(item):
                f, m, a = item
                has_oh = ('O' in f or 'H' in f)
                return (0 if has_oh else 1, a)
            sorted_data = sorted(formula_data, key=_key)
            checked.clear()
            for f, m, a in sorted_data[:n]:
                checked.add(f)
            _refresh()

        def _select_all():
            for item in tree.get_children():
                f = tree.item(item, 'values')[1]
                checked.add(f)
            _refresh()

        def _deselect_all():
            checked.clear()
            _refresh()

        _refresh()
        # 如果之前没有选中，自动预选50个
        if not self.selected_formulas:
            _auto_select()

        search_var.trace_add('write', lambda *a: _refresh())
        mmin_var.trace_add('write', lambda *a: _refresh())
        mmax_var.trace_add('write', lambda *a: _refresh())

        # 按钮
        btn_frame = ttk.Frame(picker)
        btn_frame.pack(fill=tk.X, padx=8, pady=6)
        ttk.Button(btn_frame, text="自动预选", command=_auto_select).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="全选", command=_select_all).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="全不选", command=_deselect_all).pack(side=tk.LEFT, padx=2)
        ttk.Label(btn_frame, text="点击[ ]/[√]切换勾选").pack(side=tk.LEFT, padx=12)

        def _on_confirm():
            if checked:
                self.selected_formulas = list(checked)
                self.picked_label.config(text=f"已挑取 {len(checked)} 个")
            else:
                self.selected_formulas = None
                self.picked_label.config(text="")
            picker.destroy()
            self.status_var.set(f"已挑取 {len(self.selected_formulas or [])} 个分子式")

        def _on_reset():
            self.selected_formulas = None
            self.picked_label.config(text="")
            picker.destroy()

        ttk.Button(btn_frame, text="确认挑取", command=_on_confirm, style='Accent.TButton').pack(side=tk.RIGHT, padx=2)
        ttk.Button(btn_frame, text="重置为全部", command=_on_reset).pack(side=tk.RIGHT, padx=2)
        ttk.Button(btn_frame, text="取消", command=picker.destroy).pack(side=tk.RIGHT, padx=2)

    def _run_annotation(self):
        s = self.elem_var.get().strip()
        if not s:
            messagebox.showinfo('提示', '请先输入元素并确认。'); return
        elements = [e.strip() for e in s.replace('，', ',').split(',') if e.strip()]
        elements = [e for e in elements if e in ELEMENT_ISOTOPES]
        if not elements:
            messagebox.showerror('错误', '未识别到有效元素。'); return
        if not self.count_vars: self._rebuild_counts(elements)
        max_counts = {}
        for e in elements:
            try: max_counts[e] = int(self.count_vars.get(e, tk.StringVar(value='6')).get())
            except (ValueError, tk.TclError): max_counts[e] = 6
        try: tol = float(self.tol_var.get())
        except ValueError: tol = 0.3
        try: charge = int(self.charge_var.get())
        except ValueError: charge = 1
        try: min_abund = float(self.minabund_var.get()) / 100.0
        except ValueError: min_abund = 0.005
        try: top_n = int(self.topn_var.get())
        except ValueError: top_n = 15
        try: seg_w = float(self.seg_width_var.get())
        except ValueError: seg_w = 30.0
        all_formulas = generate_cluster_isotope_formulas(elements, max_counts, charge, min_abund)
        if self.selected_formulas:
            # 只使用用户挑取的分子式
            picked_set = set(self.selected_formulas)
            formulas = [f for f in all_formulas if f[0] in picked_set]
        else:
            formulas = all_formulas
        if not formulas:
            messagebox.showinfo('提示', '未生成同位素组合，请调大个数上限。'); return
        all_mass = self.main_app._current_to_mass(self.spec.x, self.spec)
        m_min = float(np.floor(all_mass.min() / seg_w) * seg_w)
        m_max = float(np.ceil(all_mass.max() / seg_w) * seg_w)
        self.segments = []
        total_p = total_a = 0
        m0 = m_min
        while m0 < m_max:
            m1 = m0 + seg_w
            mask = (all_mass >= m0) & (all_mass < m1)
            if mask.sum() < 10: m0 = m1; continue
            sx = self.spec.x[mask]; sy = self.spec.y[mask]; sm = all_mass[mask]
            sp = detect_peaks(sx, sy)
            if sp is None or len(sp) == 0: m0 = m1; continue
            px = sp['x'].values; ph = sp['height'].values
            pm = self.main_app._current_to_mass(px, self.spec)
            ann = {}
            for i, p in enumerate(pm):
                mt = []
                for formula, mass, abund in formulas:
                    if abs(p - mass) <= tol:
                        mt.append((formula, mass, abs(p-mass), abund))
                if mt:
                    mt.sort(key=lambda x: x[2])
                    # 规范化分子式：同位素组按质量数排序，避免顺序不同的假重复
                    import re as _re
                    def _norm_formula(f):
                        parts = f.split()
                        def _mass_num(p):
                            m = _re.match(r'^(\d+)', p)
                            return int(m.group(1)) if m else 0
                        parts.sort(key=lambda x: (_mass_num(x), x))
                        return ' '.join(parts)
                    seen_f = set()
                    mt_unique = []
                    for item in mt:
                        nf = _norm_formula(item[0])
                        if nf not in seen_f:
                            seen_f.add(nf)
                            mt_unique.append((nf,) + item[1:])
                    # 按规则排序：O/H优先，丰度低优先，然后取前N个
                    def _sort_key(item):
                        f = item[0]
                        has_oh = ('O' in f or 'H' in f)
                        abund = item[3]
                        return (0 if has_oh else 1, abund)  # O/H在前，丰度低在前
                    mt_unique.sort(key=_sort_key)
                    n_keep = int(self.per_peak_var.get())
                    ann[p] = (mt_unique[:n_keep], ph[i], px[i])
            if len(ann) > top_n:
                ann = dict(sorted(ann.items(), key=lambda x: x[1][1], reverse=True)[:top_n])
            self.segments.append((m0, m1, sp, ann, sx, sy, sm))
            total_p += len(sp); total_a += len(ann)
            m0 = m1
        # 全局去重：同一分子式只在误差最小的峰位保留
        all_items = []  # (seg_idx, peak_mass, formula, err, peak_h, peak_x)
        for si, (m0, m1, sp, ann, sx, sy, sm) in enumerate(self.segments):
            for pm, (matches, ph, px) in ann.items():
                for formula, cm, err, ab in matches:
                    all_items.append((si, pm, formula, err, ph, px, cm, ab))
        # 按formula分组，保留err最小的
        best = {}
        for item in all_items:
            f = item[2]
            if f not in best or item[3] < best[f][3]:
                best[f] = item
        # 重建各段ann
        new_segments = []
        for si, (m0, m1, sp, ann, sx, sy, sm) in enumerate(self.segments):
            new_ann = {}
            for pm, (matches, ph, px) in ann.items():
                kept = []
                for formula, cm, err, ab in matches:
                    if best.get(formula, (None,)*8)[1] == pm and best[formula][0] == si:
                        kept.append((formula, cm, err, ab))
                if kept:
                    new_ann[pm] = (kept, ph, px)
            new_segments.append((m0, m1, sp, new_ann, sx, sy, sm))
        self.segments = new_segments
        total_a = sum(len(a) for _, _, _, a, _, _, _ in self.segments)
        self._draw_segments()
        self._save_to_db()
        self.status_var.set(f"完成并保存：{len(self.segments)}段，{total_p}峰，{total_a}标注（全局去重）")

    def _save_to_db(self):
        """将标注结果序列化为JSON保存到数据库"""
        import json as _json
        data = {
            'seg_width': float(self.seg_width_var.get()),
            'tol': float(self.tol_var.get()),
            'charge': int(self.charge_var.get()),
            'min_abund': float(self.minabund_var.get()),
            'top_n': int(self.topn_var.get()),
            'elements': self.elem_var.get(),
            'max_counts': {e: int(v.get()) for e, v in self.count_vars.items()},
            'segments': []
        }
        for m0, m1, sp, ann, sx, sy, sm in self.segments:
            seg_data = {
                'm_start': float(m0), 'm_end': float(m1),
                'peaks': [{'x': float(r['x']), 'height': float(r['height'])}
                          for _, r in sp.iterrows()],
                'annotations': []
            }
            for pm, (matches, ph, px) in ann.items():
                ann_data = {
                    'peak_mass': float(pm), 'peak_x': float(px), 'peak_height': float(ph),
                    'matches': [{'formula': f, 'calc_mass': float(cm), 'err': float(err), 'abund': float(ab)}
                                for f, cm, err, ab in matches]
                }
                seg_data['annotations'].append(ann_data)
            data['segments'].append(seg_data)
        self.spec.hp_annotations = _json.dumps(data, ensure_ascii=False)
        self.main_app.db.update_spectrum(self.spec)

    def _load_from_db(self):
        """从数据库加载标注结果，返回是否成功"""
        import json as _json
        if not getattr(self.spec, 'hp_annotations', ''):
            return False
        try:
            data = _json.loads(self.spec.hp_annotations)
        except Exception:
            return False
        # 恢复控制面板参数
        self.seg_width_var.set(str(data.get('seg_width', 30)))
        self.tol_var.set(str(data.get('tol', 0.3)))
        self.charge_var.set(str(data.get('charge', 1)))
        self.minabund_var.set(str(data.get('min_abund', 0.5)))
        self.topn_var.set(str(data.get('top_n', 15)))
        self.elem_var.set(data.get('elements', ''))
        elements = [e.strip() for e in data.get('elements', '').replace('，', ',').split(',') if e.strip()]
        elements = [e for e in elements if e in ELEMENT_ISOTOPES]
        self._rebuild_counts(elements)
        for e, v in data.get('max_counts', {}).items():
            if e in self.count_vars:
                self.count_vars[e].set(str(v))
        # 重建 segments
        all_mass = self.main_app._current_to_mass(self.spec.x, self.spec)
        self.segments = []
        for sd in data.get('segments', []):
            m0, m1 = sd['m_start'], sd['m_end']
            mask = (all_mass >= m0) & (all_mass < m1)
            sx = self.spec.x[mask]; sy = self.spec.y[mask]; sm = all_mass[mask]
            sp = pd.DataFrame(sd['peaks'])
            ann = {}
            import re as _re2
            def _norm2(f):
                parts = f.split()
                def _mn2(p):
                    m2 = _re2.match(r'^(\d+)', p)
                    return int(m2.group(1)) if m2 else 0
                parts.sort(key=lambda x: (_mn2(x), x))
                return ' '.join(parts)
            for ad in sd['annotations']:
                matches = [(m['formula'], m['calc_mass'], m['err'], m['abund']) for m in ad['matches']]
                matches.sort(key=lambda x: x[2])
                seen = set(); uniq = []
                for item in matches:
                    nf = _norm2(item[0])
                    if nf not in seen:
                        seen.add(nf)
                        uniq.append((nf,) + item[1:])
                ann[ad['peak_mass']] = (uniq, ad['peak_height'], ad['peak_x'])
            self.segments.append((m0, m1, sp, ann, sx, sy, sm))
        return len(self.segments) > 0

    def _redraw(self):
        if self.segments:
            self._draw_segments()
        else:
            self._draw_overview()

    def _draw_overview(self):
        for w in self.plot_inner.winfo_children(): w.destroy()
        fig = Figure(figsize=(11, 4), dpi=100, facecolor=BG)
        ax = fig.add_subplot(111); ax.set_facecolor(BG)
        um = (self.hp_xaxis_mode.get() == 'mass')
        xd = self.main_app._current_to_mass(self.spec.x, self.spec) if um else self.spec.x
        xp, yp = downsample(xd, self.spec.y, 6000)
        ax.plot(xp, yp, color=self.spec.color, linewidth=0.7)
        ax.set_xlabel('Mass (m/z)' if um else 'Magnetic field current/A', fontsize=10)
        ax.set_ylabel('Intensity (nA)', fontsize=10)
        ax.set_title(f'{self.spec.name}（全谱概览，请设置元素后点"分段自动标注"）', fontsize=11)
        ax.grid(True, alpha=0.2); ax.tick_params(direction='in')
        fig.tight_layout()
        c = FigureCanvasTkAgg(fig, master=self.plot_inner)
        c.get_tk_widget().pack(fill=tk.X); c.draw()
        c.mpl_connect('motion_notify_event', self._on_hp_mouse_move)

    def _on_hp_mouse_move(self, event):
        if event.inaxes and event.xdata is not None and event.ydata is not None:
            um = (self.hp_xaxis_mode.get() == 'mass')
            xu = 'm/z' if um else 'A'
            self.hp_coord_var.set(f"X: {event.xdata:.4f} {xu}   Y: {event.ydata:.4f}")
        else:
            self.hp_coord_var.set("X: -    Y: -")

    def _draw_segments(self):
        for w in self.plot_inner.winfo_children(): w.destroy()
        if not self.segments: self._draw_overview(); return
        n = len(self.segments)
        fig = Figure(figsize=(11, 2.8*n), dpi=100, facecolor=BG)
        um = (self.hp_xaxis_mode.get() == 'mass')
        for idx, (m0, m1, sp, ann, sx, sy, sm) in enumerate(self.segments):
            ax = fig.add_subplot(n, 1, idx+1); ax.set_facecolor(BG)
            xd = sm if um else sx
            ax.plot(xd, sy, color=self.spec.color, linewidth=0.7)
            if ann:
                al = []
                for pm, (mt, ph, pc) in ann.items():
                    fx = pm if um else pc
                    tx = '\n'.join(formula_to_mathtext(sort_formula_by_mass(f)) for f, _, _, _ in mt)
                    al.append((fx, ph, tx))
                al.sort(key=lambda t: t[0])
                xr = (m1 - m0) if um else (float(xd.max()) - float(xd.min()))
                # 交替左右排布，拥挤时增加垂直偏移
                lv = [8, 22, 36, 50]
                for i, (fx, fy, tx) in enumerate(al):
                    # 检测与前一个峰的距离，决定垂直偏移
                    voff = lv[0]
                    if i > 0:
                        prev_fx = al[i-1][0]
                        dist = fx - prev_fx
                        if dist < xr * 0.03:
                            voff = lv[min(i % len(lv), len(lv)-1)]
                    # 奇数峰右偏，偶数峰左偏，交替排布
                    if i % 2 == 0:
                        ha = 'left'
                        xoff = 4
                    else:
                        ha = 'right'
                        xoff = -4
                    ax.annotate(tx, xy=(fx, fy), xytext=(xoff, voff),
                        textcoords='offset points', fontsize=7, ha=ha, va='bottom',
                        color='#C00000', fontweight='bold')
            if um: ax.set_xlim(m0, m1)
            ax.set_ylabel('Intensity', fontsize=8)
            ax.set_title(f'{m0:.0f}-{m1:.0f} u  ({len(sp)}峰, {len(ann)}标注)', fontsize=9, loc='left')
            ax.grid(True, alpha=0.15); ax.tick_params(direction='in', labelsize=8)
            ax.set_xlabel('Mass (m/z)' if um else 'Magnetic field current/A', fontsize=8)
            ax.tick_params(axis='x', labelsize=7)
        fig.tight_layout()
        c = FigureCanvasTkAgg(fig, master=self.plot_inner)
        c.get_tk_widget().pack(fill=tk.X); c.draw()
        c.mpl_connect('motion_notify_event', self._on_hp_mouse_move)
        self._current_fig = fig

    def _export_image(self):
        if not self.segments:
            messagebox.showinfo('提示', '请先执行分段标注。'); return
        path = filedialog.asksaveasfilename(title="导出标注长图", defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("PDF", "*.pdf")])
        if not path: return
        self._current_fig.savefig(path, dpi=300, bbox_inches='tight', facecolor=BG)
        self.status_var.set(f"已导出：{path}")
        messagebox.showinfo("成功", f"长图已保存到:\n{path}")

# ============================================================
def main():
    try:
        root = tk.Tk()
        app = MassSpectrumAppPro(root)
        app.run()
    except Exception as e:
        import traceback
        err_msg = traceback.format_exc()
        # 写日志文件
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   'mass_spec_error.log'), 'w', encoding='utf-8') as f:
                f.write(err_msg)
        except Exception:
            pass
        # 尝试弹窗显示
        try:
            import tkinter as _tk
            from tkinter import messagebox as _mb
            _r = _tk.Tk()
            _r.withdraw()
            _mb.showerror('程序启动失败',
                          f'错误信息：{e}\n\n详细日志已保存到 mass_spec_error.log\n\n'
                          f'常见原因：\n1. 缺少依赖库（pip install matplotlib pandas scipy numpy openpyxl）\n'
                          f'2. 数据库文件损坏（删除 mass_spectrum_db.sqlite 重试）')
            _r.destroy()
        except Exception:
            print(err_msg)


if __name__ == '__main__':
    main()
