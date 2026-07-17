"""
股票监控工具 stock_monitor.py
================================
架构概览：
  - search_secid()    : 东方财富搜索接口，code → (secid, name)，结果内存缓存
  - fetch_stock_data(): 东方财富K线接口拉80日历史，计算最新价/涨跌幅/MA20/MA60
  - FetchWorker       : QThread，遍历所有code依次拉数据，通过signal推给主线程
  - FloatWidget       : 无边框置顶浮窗，最多3只股票，每只一行(价格+涨跌幅)
  - MainWindow        : 主窗口，表格+定时刷新+右键菜单+底部推送配置

数据接口（如接口挂了看这里换）：
  - 代码搜索: https://searchapi.eastmoney.com/api/suggest/get
  - K线历史: https://push2his.eastmoney.com/api/qt/stock/kline/get (东方财富，保留3位小数)
    支持 A股/ETF/期指/港股/恒生指数，secid 格式由搜索接口返回自动适配
  - 微信推送: https://sctapi.ftqq.com/{key}.send  (Server酱)

风险判断规则（在 MainWindow._on_result 里）：
  - price < MA60  → 跌破MA60 ⚠⚠（红色）
  - price < MA20  → 跌破MA20 ⚠（橙色）
  - 涨跌幅 <= -3% → 单日跌幅提示（橙色）
  - 涨跌幅 >= 7%  → 单日涨幅追高提示（橙色）

浮窗背景色规则（在 FloatWidget._apply_data 里）：
  - 有 MA60 风险 → #7f1d1d（暗红）
  - 有其他风险   → #7c4a00（橙红）
  - 正常          → #2c3e50（深色）

扩展方向：
  - 更多技术指标（MACD/RSI）: 在 fetch_stock_data() 里用 closes 列表扩展计算
  - 成本价/盈亏: config 里加 cost_price 字段，_on_result 里加列展示
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
    QHeaderView, QMessageBox, QSpinBox, QMenu, QSystemTrayIcon, QColorDialog
)
from PyQt6.QtCore import QThread, pyqtSignal, QTimer, Qt, QPoint
from PyQt6.QtGui import QColor, QFont, QCursor, QIcon, QPixmap

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


def fetch_stock_data(code):
    """拉取单只股票数据，返回 (name, price, change_pct, ma5, ma10, ma20, ma30, ma60, volumes, closes)
    使用腾讯K线接口，支持 A股/ETF/期指/港股/恒生指数。
    K线格式: [日期, 开, 收, 高, 低, 成交量, ...]，收盘价索引2，成交量索引5
    """
    secid, name = search_secid(code)
    tc = _get_tencent_prefix(secid)
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
    stock_data = (data.get("data") or {}).get(tc, {})
    klines = stock_data.get("day") or stock_data.get("qfqday") or []
    if not klines:
        raise Exception("无K线数据（代码有误或停牌）")

    closes = [float(k[2]) for k in klines]
    volumes = []
    for k in klines:
        try:
            volumes.append(float(k[5]))
        except (IndexError, ValueError, TypeError):
            volumes.append(0.0)

    current = closes[-1]
    prev = closes[-2] if len(closes) > 1 else current
    change_pct = (current - prev) / prev * 100 if prev else 0

    def ma(n):
        return sum(closes[-n:]) / min(n, len(closes))

    return name, current, change_pct, ma(5), ma(10), ma(20), ma(30), ma(60), volumes, closes


class FloatWidget(QWidget):
    """常驻最顶层浮窗，支持任意数量股票，每行：价格  涨跌幅
    内部状态：
      _codes  : 有序列表，决定行的显示顺序
      _data   : code → (price, change_pct, risk_text)，用于重建行时恢复数据
      _rows   : code → (price_lbl, chg_lbl)，当前显示的 Label 引用
    """

    color_changed = pyqtSignal(str)

    def __init__(self, bg_color="#2c3e50"):
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
                color: white;
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

    def _apply_data(self, code, price, change_pct, risk_text):
        """把数据写入对应行的 Label，并更新整体背景色（取最严重风险）"""
        if code not in self._rows:
            return
        price_lbl, chg_lbl = self._rows[code]
        price_lbl.setText(f"{price:.4f}")
        if change_pct > 0:
            chg_lbl.setText(f"{change_pct:+.2f}%")
            chg_lbl.setStyleSheet("color: white; background: transparent;")
        elif change_pct < 0:
            chg_lbl.setText(f"{change_pct:.2f}%")
            chg_lbl.setStyleSheet("color: white; background: transparent;")
        else:
            chg_lbl.setText(f"{change_pct:.2f}%")
            chg_lbl.setStyleSheet("color: white; background: transparent;")


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

    def update_stock(self, code, price, change_pct, risk_text):
        self._data[code] = (price, change_pct, risk_text)
        self._apply_data(code, price, change_pct, risk_text)
        self.adjustSize()

    def has_stock(self, code):
        return code in self._codes

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        elif event.button() == Qt.MouseButton.RightButton:
            menu = QMenu(self)
            act_color = menu.addAction("修改背景色...")
            act_close = menu.addAction("关闭浮窗")
            act = menu.exec(QCursor.pos())
            if act == act_color:
                color = QColorDialog.getColor(QColor(self._bg_color), self, "选择浮窗背景色")
                if color.isValid():
                    self._bg_color = color.name()
                    self._update_style()
                    self.color_changed.emit(self._bg_color)
            elif act == act_close:
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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("股票监控工具")
        self.resize(960, 520)
        self.config = load_config()
        self.worker = None
        self._last_push = {}      # code → 上次推送时间戳，限流用
        self._float_win = FloatWidget(bg_color=self.config.get("float_bg", "#2c3e50"))
        self._float_win.color_changed.connect(self._on_float_color_changed)
        self._build_ui()
        self._build_tray()
        self._start_timer()
        if self.config.get("stocks"):
            self._refresh()

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

        # 数据表格，列：代码/名称/最新价/涨跌幅/趋势/均线状态/活跃度
        self.table = QTableWidget()
        self.table.setColumnCount(9)
        self.table.setHorizontalHeaderLabels(["代码", "名称", "最新价", "涨跌幅", "均线状态", "趋势", "活跃度(N/M)", "做T策略", "排序"])
        h = self.table.horizontalHeader()
        for i in range(9):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        tooltips = {
            5: "趋势（算法4）：综合均线位置(40%)、MA20斜率(30%)、价格结构(30%)加权评分\n强势↑ ≥0.85 | 偏多↗ 0.60~0.85 | 震荡→ 0.40~0.60 | 偏空↘ 0.15~0.40 | 弱势↓ <0.15",
            6: "活跃度 = N日均量 / M日均量\n≥2.0x 明显放量(红) | 1.2~2.0x 轻微放量(橙) | 1.0~1.2x 正常(灰) | <1.0x 缩量(绿)",
            7: "做T策略：股价 ≥ MA10 → 积极买进(红)\n股价 < MA10 → 积极卖出(绿)",
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

        # 底部：微信推送Key + 状态栏
        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("微信推送Key(Server酱，可选):"))
        self.key_input = QLineEdit()
        self.key_input.setPlaceholderText("SCT... 填写后风险触发时自动推送微信")
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

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._tray_show()

    def changeEvent(self, event):
        super().changeEvent(event)
        from PyQt6.QtCore import QEvent
        if event.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            self.hide()

    def closeEvent(self, event):
        QApplication.quit()

    def _insert_row(self, code):
        r = self.table.rowCount()
        self.table.insertRow(r)
        for c, text in enumerate([code, "--", "--", "--", "--", "--", "--", "--"]):
            self.table.setItem(r, c, QTableWidgetItem(text))
        self._set_sort_buttons(r)

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
        lay.addWidget(btn_up)
        lay.addWidget(btn_dn)
        self.table.setCellWidget(row, 8, w)

    def _widget_row(self, widget):
        for r in range(self.table.rowCount()):
            if self.table.cellWidget(r, 8) == widget:
                return r
        return -1

    def _move_row(self, row, direction):
        target = row + direction
        if target < 0 or target >= self.table.rowCount():
            return
        for c in range(8):
            a = self.table.takeItem(row, c)
            b = self.table.takeItem(target, c)
            self.table.setItem(row, c, b)
            self.table.setItem(target, c, a)
        self.table.setCurrentCell(target, 0)
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

    def _start_timer(self):
        if hasattr(self, "_timer"):
            self._timer.stop()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(self.config.get("interval", 10) * 1000)

    def _refresh(self):
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
                self._float_win.update_stock(code, price, change_pct, trend_text)
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
            self.table.removeRow(row)
            self._save_stocks()

    def _on_error(self, code, msg):
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).text() == code:
                item = QTableWidgetItem(f"获取失败: {msg}")
                item.setForeground(QColor("#999999"))
                self.table.setItem(r, 6, item)
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
