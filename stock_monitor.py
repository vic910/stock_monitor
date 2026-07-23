"""
股票监控工具 stock_monitor.py
================================
架构概览：
  - search_secid()        : 东方财富搜索接口，code → (secid, name)，结果内存缓存
  - _fetch_kline_tencent(): 腾讯K线接口（风控松，优先用），覆盖 A股/ETF/港股/恒指
  - _fetch_kline_eastmoney(): 东方财富K线接口（secid直查，全品种但风控严），仅作期指等兜底
  - fetch_stock_data()    : 组合上面两个K线源 + 实时价，返回最新价/涨跌幅/MA5-60/成交量/收盘价
  - fetch_realtime_quote(): 腾讯实时行情接口，盘中拿高精度当天价/涨跌幅（取不到则降级）
  - FetchWorker           : QThread，遍历所有code依次拉数据，通过signal推给主线程
  - FloatWidget           : 无边框置顶浮窗，支持任意数量股票，每只一行(价格+涨跌幅)
  - MainWindow            : 主窗口，表格+定时刷新+右键菜单+托盘

数据接口（如接口挂了看这里换）：
  - 代码搜索: https://searchapi.eastmoney.com/api/suggest/get  (东方财富，任意代码 → secid)
  - K线历史(主): https://web.ifzq.gtimg.cn/appstock/app/fqkline/get  (腾讯，前复权日K，风控松)
  - K线历史(兜底): https://push2his.eastmoney.com/api/qt/stock/kline/get  (东方财富，secid直查，覆盖 A50 期指等)
    腾讯取不到才用东财——东财 push2his 高频请求易触发限流，只留给期指等特殊品种
  - 实时行情: https://qt.gtimg.cn/q={前缀+代码}  (腾讯，盘中高精度价，期指等不覆盖时降级用K线收盘价)
  - 微信推送: https://sctapi.ftqq.com/{key}.send  (Server酱，_push_weixin 已封装但当前未接入)

表格各列的计算规则（在 MainWindow._on_result 里）：
  - 趋势(算法4): 均线位置(40%) + MA20斜率(30%) + 价格结构(30%) 加权评分，5档强弱
  - 均线状态   : 股价与 MA5/10/20/30 的上下方位置关系
  - 活跃度(N/M): N日均量 / M日均量，N、M 可在界面设置
  - 趋势做T策略 : 股价 ≥ MA10 → 积极买进，< MA10 → 积极卖出（自动）
  - 我的做T策略 : 下拉框手动选择（3档策略，带字体色），按股票持久化到 config.t_strategy

扩展方向：
  - 微信/风险推送: 实现风险判断 → _last_push 限流 → _push_weixin（建议放子线程）
  - 更多技术指标（MACD/RSI）: 在 fetch_stock_data() 里用 closes 列表扩展计算
  - 成本价/盈亏: config 里加 cost_prices 字段，_on_result 里加列展示
  - K线图: 双击行弹窗，用 matplotlib 画 closes 折线
"""

import sys
import os
import json
import time
import ctypes
import urllib.request
import urllib.parse
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QSpinBox, QMenu, QSystemTrayIcon, QColorDialog,
    QComboBox
)
from PyQt6.QtCore import QThread, pyqtSignal, QTimer, Qt, QPoint, QEvent
from PyQt6.QtGui import QColor, QFont, QCursor, QIcon, QPixmap, QPainter, QPen

# 打包后用 exe 所在目录，开发时用脚本所在目录
if getattr(sys, 'frozen', False):
    _BASE_DIR = os.path.dirname(sys.executable)
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_BASE_DIR, "stock_monitor_config.json")


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {"stocks": [], "interval": 10, "server_key": ""}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=4)


# 进程级缓存，避免每次刷新都重复搜索 secid
_secid_cache = {}   # code → (secid, name)

# “我的做T策略”下拉选项：(显示文本, 字体颜色)，索引即持久化到 config 的值
T_STRATEGY_OPTIONS = [
    ("", "#000000"),                              # 0 空（默认）
    ("开盘或回踩买进（强势）", "#e74c3c"),        # 1 红
    ("拉高卖出下跌买入（震荡分歧）", "#000000"),   # 2 黑
    ("开盘或拉高卖出（弱势）", "#27ae60"),        # 3 绿
]


def search_secid(code):
    """东方财富搜索接口：任意代码 → (secid, name)
    secid 格式示例：'0.159995'（深市），'1.600519'（沪市），'100.XIN9'（境外）
    """
    if code in _secid_cache:
        return _secid_cache[code]
    url = (
        "https://searchapi.eastmoney.com/api/suggest/get"
        f"?input={urllib.parse.quote(code)}&type=14&token=D43BF722C8E33BDC906FB84D85E326E8"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.eastmoney.com/",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    items = (data.get("QuotationCodeTable") or {}).get("Data") or []
    if not items:
        raise Exception(f"找不到代码: {code}")
    secid = f"{items[0]['MktNum']}.{items[0]['Code']}"
    name = items[0].get("Name", code)
    _secid_cache[code] = (secid, name)
    return secid, name


def _get_tencent_prefix(secid):
    """secid → 腾讯接口前缀，支持 A股(0/1)、港股(116)、恒生指数(100)"""
    mkt, code = secid.split(".", 1)
    if mkt == "1":
        return f"sh{code}"
    elif mkt in ("116", "100"):
        return f"hk{code}"
    else:
        return f"sz{code}"


def fetch_realtime_quote(tc):
    """腾讯实时行情接口：tc(如 sz159995) → (price, change_pct)，取不到返回 None

    盘中 K线接口当天那根收盘价只有 2 位精度（如 1.26），实时接口给 3 位（1.265），
    且涨跌幅用真实昨收计算。期指等特殊品种此接口不覆盖，返回 None 由调用方降级。
    字段(~分隔): 索引3=当前价, 索引4=昨收, 索引32=涨跌幅
    """
    try:
        url = f"https://qt.gtimg.cn/q={tc}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://gu.qq.com/",
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("gbk", errors="ignore")
        if '="' not in raw:
            return None
        body = raw.split('="', 1)[1].rsplit('";', 1)[0]
        f = body.split("~")
        if len(f) <= 32 or not f[3]:
            return None
        price = float(f[3])
        change_pct = float(f[32]) if f[32] else 0.0
        return price, change_pct
    except Exception:
        return None


def _fetch_kline_tencent(tc):
    """腾讯K线接口：tc(如 sz159995) → (closes, volumes)，风控较松，覆盖 A股/ETF/港股/恒指。
    境外期指(如 A50)不覆盖，返回 None 由调用方转东财兜底。
    K线格式(数组): [日期, 开, 收, 高, 低, 成交量, ...]，收盘索引2，成交量索引5
    """
    url = (
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?_var=kline_day&param={tc},day,,,80,qfq"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Referer": "https://gu.qq.com/",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw[raw.index("=") + 1:])
    sd = (data.get("data") or {}).get(tc, {})
    klines = sd.get("day") or sd.get("qfqday") or []
    if not klines:
        return None
    closes = [float(k[2]) for k in klines]
    volumes = []
    for k in klines:
        try:
            volumes.append(float(k[5]))
        except (IndexError, ValueError, TypeError):
            volumes.append(0.0)
    return closes, volumes


def _fetch_kline_eastmoney(secid):
    """东方财富K线接口：secid 直查 → (closes, volumes, name)，全品种覆盖但风控严，仅作兜底。
    K线格式(逗号分隔): 日期,开,收,高,低,成交量,成交额，收盘索引2，成交量索引5
    """
    url = (
        f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57"
        f"&klt=101&fqt=1&end=20500101&lmt=80"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Referer": "https://www.eastmoney.com/",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw).get("data") or {}
    klines = data.get("klines") or []
    if not klines:
        return None
    rows = [k.split(",") for k in klines]
    closes = [float(r[2]) for r in rows]
    volumes = []
    for r in rows:
        try:
            volumes.append(float(r[5]))
        except (IndexError, ValueError, TypeError):
            volumes.append(0.0)
    return closes, volumes, data.get("name")


# 板块总成交额统计用的三只指数腾讯前缀：上证指数 + 深证成指 + 创业板指
_MARKET_INDEX_TC = ["sh000001", "sz399001", "sz399006"]


def _fetch_index_amount_series(tc, lmt=70):
    """腾讯 newfqkline 接口拉单只指数近 lmt 天成交额，返回 {日期: 成交额(元)}。失败返回 {}。
    该接口带成交额字段（索引8，单位万元），且腾讯不限流。
    K线格式: [日期, 开, 收, 高, 低, 成交量, {}, 涨跌幅, 成交额(万元), ...]
    """
    try:
        url = (
            f"https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get"
            f"?_var=k&param={tc},day,,,{lmt},qfq"
        )
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://gu.qq.com/",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw[raw.index("=") + 1:])
        sd = (data.get("data") or {}).get(tc, {})
        klines = sd.get("qfqday") or sd.get("day") or []
        out = {}
        for k in klines:
            try:
                out[k[0]] = float(k[8]) * 1e4   # 万元 → 元
            except (IndexError, ValueError, TypeError):
                pass
        return out
    except Exception:
        return {}


def fetch_market_amounts(lmt=70):
    """腾讯 newfqkline 拉三只指数近 lmt 天成交额，按日期对齐相加，
    返回 [(日期, 总成交额元), ...] 升序。只保留三只都有数据的交易日。
    腾讯接口不限流，一次即含完整历史+当日，任一只失败返回 []。
    """
    series = [_fetch_index_amount_series(tc, lmt) for tc in _MARKET_INDEX_TC]
    if any(not s for s in series):
        return []
    common_dates = set(series[0])
    for s in series[1:]:
        common_dates &= set(s)
    return [(d, sum(s[d] for s in series)) for d in sorted(common_dates)]


def _fetch_index_amount_today(tc):
    """腾讯实时接口取单只指数当日成交额（元）。字段37单位万元。失败返回 None。"""
    try:
        url = f"https://qt.gtimg.cn/q={tc}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://gu.qq.com/",
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("gbk", errors="ignore")
        if '="' not in raw:
            return None
        f = raw.split('="', 1)[1].rsplit('";', 1)[0].split("~")
        if len(f) <= 37 or not f[37]:
            return None
        return float(f[37]) * 1e4   # 万元 → 元
    except Exception:
        return None


def fetch_market_amount_today():
    """腾讯实时取三只指数当日成交额之和 (日期, 总额元)。任一失败返回 None。"""
    total = 0.0
    for tc in _MARKET_INDEX_TC:
        a = _fetch_index_amount_today(tc)
        if a is None:
            return None
        total += a
    return time.strftime("%Y-%m-%d"), total


def fetch_stock_data(code):
    """拉取单只股票数据，返回 (name, price, change_pct, ma5, ma10, ma20, ma30, ma60, volumes, closes)

    K线来源：优先腾讯（风控松，覆盖 A股/ETF/港股/恒指），腾讯取不到再降级东方财富
    （secid 直查，覆盖 A50 等境外期指）。避免东财 push2his 高频请求触发限流。

    价格精度：均线/趋势/成交量全部基于 K线收盘价计算；当天最新价和涨跌幅优先取
    腾讯实时行情接口（高精度），取不到时降级用 K线收盘价（期指等特殊品种走此分支）。
    """
    secid, name = search_secid(code)
    tc = _get_tencent_prefix(secid)

    result = _fetch_kline_tencent(tc)     # 优先腾讯
    if result is not None:
        closes, volumes = result
    else:
        em = _fetch_kline_eastmoney(secid)   # 期指等降级东财
        if em is None:
            raise Exception("无K线数据（代码有误或停牌）")
        closes, volumes, em_name = em
        if em_name:
            name = em_name

    current = closes[-1]
    prev = closes[-2] if len(closes) > 1 else current
    change_pct = (current - prev) / prev * 100 if prev else 0

    # 用腾讯实时行情覆盖当天最新价/涨跌幅（高精度），并同步 closes[-1] 使均线口径一致；
    # 取不到（如期指，腾讯实时接口不覆盖）则保持 K线收盘价不变
    rt = fetch_realtime_quote(tc)
    if rt is not None:
        current, change_pct = rt
        closes[-1] = current

    def ma(n):
        return sum(closes[-n:]) / min(n, len(closes))

    return name, current, change_pct, ma(5), ma(10), ma(20), ma(30), ma(60), volumes, closes


class AmountChartWidget(QWidget):
    """两市+创业板总成交额可视化（QPainter 手绘）：
    左侧最近 5 日柱状图（柱顶标数值、柱下标日期），右侧 5/10/20/30/60 日均值文字。
    数据由 set_data([(日期, 总成交额元), ...] 升序) 传入，单位显示为“亿”。
    """

    def __init__(self):
        super().__init__()
        self._data = []          # [(date, amount_yuan), ...] 升序
        self.setMinimumHeight(120)
        self.setToolTip("上证指数 + 深证成指 + 创业板指 每日成交额之和\n左：最近5日柱状图  右：5/10/20/30/60日均值")

    def set_data(self, series):
        self._data = series or []
        self.update()

    def _avg(self, n):
        """最近 n 日总成交额均值（元），数据不足则按现有天数"""
        if not self._data:
            return 0.0
        vals = [a for _, a in self._data[-n:]]
        return sum(vals) / len(vals) if vals else 0.0

    @staticmethod
    def _fmt_yi(amount_yuan):
        """元 → “xxxx亿”"""
        return f"{amount_yuan / 1e8:.0f}亿"

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        # 背景
        painter.fillRect(0, 0, w, h, QColor("#fafafa"))

        if not self._data:
            painter.setPen(QColor("#999999"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "成交额数据加载中…")
            painter.end()
            return

        # 标题
        title_font = QFont(); title_font.setPointSize(9); title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor("#333333"))
        painter.drawText(8, 16, "两市+创业板 总成交额")

        # ── 左侧：最近 5 日柱状图 ──────────────────────
        recent = self._data[-5:]
        chart_left, chart_top = 12, 26
        chart_w = int(w * 0.52)
        chart_bottom = h - 20
        chart_h = chart_bottom - chart_top
        if recent and chart_h > 10:
            max_amt = max(a for _, a in recent) or 1.0
            n = len(recent)
            slot = chart_w / n
            bar_w = min(slot * 0.6, 48)
            small_font = QFont(); small_font.setPointSize(7)
            for i, (date, amt) in enumerate(recent):
                bar_h = int(chart_h * (amt / max_amt))
                x = int(chart_left + slot * i + (slot - bar_w) / 2)
                y = chart_bottom - bar_h
                painter.fillRect(x, y, int(bar_w), bar_h, QColor("#e74c3c"))
                # 柱顶数值
                painter.setFont(small_font)
                painter.setPen(QColor("#333333"))
                painter.drawText(x - 6, y - 3, int(bar_w) + 12, 12,
                                 Qt.AlignmentFlag.AlignHCenter, self._fmt_yi(amt))
                # 柱下日期（MM-DD）
                painter.setPen(QColor("#888888"))
                md = date[5:] if len(date) >= 10 else date
                painter.drawText(x - 6, chart_bottom + 2, int(bar_w) + 12, 14,
                                 Qt.AlignmentFlag.AlignHCenter, md)

        # ── 右侧：均值文字 ──────────────────────────────
        avg_left = int(w * 0.58)
        label_font = QFont(); label_font.setPointSize(9)
        painter.setFont(label_font)
        rows = [("5日均量", 5), ("10日均量", 10), ("20日均量", 20),
                ("30日均量", 30), ("60日均量", 60)]
        row_h = 18
        y0 = 30
        for i, (label, n) in enumerate(rows):
            y = y0 + i * row_h
            painter.setPen(QColor("#666666"))
            painter.drawText(avg_left, y, y0 + 200, 16,
                             Qt.AlignmentFlag.AlignLeft, f"{label}:")
            painter.setPen(QColor("#c0392b"))
            painter.drawText(avg_left + 70, y, 120, 16,
                             Qt.AlignmentFlag.AlignLeft, self._fmt_yi(self._avg(n)))
        painter.end()


class FloatWidget(QWidget):
    """常驻最顶层浮窗，支持任意数量股票，每行：价格  涨跌幅
    内部状态：
      _codes  : 有序列表，决定行的显示顺序
      _data   : code → (price, change_pct)，用于重建行时恢复数据
      _rows   : code → (price_lbl, chg_lbl)，当前显示的 Label 引用
    """

    color_changed = pyqtSignal(str)
    font_color_changed = pyqtSignal(str)
    cleared = pyqtSignal()

    def __init__(self, bg_color="#2c3e50", font_color="#ffffff"):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._drag_pos = None
        self._codes = []
        self._data = {}
        self._rows = {}

        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(8, 6, 8, 6)
        self._main_layout.setSpacing(3)

        self._bg_color = bg_color
        self._font_color = font_color
        self._update_style()

    def showEvent(self, event):
        super().showEvent(event)
        # 设置 WS_EX_TOOLWINDOW：不在任务栏显示，不随主窗口最小化
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        hwnd = int(self.winId())
        cur = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, cur | WS_EX_TOOLWINDOW)

    def _make_font(self, size, bold=False):
        f = QFont()
        f.setPointSize(size)
        f.setBold(bold)
        return f

    def _update_style(self):
        self.setStyleSheet(f"""
            FloatWidget {{
                background-color: {self._bg_color};
                border-radius: 7px;
            }}
            QLabel {{
                color: {self._font_color};
                background: transparent;
            }}
        """)

    def _rebuild_rows(self):
        """重建所有行的 Label，按 _codes 顺序，并恢复已有数据"""
        while self._main_layout.count():
            item = self._main_layout.takeAt(0)
            if item.layout():
                while item.layout().count():
                    w = item.layout().takeAt(0).widget()
                    if w:
                        w.deleteLater()
        self._rows.clear()

        for code in self._codes:
            row = QHBoxLayout()
            row.setSpacing(6)
            price_lbl = QLabel("--")
            price_lbl.setFont(self._make_font(9))
            price_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            price_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            chg_lbl = QLabel("--")
            chg_lbl.setFont(self._make_font(9))
            chg_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            chg_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            row.addWidget(price_lbl)
            row.addWidget(chg_lbl)
            self._main_layout.addLayout(row)
            self._rows[code] = (price_lbl, chg_lbl)
            if code in self._data:
                self._apply_data(code, *self._data[code])

        self.adjustSize()

    def _apply_data(self, code, price, change_pct):
        """把数据写入对应行的 Label（价格 + 涨跌幅）"""
        if code not in self._rows:
            return
        price_lbl, chg_lbl = self._rows[code]
        price_lbl.setText(f"{price:.4f}")
        fc = self._font_color
        if change_pct > 0:
            chg_lbl.setText(f"{change_pct:+.2f}%")
        elif change_pct < 0:
            chg_lbl.setText(f"{change_pct:.2f}%")
        else:
            chg_lbl.setText(f"{change_pct:.2f}%")
        chg_lbl.setStyleSheet(f"color: {fc}; background: transparent;")


    def add_stock(self, code):
        if code in self._codes:
            return
        self._codes.append(code)
        self._rebuild_rows()

    def remove_stock(self, code):
        if code not in self._codes:
            return
        self._codes.remove(code)
        self._data.pop(code, None)
        self._rebuild_rows()

    def update_stock(self, code, price, change_pct):
        self._data[code] = (price, change_pct)
        self._apply_data(code, price, change_pct)
        self.adjustSize()

    def has_stock(self, code):
        return code in self._codes

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        elif event.button() == Qt.MouseButton.RightButton:
            menu = QMenu(self)
            act_color = menu.addAction("修改背景色...")
            act_font_color = menu.addAction("修改字体颜色...")
            act_close = menu.addAction("关闭浮窗")
            act = menu.exec(QCursor.pos())
            if act == act_color:
                color = QColorDialog.getColor(QColor(self._bg_color), self, "选择浮窗背景色")
                if color.isValid():
                    self._bg_color = color.name()
                    self._update_style()
                    self.color_changed.emit(self._bg_color)
            elif act == act_font_color:
                color = QColorDialog.getColor(QColor(self._font_color), self, "选择浮窗字体颜色")
                if color.isValid():
                    self._font_color = color.name()
                    self._update_style()
                    self.font_color_changed.emit(self._font_color)
            elif act == act_close:
                self._codes.clear()
                self._data.clear()
                self._rebuild_rows()
                self.cleared.emit()
                self.hide()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None


class FetchWorker(QThread):
    """后台线程，依次拉取每只股票数据，通过 signal 通知主线程更新 UI
    result signal: (code, name, price, change_pct, ma5, ma10, ma20, ma30, ma60, vols_json, closes_json)
    error  signal: (code, error_msg)
    """
    result = pyqtSignal(str, str, float, float, float, float, float, float, float, str, str)
    error = pyqtSignal(str, str)

    def __init__(self, codes):
        super().__init__()
        self.codes = list(codes)

    def run(self):
        for code in self.codes:
            try:
                name, price, change_pct, ma5, ma10, ma20, ma30, ma60, volumes, closes = fetch_stock_data(code)
                self.result.emit(code, name, price, change_pct, ma5, ma10, ma20, ma30, ma60,
                                 json.dumps(volumes), json.dumps(closes))
            except Exception as e:
                self.error.emit(code, str(e))


class IndexHistoryWorker(QThread):
    """后台线程，拉三只指数完整历史成交额序列（腾讯 newfqkline），启动时用一次。
    result signal: series_json ([[日期, 总成交额元], ...] 的 JSON)
    """
    result = pyqtSignal(str)

    def run(self):
        try:
            series = fetch_market_amounts()
        except Exception:
            series = []
        self.result.emit(json.dumps(series))


class IndexTodayWorker(QThread):
    """后台线程，只拉当日总成交额（腾讯实时接口，秒回），跟随刷新间隔用。
    result signal: (日期, 总成交额元)；失败发 ("", -1)
    """
    result = pyqtSignal(str, float)

    def run(self):
        try:
            r = fetch_market_amount_today()
        except Exception:
            r = None
        if r is None:
            self.result.emit("", -1.0)
        else:
            self.result.emit(r[0], r[1])


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("股票监控工具")
        self.resize(960, 520)
        self.config = load_config()
        self.worker = None
        self._hist_worker = None       # 历史成交额（启动拉一次）
        self._today_worker = None      # 当日成交额（跟随刷新间隔）
        self._amount_series = []       # 内存中的总成交额序列 [(日期, 元), ...] 升序
        self._float_win = FloatWidget(bg_color=self.config.get("float_bg", "#2c3e50"), font_color=self.config.get("float_font_color", "#ffffff"))
        self._float_win.color_changed.connect(self._on_float_color_changed)
        self._float_win.font_color_changed.connect(self._on_float_font_color_changed)
        self._build_ui()
        self._build_tray()
        self._start_timer()
        self._refresh()                  # 首次加载股票数据
        self._load_amount_history()      # 启动拉一次 60 日历史成交额

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        # 顶部工具栏
        top = QHBoxLayout()
        top.addWidget(QLabel("股票代码:"))
        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText("股票/ETF/期指代码，如 600519、CN00Y")
        self.code_input.setFixedWidth(200)
        self.code_input.returnPressed.connect(self._add_stock)
        top.addWidget(self.code_input)
        btn_add = QPushButton("添加")
        btn_add.setFixedWidth(60)
        btn_add.clicked.connect(self._add_stock)
        top.addWidget(btn_add)
        btn_del = QPushButton("删除选中")
        btn_del.setFixedWidth(80)
        btn_del.clicked.connect(self._del_stock)
        top.addWidget(btn_del)
        top.addStretch()
        top.addWidget(QLabel("活跃度 N:"))
        self.vol_n_spin = QSpinBox()
        self.vol_n_spin.setRange(1, 60)
        self.vol_n_spin.setValue(self.config.get("vol_n", 5))
        self.vol_n_spin.setFixedWidth(70)
        self.vol_n_spin.setKeyboardTracking(False)
        self.vol_n_spin.valueChanged.connect(self._on_vol_param_changed)
        top.addWidget(self.vol_n_spin)
        top.addWidget(QLabel("日 /"))
        self.vol_m_spin = QSpinBox()
        self.vol_m_spin.setRange(1, 60)
        self.vol_m_spin.setValue(self.config.get("vol_m", 20))
        self.vol_m_spin.setFixedWidth(70)
        self.vol_m_spin.setKeyboardTracking(False)
        self.vol_m_spin.valueChanged.connect(self._on_vol_param_changed)
        top.addWidget(self.vol_m_spin)
        top.addWidget(QLabel("日"))
        top.addSpacing(12)
        top.addWidget(QLabel("自动刷新(秒):"))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(5, 3600)
        self.interval_spin.setValue(self.config.get("interval", 10))
        self.interval_spin.setFixedWidth(80)
        self.interval_spin.setKeyboardTracking(False)
        self.interval_spin.valueChanged.connect(self._on_interval_changed)
        top.addWidget(self.interval_spin)
        self.refresh_btn = QPushButton("立即刷新")
        self.refresh_btn.setFixedWidth(80)
        self.refresh_btn.clicked.connect(self._refresh)
        top.addWidget(self.refresh_btn)
        layout.addLayout(top)

        # 数据表格，列：代码/名称/最新价/涨跌幅/均线状态/趋势/活跃度/趋势做T策略/我的做T策略/排序
        self.table = QTableWidget()
        self.table.setColumnCount(10)
        self.table.setHorizontalHeaderLabels(["代码", "名称", "最新价", "涨跌幅", "均线状态", "趋势", "活跃度(N/M)", "趋势做T策略", "我的做T策略", "排序"])
        h = self.table.horizontalHeader()
        for i in range(10):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        tooltips = {
            5: "趋势（算法4）：综合均线位置(40%)、MA20斜率(30%)、价格结构(30%)加权评分\n强势↑ ≥0.85 | 偏多↗ 0.60~0.85 | 震荡→ 0.40~0.60 | 偏空↘ 0.15~0.40 | 弱势↓ <0.15",
            6: "活跃度 = N日均量 / M日均量\n≥2.0x 明显放量(红) | 1.2~2.0x 轻微放量(橙) | 1.0~1.2x 正常(灰) | <1.0x 缩量(绿)",
            7: "趋势做T策略（自动）：股价 ≥ MA10 → 积极买进(红)\n股价 < MA10 → 积极卖出(绿)",
            8: "我的做T策略：手动下拉选择\n开盘回踩买进(强势,红) | 拉高卖出下跌买入(震荡分歧,黑) | 开盘拉高卖出(弱势,绿)",
        }
        for col, tip in tooltips.items():
            item = self.table.horizontalHeaderItem(col)
            if item:
                item.setToolTip(tip)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_context_menu)
        layout.addWidget(self.table)

        # 成交额图表（两市+创业板总成交额）
        self.amount_chart = AmountChartWidget()
        layout.addWidget(self.amount_chart)

        # 底部：微信推送Key + 状态栏
        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("微信推送Key(Server酱，可选):"))
        self.key_input = QLineEdit()
        self.key_input.setPlaceholderText("SCT...（预留：推送功能待实现）")
        self.key_input.setText(self.config.get("server_key", ""))
        self.key_input.setFixedWidth(260)
        self.key_input.textChanged.connect(self._save_server_key)
        bottom.addWidget(self.key_input)
        bottom.addStretch()
        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet("color: gray; font-size: 11px;")
        bottom.addWidget(self.status_label)
        layout.addLayout(bottom)

        for code in self.config.get("stocks", []):
            self._insert_row(code)

    def _build_tray(self):
        # 用纯色小图标（绿色方块）作为托盘图标，无需外部图片
        pix = QPixmap(16, 16)
        pix.fill(QColor("#27ae60"))
        icon = QIcon(pix)

        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("股票监控工具")

        menu = QMenu()
        act_show = menu.addAction("显示主窗口")
        act_quit = menu.addAction("退出")
        act_show.triggered.connect(self._tray_show)
        act_quit.triggered.connect(QApplication.quit)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()

    def _tray_show(self):
        self.showNormal()
        self.activateWindow()
        # 恢复窗口时立即刷新一次（暂停期间数据可能已过期）
        self._refresh()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._tray_show()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            self.hide()

    def closeEvent(self, event):
        QApplication.quit()

    def _insert_row(self, code):
        r = self.table.rowCount()
        self.table.insertRow(r)
        # 文本列 0~7（代码+6个数据列+趋势做T策略），第8列下拉框，第9列排序按钮
        for c, text in enumerate([code, "--", "--", "--", "--", "--", "--", "--"]):
            self.table.setItem(r, c, QTableWidgetItem(text))
        self._set_t_strategy_combo(r, code)
        self._set_sort_buttons(r)

    def _set_t_strategy_combo(self, row, code):
        """第8列“我的做T策略”下拉框，选项文本带对应字体色，选择后按股票代码持久化"""
        combo = QComboBox()
        for text, _color in T_STRATEGY_OPTIONS:
            combo.addItem(text)
        saved = self.config.get("t_strategy", {}).get(code, 0)
        if 0 <= saved < len(T_STRATEGY_OPTIONS):
            combo.setCurrentIndex(saved)
        self._apply_combo_color(combo, combo.currentIndex())
        combo.currentIndexChanged.connect(
            lambda idx, c=combo: self._on_t_strategy_changed(c, idx)
        )
        self.table.setCellWidget(row, 8, combo)

    def _apply_combo_color(self, combo, idx):
        """把下拉框当前选项的字体色应用到显示"""
        if 0 <= idx < len(T_STRATEGY_OPTIONS):
            color = T_STRATEGY_OPTIONS[idx][1]
            combo.setStyleSheet(f"QComboBox {{ color: {color}; }}")

    def _on_t_strategy_changed(self, combo, idx):
        self._apply_combo_color(combo, idx)
        # 定位该下拉框所在行的股票代码并持久化
        for r in range(self.table.rowCount()):
            if self.table.cellWidget(r, 8) is combo:
                code = self.table.item(r, 0).text()
                self.config.setdefault("t_strategy", {})[code] = idx
                save_config(self.config)
                break

    def _forget_t_strategy(self, code):
        """删除股票时清理其“我的做T策略”持久化，避免残留"""
        t_map = self.config.get("t_strategy")
        if t_map and code in t_map:
            del t_map[code]

    def _set_sort_buttons(self, row):
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(2, 1, 2, 1)
        lay.setSpacing(2)
        btn_up = QPushButton("↑")
        btn_dn = QPushButton("↓")
        btn_up.setFixedSize(24, 20)
        btn_dn.setFixedSize(24, 20)
        btn_up.clicked.connect(lambda: self._move_row(self._widget_row(w), -1))
        btn_dn.clicked.connect(lambda: self._move_row(self._widget_row(w), 1))
        btn_drag = QPushButton("⠿")
        btn_drag.setFixedSize(20, 20)
        btn_drag.setToolTip("长按拖拽调整顺序")
        btn_drag.setCursor(Qt.CursorShape.OpenHandCursor)
        btn_drag.setStyleSheet("font-size:11px;")
        # 拖拽状态
        btn_drag._drag_start_pos = None
        btn_drag._drag_active = False
        btn_drag._drag_indicator = None

        def on_press(event, _w=w):
            if event.button() == Qt.MouseButton.LeftButton:
                btn_drag._drag_start_pos = event.globalPosition().toPoint()
                btn_drag._drag_active = False
                # 长按300ms后激活拖拽
                btn_drag._press_timer = QTimer()
                btn_drag._press_timer.setSingleShot(True)
                btn_drag._press_timer.timeout.connect(lambda: setattr(btn_drag, '_drag_active', True))
                btn_drag._press_timer.start(300)

        def on_move(event, _w=w):
            if not (event.buttons() & Qt.MouseButton.LeftButton):
                return
            if not btn_drag._drag_active:
                return
            btn_drag.setCursor(Qt.CursorShape.ClosedHandCursor)
            gpos = event.globalPosition().toPoint()
            tpos = self.table.viewport().mapFromGlobal(gpos)
            target_row = self.table.rowAt(tpos.y())
            # 绘制插入线指示
            if btn_drag._drag_indicator is None:
                btn_drag._drag_indicator = QWidget(self.table.viewport())
                btn_drag._drag_indicator.setStyleSheet("background: #e74c3c;")
                btn_drag._drag_indicator.setFixedHeight(2)
                btn_drag._drag_indicator.show()
            ind = btn_drag._drag_indicator
            if target_row >= 0:
                rect = self.table.visualItemRect(self.table.item(target_row, 0))
                ind.setGeometry(0, rect.top(), self.table.viewport().width(), 2)
            else:
                n = self.table.rowCount()
                if n > 0:
                    rect = self.table.visualItemRect(self.table.item(n - 1, 0))
                    ind.setGeometry(0, rect.bottom(), self.table.viewport().width(), 2)

        def on_release(event, _w=w):
            if hasattr(btn_drag, '_press_timer'):
                btn_drag._press_timer.stop()
            btn_drag.setCursor(Qt.CursorShape.OpenHandCursor)
            if btn_drag._drag_indicator:
                btn_drag._drag_indicator.deleteLater()
                btn_drag._drag_indicator = None
            if not btn_drag._drag_active:
                btn_drag._drag_active = False
                return
            btn_drag._drag_active = False
            gpos = event.globalPosition().toPoint()
            tpos = self.table.viewport().mapFromGlobal(gpos)
            src_row = self._widget_row(_w)
            target_row = self.table.rowAt(tpos.y())
            if target_row < 0:
                target_row = self.table.rowCount() - 1
            if src_row >= 0 and target_row != src_row:
                self._drag_move_row(src_row, target_row)

        btn_drag.mousePressEvent = on_press
        btn_drag.mouseMoveEvent = on_move
        btn_drag.mouseReleaseEvent = on_release

        lay.addWidget(btn_up)
        lay.addWidget(btn_dn)
        lay.addWidget(btn_drag)
        self.table.setCellWidget(row, 9, w)

    def _widget_row(self, widget):
        for r in range(self.table.rowCount()):
            if self.table.cellWidget(r, 9) == widget:
                return r
        return -1

    def _move_row(self, row, direction):
        target = row + direction
        if target < 0 or target >= self.table.rowCount():
            return
        # 交换文本列（0~7）
        for c in range(8):
            a = self.table.takeItem(row, c)
            b = self.table.takeItem(target, c)
            self.table.setItem(row, c, b)
            self.table.setItem(target, c, a)
        # 交换“我的做T策略”下拉框的选择（第8列是 cellWidget，不能 takeItem）
        cb_a = self.table.cellWidget(row, 8)
        cb_b = self.table.cellWidget(target, 8)
        if cb_a is not None and cb_b is not None:
            ia, ib = cb_a.currentIndex(), cb_b.currentIndex()
            cb_a.setCurrentIndex(ib)
            cb_b.setCurrentIndex(ia)
        self.table.setCurrentCell(target, 0)
        self._save_stocks()

    def _drag_move_row(self, src, dst):
        if src == dst:
            return
        # 取出源行文本列（0~7）和“我的做T策略”下拉框的选择值
        items = [self.table.takeItem(src, c) for c in range(8)]
        cb = self.table.cellWidget(src, 8)
        t_idx = cb.currentIndex() if cb is not None else 0
        code = items[0].text() if items[0] else ""
        self.table.removeRow(src)
        # 往下拖时，删除源行会使目标行上移一位，插入点需 -1；往上拖不受影响
        insert_at = dst if dst < src else dst - 1
        self.table.insertRow(insert_at)
        for c, item in enumerate(items):
            self.table.setItem(insert_at, c, item)
        self._set_t_strategy_combo(insert_at, code)
        new_cb = self.table.cellWidget(insert_at, 8)
        if new_cb is not None:
            new_cb.setCurrentIndex(t_idx)
        self._set_sort_buttons(insert_at)
        self.table.setCurrentCell(insert_at, 0)
        self._save_stocks()

    def _add_stock(self):
        code = self.code_input.text().strip().upper()
        if not code or len(code) < 2:
            return
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).text() == code:
                QMessageBox.warning(self, "提示", f"{code} 已存在")
                return
        self._insert_row(code)
        self.code_input.clear()
        self._save_stocks()
        self._refresh()

    def _del_stock(self):
        rows = sorted({i.row() for i in self.table.selectedItems()}, reverse=True)
        for r in rows:
            code = self.table.item(r, 0).text()
            self._float_win.remove_stock(code)
            self._forget_t_strategy(code)
            self.table.removeRow(r)
        if not self._float_win._codes:
            self._float_win.hide()
        self._save_stocks()

    def _save_stocks(self):
        self.config["stocks"] = [self.table.item(r, 0).text() for r in range(self.table.rowCount())]
        save_config(self.config)

    def _on_rows_moved(self):
        self._save_stocks()

    def _on_interval_changed(self, val):
        self.config["interval"] = val
        save_config(self.config)
        self._start_timer()

    def _on_vol_param_changed(self):
        self.config["vol_n"] = self.vol_n_spin.value()
        self.config["vol_m"] = self.vol_m_spin.value()
        save_config(self.config)

    def _save_server_key(self):
        self.config["server_key"] = self.key_input.text().strip()
        save_config(self.config)

    def _on_float_color_changed(self, color):
        self.config["float_bg"] = color
        save_config(self.config)

    def _on_float_font_color_changed(self, color):
        self.config["float_font_color"] = color
        save_config(self.config)

    def _start_timer(self):
        if hasattr(self, "_timer"):
            self._timer.stop()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._auto_refresh)
        self._timer.start(self.config.get("interval", 10) * 1000)

    def _auto_refresh(self):
        """定时器触发的刷新：主窗口和浮窗都不可见时跳过，省流量/省接口调用"""
        if not self.isVisible() and not self._float_win.isVisible():
            self.status_label.setText("已暂停（窗口/浮窗均未显示）")
            return
        self._refresh()

    def _refresh(self):
        # 当日成交额跟随刷新间隔更新图表最新一根（历史在启动时已拉，不重复请求）
        self._refresh_amount_today()
        if self.worker and self.worker.isRunning():
            return
        codes = [self.table.item(r, 0).text() for r in range(self.table.rowCount())]
        if not codes:
            return
        self.refresh_btn.setEnabled(False)
        self.status_label.setText("刷新中...")
        self.worker = FetchWorker(codes)
        self.worker.result.connect(self._on_result)
        self.worker.error.connect(self._on_error)
        self.worker.finished.connect(self._on_fetch_done)
        self.worker.start()

    def _load_amount_history(self):
        """启动时拉一次完整历史成交额（幂等）"""
        if self._hist_worker and self._hist_worker.isRunning():
            return
        self._hist_worker = IndexHistoryWorker()
        self._hist_worker.result.connect(self._on_amount_history)
        self._hist_worker.start()

    def _on_amount_history(self, series_json):
        try:
            series = json.loads(series_json)
        except (ValueError, TypeError):
            series = []
        if not series:
            return   # 历史拉取失败，保留现状（当日刷新仍会兜底追加）
        self._amount_series = [(d, a) for d, a in series]
        self.amount_chart.set_data(self._amount_series)

    def _refresh_amount_today(self):
        """跟随刷新间隔拉当日总成交额，更新内存序列最新一根（幂等）"""
        if self._today_worker and self._today_worker.isRunning():
            return
        self._today_worker = IndexTodayWorker()
        self._today_worker.result.connect(self._on_amount_today)
        self._today_worker.start()

    def _on_amount_today(self, date, total):
        if not date or total < 0:
            return   # 当日拉取失败
        if self._amount_series and self._amount_series[-1][0] == date:
            self._amount_series[-1] = (date, total)   # 覆盖当日
        else:
            self._amount_series.append((date, total)) # 新交易日追加
        self.amount_chart.set_data(self._amount_series)

    def _on_fetch_done(self):
        self.refresh_btn.setEnabled(True)
        self.status_label.setText(f"上次刷新: {time.strftime('%H:%M:%S')}")

    def _on_result(self, code, name, price, change_pct, ma5, ma10, ma20, ma30, ma60, vols_json, closes_json):
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).text() != code:
                continue

            closes = json.loads(closes_json)
            vols   = json.loads(vols_json)

            # ── 算法4：综合趋势强度 ──────────────────────────────
            # 维度1(40%)：均线得分，股价在几条均线上方
            ma_list = [ma5, ma10, ma20, ma30]
            ma_score = sum(1 for v in ma_list if price >= v) / 4.0  # 0~1

            # 维度2(30%)：MA20斜率方向
            if len(closes) >= 25:
                ma20_now  = sum(closes[-20:]) / 20
                ma20_prev = sum(closes[-25:-5]) / 20
                slope_score = 1.0 if ma20_now > ma20_prev else 0.0
            else:
                slope_score = 0.5

            # 维度3(30%)：高低点结构（取最近15根K线的首尾斜率）
            if len(closes) >= 15:
                seg = closes[-15:]
                slope_h = seg[-1] - seg[0]
                structure_score = 1.0 if slope_h > 0 else 0.0
            else:
                structure_score = 0.5

            trend_score = ma_score * 0.4 + slope_score * 0.3 + structure_score * 0.3

            if trend_score >= 0.85:
                trend_text, trend_fg = "强势↑", QColor("#e74c3c")
            elif trend_score >= 0.6:
                trend_text, trend_fg = "偏多↗", QColor("#e74c3c")
            elif trend_score >= 0.4:
                trend_text, trend_fg = "震荡→", QColor("#888888")
            elif trend_score >= 0.15:
                trend_text, trend_fg = "偏空↘", QColor("#27ae60")
            else:
                trend_text, trend_fg = "弱势↓", QColor("#27ae60")

            # ── 活跃度：N日均量 / M日均量 ──────────────────────
            vol_n = self.vol_n_spin.value()
            vol_m = self.vol_m_spin.value()
            def vol_avg(n):
                return sum(vols[-n:]) / min(n, len(vols)) if vols else 0
            avg_n = vol_avg(vol_n)
            avg_m = vol_avg(vol_m)
            if avg_m > 0:
                vol_ratio = avg_n / avg_m
                vol_text = f"{vol_ratio:.2f}x"
                if vol_ratio >= 2.0:
                    vol_fg = QColor("#e74c3c")
                elif vol_ratio >= 1.2:
                    vol_fg = QColor("#e67e22")
                elif vol_ratio >= 1.0:
                    vol_fg = QColor("#888888")
                else:
                    vol_fg = QColor("#27ae60")
            else:
                vol_text, vol_fg = "--", None

            # ── 均线状态：股价在各均线上方/下方 ────────────────
            ma_items = [("MA5", ma5), ("MA10", ma10), ("MA20", ma20), ("MA30", ma30)]
            above = [n for n, v in ma_items if price >= v]
            below = [n for n, v in ma_items if price < v]
            if above and not below:
                ma_status, ma_fg = "均线全上方", QColor("#e74c3c")
            elif below and not above:
                ma_status, ma_fg = "均线全下方", QColor("#27ae60")
            elif above:
                ma_status = f"上方{'/'.join(above)}  下方{'/'.join(below)}"
                ma_fg = QColor("#e67e22")
            else:
                ma_status, ma_fg = "--", None

            # ── 做T策略：股价与MA10位置关系 ──────────────────────
            if price >= ma10:
                t_text, t_fg = "积极买进", QColor("#e74c3c")
            else:
                t_text, t_fg = "积极卖出", QColor("#27ae60")

            bg = QColor("#ffffff")
            chg_fg = QColor("#e74c3c") if change_pct > 0 else (QColor("#27ae60") if change_pct < 0 else None)

            updates = {
                1: (name, None),
                2: (f"{price:.4f}", None),
                3: (f"{change_pct:+.2f}%", chg_fg),
                4: (ma_status, ma_fg),
                5: (trend_text, trend_fg),
                6: (vol_text, vol_fg),
                7: (t_text, t_fg),
            }
            for c, (text, fg) in updates.items():
                item = QTableWidgetItem(text)
                item.setBackground(bg)
                if fg:
                    item.setForeground(fg)
                self.table.setItem(r, c, item)
            self.table.item(r, 0).setBackground(bg)

            # 同步浮窗
            if self._float_win.has_stock(code):
                self._float_win.update_stock(code, price, change_pct)
            break

    def _table_context_menu(self, pos):
        """右键菜单：加入/移出浮窗（最多3个），删除"""
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        code = self.table.item(row, 0).text()
        name = self.table.item(row, 1).text() if self.table.item(row, 1) else code
        menu = QMenu(self)
        in_float = self._float_win.has_stock(code)
        if in_float:
            act_pin = menu.addAction(f"从浮窗移除：{code}")
        else:
            act_pin = menu.addAction(f"加入浮窗：{name}({code})")
        menu.addSeparator()
        act_del = menu.addAction(f"删除 {code}")
        act = menu.exec(QCursor.pos())
        if act == act_pin:
            if in_float:
                self._float_win.remove_stock(code)
                if not self._float_win._codes:
                    self._float_win.hide()
            else:
                self._float_win.add_stock(code)
                if not self._float_win.isVisible():
                    self._float_win.show()
                    screen = QApplication.primaryScreen().availableGeometry()
                    self._float_win.move(screen.right() - 180, screen.bottom() - 120)
        elif act == act_del:
            self._float_win.remove_stock(code)
            if not self._float_win._codes:
                self._float_win.hide()
            self._forget_t_strategy(code)
            self.table.removeRow(row)
            self._save_stocks()

    def _on_error(self, code, msg):
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).text() == code:
                item = QTableWidgetItem(f"获取失败: {msg}")
                item.setForeground(QColor("#999999"))
                item.setToolTip(msg)
                self.table.setItem(r, 1, item)   # 写入“名称”列（文本列），不覆盖数据列
                for c in range(2, 8):            # 数据列(2~7)清空；第8列是用户手动策略，不动
                    self.table.setItem(r, c, QTableWidgetItem("--"))
                break

    def _push_weixin(self, code, name, price, change_pct, risks, ma_status=""):
        """Server酱微信推送，key 为空则跳过"""
        key = self.key_input.text().strip()
        if not key:
            return
        title = f"【股票风险】{name}({code})"
        desp = f"最新价: {price:.4f}  涨跌幅: {change_pct:+.2f}%\n均线状态: {ma_status}\n\n风险: {'、'.join(risks)}"
        try:
            url = f"https://sctapi.ftqq.com/{key}.send"
            data = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
            urllib.request.urlopen(url, data=data, timeout=8)
        except Exception:
            pass


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
