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
  - fetch_kline_daily()   : 回测用带日期日K（腾讯优先/东财兜底），支持最近N天或起止日期区间
  - run_backtest()        : 多条件回测——买入/卖出各一组条件(AND)，10000股基数，算盈亏/胜率/回撤
                            条件类型见 COND_TYPES：站上X日线/跌破X日线/上次卖出后超过X天
  - AnalysisWindow        : 「股票分析」窗口，多行表格，每行买入+卖出两列可增删条件，后台跑，结果弹窗

数据接口（如接口挂了看这里换）：
  - 代码搜索: https://searchapi.eastmoney.com/api/suggest/get  (东方财富，任意代码 → secid)
  - K线历史(主): https://web.ifzq.gtimg.cn/appstock/app/fqkline/get  (腾讯，前复权日K，风控松)
  - K线历史(兜底): https://push2his.eastmoney.com/api/qt/stock/kline/get  (东方财富，secid直查，覆盖 A50 期指等)
    腾讯取不到才用东财——东财 push2his 高频请求易触发限流，只留给期指等特殊品种
  - 实时行情: https://qt.gtimg.cn/q={前缀+代码}  (腾讯，盘中高精度价，期指等不覆盖时降级用K线收盘价)
  - 微信推送: https://sctapi.ftqq.com/{key}.send  (Server酱，_push_weixin 已封装但当前未接入)

表格各列的计算规则（在 MainWindow._on_result 里）：
  - 量比       : 腾讯实时接口 f[38]，>2 红/1~2 灰/<1 绿；盘后/期指取不到显示 --
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
import datetime
import urllib.request
import urllib.parse
import urllib.error
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QSpinBox, QMenu, QSystemTrayIcon, QColorDialog,
    QComboBox, QStackedWidget, QDateEdit, QDialog, QFrame, QInputDialog,
    QCheckBox, QTextEdit
)
from PyQt6.QtCore import QThread, pyqtSignal, QTimer, Qt, QPoint, QEvent, QDate
from PyQt6.QtGui import QColor, QFont, QCursor, QIcon, QPixmap, QPainter, QPen, QBrush

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

# 回测日K缓存：key=(mode, code, 时间段参数, 当天日期) → (name, series)。
# 同一时间段重复点「确定」（只改买卖条件）直接复用，不再联网；换时间段或隔天才重取。
_backtest_kline_cache = {}

# "我的做T策略"下拉选项：(显示文本, 字体颜色)，索引即持久化到 config 的值
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
    """腾讯实时行情接口：tc(如 sz159995) → (price, change_pct, vol_ratio)，取不到返回 None

    盘中 K线接口当天那根收盘价只有 2 位精度（如 1.26），实时接口给 3 位（1.265），
    且涨跌幅用真实昨收计算。期指等特殊品种此接口不覆盖，返回 None 由调用方降级。
    字段(~分隔): 索引3=当前价, 索引32=涨跌幅, 索引38=换手率, 索引49=量比
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
        vol_ratio = float(f[49]) if len(f) > 49 and f[49] else None
        return price, change_pct, vol_ratio
    except Exception:
        return None


def _fetch_kline_tencent(tc):
    """腾讯K线接口：tc(如 sz159995) → (closes, volumes)，风控较松，覆盖 A股/ETF/港股/恒指。
    境外期指(如 A50)不覆盖，返回 None 由调用方转东财兜底。
    K线格式(数组): [日期, 开, 收, 高, 低, 成交量, ...]，收盘索引2，成交量索引5
    """
    # 用 newfqkline 而非 fqkline：后者路径时好时坏（间歇性 HTTP 501），
    # newfqkline 稳定返回且字段一致（日期idx0/收盘idx2/量idx5）。
    # 整体 try/except 兜底：任何异常（501/超时/断连）都返回 None，让调用方降级东财。
    url = (
        f"https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get"
        f"?_var=kline_day&param={tc},day,,,80,qfq"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Referer": "https://gu.qq.com/",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw[raw.index("=") + 1:])
        sd = (data.get("data") or {}).get(tc, {})
        klines = sd.get("qfqday") or sd.get("day") or []
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
    except Exception:
        return None


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
    """拉取单只股票数据，返回 (name, price, change_pct, ma5, ma10, ma20, ma30, ma60, volumes, closes, vol_ratio)

    K线来源：优先腾讯（风控松，覆盖 A股/ETF/港股/恒指），腾讯取不到再降级东方财富
    （secid 直查，覆盖 A50 等境外期指）。避免东财 push2his 高频请求触发限流。

    价格精度：均线/趋势/成交量全部基于 K线收盘价计算；当天最新价和涨跌幅优先取
    腾讯实时行情接口（高精度），取不到时降级用 K线收盘价（期指等特殊品种走此分支）。
    vol_ratio：量比，从腾讯实时接口 f[38] 取，取不到为 None。
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
    vol_ratio = None
    rt = fetch_realtime_quote(tc)
    if rt is not None:
        current, change_pct, vol_ratio = rt
        closes[-1] = current

    def ma(n):
        return sum(closes[-n:]) / min(n, len(closes))

    return name, current, change_pct, ma(5), ma(10), ma(20), ma(30), ma(60), volumes, closes, vol_ratio


def _fetch_kline_daily_tencent(tc, start="", end="", count=320):
    """腾讯K线（带日期）：tc → [(date, close), ...] 升序，取不到返回 None。
    param 位置: code,period,start,end,count,fq —— 传 start/end(YYYY-MM-DD)拉区间，
    留空传 count 拉最近 count 天。K线数组 [日期, 开, 收, 高, 低, 成交量, ...]。
    """
    param = f"{tc},day,{start},{end},{count},qfq"
    # newfqkline 而非 fqkline：后者路径间歇 501，newfqkline 稳定且字段一致
    url = f"https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get?_var=k&param={param}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Referer": "https://gu.qq.com/",
    })
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw[raw.index("=") + 1:])
    sd = (data.get("data") or {}).get(tc, {})
    klines = sd.get("qfqday") or sd.get("day") or []
    if not klines:
        return None
    out = []
    for k in klines:
        try:
            out.append((k[0], float(k[2])))
        except (IndexError, ValueError, TypeError):
            pass
    return out or None


def _fetch_kline_daily_eastmoney(secid, start="", end="", count=320):
    """东财K线（带日期）兜底：secid → [(date, close), ...] 升序，取不到返回 None。
    beg/end 用 YYYYMMDD；给了区间就用区间，否则用 lmt=count 拉最近 count 天。
    """
    if start and end:
        beg = start.replace("-", "")
        fin = end.replace("-", "")
        rng = f"&beg={beg}&end={fin}"
    else:
        rng = f"&end=20500101&lmt={count}"
    url = (
        f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57"
        f"&klt=101&fqt=1{rng}"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Referer": "https://www.eastmoney.com/",
    })
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw).get("data") or {}
    klines = data.get("klines") or []
    if not klines:
        return None
    out = []
    for line in klines:
        r = line.split(",")
        try:
            out.append((r[0], float(r[2])))
        except (IndexError, ValueError, TypeError):
            pass
    return out or None


# 单次日K最多拉取的交易日数（约 8 年）。腾讯 count 只作上限：默认 320 会把区间
# 截成「最近 320 个交易日」，所以区间模式必须按日历跨度把 count 放大到覆盖整个区间。
MAX_KLINE_COUNT = 2000


def fetch_kline_daily(code, start="", end="", count=320):
    """回测用日K：code → (name, [(date, close), ...] 升序)。优先腾讯，东财兜底。
    start/end 传 'YYYY-MM-DD' 拉区间；留空则用 count 拉最近 count 天。
    """
    secid, name = search_secid(code)
    tc = _get_tencent_prefix(secid)
    # 区间模式：count 仅是行数上限，默认 320 会把 start 截断成最近 320 个交易日。
    # 按日历跨度把 count 抬高到足以覆盖整个区间（封顶 MAX_KLINE_COUNT），让 start 生效。
    if start:
        end_ref = end or time.strftime("%Y-%m-%d")
        try:
            span_days = (datetime.datetime.strptime(end_ref, "%Y-%m-%d")
                         - datetime.datetime.strptime(start, "%Y-%m-%d")).days
        except ValueError:
            span_days = count
        count = min(MAX_KLINE_COUNT, max(count, span_days + 30))
    try:
        series = _fetch_kline_daily_tencent(tc, start, end, count)
    except Exception:
        series = None    # 腾讯异常（501/超时）时降级东财，而非整体失败
    if series is None:
        series = _fetch_kline_daily_eastmoney(secid, start, end, count)
    if series is None:
        raise Exception("无K线数据（代码有误或停牌）")
    return name, series


# 条件类型：type → (归属, 显示模板)。param 为条件参数（日线周期 / 天数）
COND_TYPES = {
    "ma_above":           ("buy",  "站上{p}日线"),      # 收盘 > MA(param)
    "cooldown":           ("buy",  "距上次卖出>{p}天"),  # 距上次卖出超过 param 个交易日
    "macd_golden":        ("buy",  "MACD金叉"),         # DIF 上穿 DEA（12/26/9，param 无意义）
    "breakout_last_sell": ("buy",  "突破上次卖点"),      # 收盘 > 上一次卖出价（OR 分支）
    "ma_below":           ("sell", "跌破{p}日线"),       # 收盘 < MA(param)
    "macd_death":         ("sell", "MACD死叉"),         # DIF 下穿 DEA（12/26/9，param 无意义）
}

# 无参数条件：这些类型的 param 数字框无意义，UI 置灰、回测忽略其值
PARAMLESS_TYPES = {"macd_golden", "macd_death", "breakout_last_sell"}

# OR 分支条件：买入侧这些条件与常规 AND 条件组「并列取或」——(AND组) 或 (任一OR条件)
OR_TYPES = {"breakout_last_sell"}


def _macd(closes, fast=12, slow=26, signal=9):
    """标准 MACD：closes → (dif[], dea[])，与 closes 等长。
    DIF = EMA(fast) - EMA(slow)，DEA = EMA(DIF, signal)。EMA 以首值播种、递推。
    前 slow 根尚未稳定，穿越判定由调用方用 i>=slow 过滤。
    """
    n = len(closes)
    if n == 0:
        return [], []

    def _ema(seq, period):
        k = 2.0 / (period + 1)
        out = [seq[0]]
        for x in seq[1:]:
            out.append(x * k + out[-1] * (1 - k))
        return out

    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    dif = [ema_fast[i] - ema_slow[i] for i in range(n)]
    dea = _ema(dif, signal)
    return dif, dea


def _cond_met(cond, closes, i, last_sell_idx, macd=None, last_sell_price=None):
    """单个条件在第 i 根K线是否成立。
    macd 为 (dif, dea) 预算结果，仅 MACD 类条件需要（由 run_backtest 传入）。
    last_sell_price 为上一次卖出价，仅"突破上次卖点"需要。
    """
    t, p = cond["type"], cond["param"]
    if t == "ma_above":
        if i < p - 1:
            return False
        return closes[i] > sum(closes[i - p + 1:i + 1]) / p
    if t == "ma_below":
        if i < p - 1:
            return False
        return closes[i] < sum(closes[i - p + 1:i + 1]) / p
    if t == "cooldown":
        if last_sell_idx is None:
            return True                      # 从未卖出过 → 无冷却限制
        return (i - last_sell_idx) > p
    if t in ("macd_golden", "macd_death"):
        if macd is None or i < 26:           # 前 26 根 EMA 未稳定，不判穿越
            return False
        dif, dea = macd
        if t == "macd_golden":               # DIF 上穿 DEA
            return dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]
        return dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]   # 下穿
    if t == "breakout_last_sell":
        if last_sell_price is None:          # 从未卖出过 → 无“上次卖点”可突破
            return False
        return closes[i] > last_sell_price
    return False


def conds_desc(conds):
    """把条件列表拼成中文描述。常规条件用「且」连；OR 分支（如突破上次卖点）
    与常规组「或」连，如 '(站上5日线 且 距上次卖出>3天) 或 突破上次卖点'。
    """
    def _fmt(c):
        return COND_TYPES.get(c["type"], (None, "?"))[1].format(p=c["param"])

    reg_txt = " 且 ".join(_fmt(c) for c in conds if c["type"] not in OR_TYPES)
    or_txt = " 或 ".join(_fmt(c) for c in conds if c["type"] in OR_TYPES)
    if reg_txt and or_txt:
        return f"({reg_txt}) 或 {or_txt}"
    return reg_txt or or_txt or "（无条件）"


def run_backtest(dates, closes, buy_conds, sell_conds, start_idx, shares=10000):
    """多条件回测：空仓且全部买入条件成立 → 买入 shares 股；
    持仓且全部卖出条件成立 → 全部卖出。逐日推进（信号在 start_idx 起生效）。
    某侧条件列表为空则该侧永不触发。毛收益口径（不计手续费）。

    返回 dict：
      trades   : [{date, side, price, shares, pnl}]  side ∈ {'买入','卖出'}
      summary  : {total_pnl, return_pct, principal, final_equity, holding}
      buy_hold : {has_buy, buy_date, buy_price, pnl, return_pct}  首个买点买入并持有到期末的对照
      stats    : {trade_count, win_rate, max_drawdown, holding, buy_desc, sell_desc}
    """
    trades = []
    holding = False
    buy_price = 0.0
    realized = 0.0            # 已实现盈亏累计
    wins = 0
    round_trips = 0
    last_sell_idx = None      # 上次卖出的K线索引（冷却条件用）
    last_sell_price = None    # 上次卖出价（"突破上次卖点"条件用）
    equity_curve = []         # 逐日盯市权益（相对本金的浮动，用于最大回撤）

    # MACD 只在有 MACD 类条件时预算一次（DIF/DEA），避免逐根重算
    macd = None
    if any(c["type"] in ("macd_golden", "macd_death") for c in buy_conds + sell_conds):
        macd = _macd(closes)

    # 「同时满足」规则用：买入侧「站上X日线」的周期 + 卖出侧是否含「跌破X日线」。
    # 买入当天若卖出条件也已全部满足，则这一笔的「跌破X日线」改用买入周期 X（见 eff_sell_conds），
    # 避免「站上5买、跌破10卖，进场即在10日线下、次日就被卖」这种刚进场就出场的不合理。
    buy_ma_p = next((c["param"] for c in buy_conds if c["type"] == "ma_above"), None)
    has_ma_sell = any(c["type"] == "ma_below" for c in sell_conds)
    eff_sell_conds = sell_conds   # 当前持仓实际生效的卖出条件（默认与配置一致）

    n = len(closes)
    for i in range(start_idx, n):
        price = closes[i]
        # 当日盯市权益（已实现 + 持仓浮盈）
        floating = (price - buy_price) * shares if holding else 0.0
        equity_curve.append(realized + floating)

        if not holding:
            # 买入 = (常规 AND 条件组全成立) 或 (任一 OR 分支条件成立，如突破上次卖点)
            regular = [c for c in buy_conds if c["type"] not in OR_TYPES]
            or_conds = [c for c in buy_conds if c["type"] in OR_TYPES]
            reg_ok = bool(regular) and all(
                _cond_met(c, closes, i, last_sell_idx, macd, last_sell_price) for c in regular)
            or_ok = any(
                _cond_met(c, closes, i, last_sell_idx, macd, last_sell_price) for c in or_conds)
            if reg_ok or or_ok:
                holding = True
                buy_price = price
                trades.append({"date": dates[i], "side": "买入", "price": price,
                               "shares": shares, "pnl": None})
                # 买入当天卖出条件也全满足 → 这一笔的「跌破X日线」改用买入周期，避免刚进场就被卖
                sell_now = bool(sell_conds) and all(
                    _cond_met(c, closes, i, last_sell_idx, macd, last_sell_price) for c in sell_conds)
                if sell_now and buy_ma_p is not None and has_ma_sell:
                    eff_sell_conds = [dict(c, param=buy_ma_p) if c["type"] == "ma_below" else c
                                      for c in sell_conds]
                else:
                    eff_sell_conds = sell_conds
        else:
            if eff_sell_conds and all(
                    _cond_met(c, closes, i, last_sell_idx, macd, last_sell_price) for c in eff_sell_conds):
                pnl = (price - buy_price) * shares
                realized += pnl
                round_trips += 1
                if pnl > 0:
                    wins += 1
                holding = False
                last_sell_idx = i
                last_sell_price = price
                trades.append({"date": dates[i], "side": "卖出", "price": price,
                               "shares": shares, "pnl": pnl})

    # 期末仍持仓：按最后一根收盘价计未实现盈亏
    last_price = closes[-1] if n else 0.0
    unrealized = (last_price - buy_price) * shares if holding else 0.0
    total_pnl = realized + unrealized

    first_buy_trade = next((t for t in trades if t["side"] == "买入"), None)
    first_buy = first_buy_trade["price"] if first_buy_trade else 0.0
    principal = first_buy * shares
    return_pct = (total_pnl / principal * 100) if principal else 0.0

    # 买入并持有对照：首个买点买入 shares 股，忽略卖出策略，持有到区间末按末日收盘价计
    if first_buy_trade:
        bh_pnl = (last_price - first_buy) * shares
        buy_hold = {
            "has_buy": True,
            "buy_date": first_buy_trade["date"],
            "buy_price": first_buy,
            "pnl": bh_pnl,
            "return_pct": (bh_pnl / principal * 100) if principal else 0.0,
        }
    else:
        buy_hold = {"has_buy": False, "buy_date": None, "buy_price": 0.0,
                    "pnl": 0.0, "return_pct": 0.0}

    # 最大回撤：权益曲线峰值到谷底的最大跌幅（金额）
    max_dd = 0.0
    peak = equity_curve[0] if equity_curve else 0.0
    for v in equity_curve:
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)

    return {
        "trades": trades,
        "summary": {
            "total_pnl": total_pnl,
            "return_pct": return_pct,
            "principal": principal,
            "final_equity": principal + total_pnl,
            "holding": holding,
        },
        "buy_hold": buy_hold,
        "stats": {
            "trade_count": round_trips,
            "win_rate": (wins / round_trips * 100) if round_trips else 0.0,
            "max_drawdown": max_dd,
            "holding": holding,
            "buy_desc": conds_desc(buy_conds),
            "sell_desc": conds_desc(sell_conds),
        },
    }


def _resolve_backtest_window(series, mode, days, start):
    """把 (时间类型, 参数) 解析成回测所需的 (dates, closes, start_idx)。
    mode='days' 取最近 days 天为回测区间；mode='range' 从 start 起为回测区间。
    区间前的数据（fetch 时已多取）用于均线预热，start_idx 指向回测区间首日。
    """
    dates = [d for d, _ in series]
    closes = [c for _, c in series]
    if not dates:
        raise Exception("无K线数据")
    if mode == "range":
        start_idx = next((i for i, d in enumerate(dates) if d >= start), len(dates))
        if start_idx >= len(dates):
            raise Exception("所选起始日期晚于最新数据")
    else:
        start_idx = max(0, len(dates) - max(1, days))
    return dates, closes, start_idx


# ── 组合寻优：在参数网格上批量回测，按目标函数（收益率/胜率/综合评分）排序，结果并入排行榜 ──
OPT_BUY_MA = [3, 5, 10, 20, 30, 60]     # 买入「站上X日线」候选周期
OPT_SELL_MA = [3, 5, 10, 20, 30, 60]    # 卖出「跌破X日线」候选周期
OPT_COOLDOWN = [0, 5]                   # 买入侧冷却天数（0=不加冷却）
# 组合寻优说明（排行窗常驻）
OPTIMIZE_NOTE = ("组合寻优：买入[站上3/5/10/20/30/60日线、MACD金叉]"
                 "（各可选 或突破上次卖点、各可选叠加冷却5天） × 卖出[跌破3/5/10/20/30/60日线、MACD死叉] = 196 种")
COMBO_NOTE = OPTIMIZE_NOTE   # 兼容旧引用


def enumerate_optimize_combos():
    """枚举寻优网格的全部买卖组合，返回 [(buy_conds, sell_conds), ...]。

    买入主条件（7）：站上3/5/10/20/30/60日线 + MACD金叉；
      每个主条件 × 是否叠加冷却5天(2) × 是否叠加「或突破上次卖点」(2) = 28 种买入。
    卖出（7）：跌破3/5/10/20/30/60日线 + MACD死叉。
    → 28 × 7 = 196 种。突破上次卖点单独无法触发首次买入，故只作 OR 附加项。
    """
    primaries = [{"type": "ma_above", "param": p} for p in OPT_BUY_MA]
    primaries.append({"type": "macd_golden", "param": 0})
    buys = []
    for pc in primaries:
        for cd in OPT_COOLDOWN:
            base = [pc] + ([{"type": "cooldown", "param": cd}] if cd else [])
            buys.append(base)                                                  # 不叠加突破
            buys.append(base + [{"type": "breakout_last_sell", "param": 0}])   # 叠加「或突破上次卖点」
    sells = [[{"type": "ma_below", "param": p}] for p in OPT_SELL_MA]
    sells.append([{"type": "macd_death", "param": 0}])
    return [(b, s) for b in buys for s in sells]


def optimize_score(return_pct, win_rate, trade_count):
    """综合评分：收益率 × 胜率 × min(1, 次数/3)。用交易次数因子惩罚样本过少的偶然高收益。"""
    return return_pct * (win_rate / 100.0) * min(1.0, trade_count / 3.0)


# ── AI 诊股：调用大模型（Anthropic Messages / OpenAI 兼容），只用标准库 urllib，无第三方依赖 ──
AI_DEFAULTS = {
    "anthropic": {"base_url": "https://api.anthropic.com", "model": "claude-opus-5"},
    "openai":    {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
}

AI_DIAGNOSE_SYSTEM = (
    "你是一名严谨的A股/ETF技术分析助手。基于用户提供的均线、量比、MACD等技术指标，用简体中文分析，"
    "结构分为：①趋势研判 ②关键均线/价位 ③操作倾向（偏多/偏空/观望，并说明理由）④风险提示。"
    "要求：客观、简洁（400字以内），只依据给出的数据，不臆测未提供的基本面或消息面，"
    "不承诺具体买卖点位。结尾不要写免责声明（界面已提供）。"
)


def _ai_config(config):
    """读取 AI 配置，缺省用默认值。返回 (provider, base_url, api_key, model)。"""
    provider = config.get("ai_provider") or "anthropic"
    d = AI_DEFAULTS.get(provider, AI_DEFAULTS["anthropic"])
    base_url = ((config.get("ai_base_url") or "").strip() or d["base_url"]).rstrip("/")
    api_key = (config.get("ai_api_key") or "").strip()
    model = (config.get("ai_model") or "").strip() or d["model"]
    return provider, base_url, api_key, model


def ai_chat(config, system, user, max_tokens=1500, timeout=60):
    """调用配置的大模型，返回回复文本。未配置 key 或出错抛异常。"""
    provider, base_url, api_key, model = _ai_config(config)
    if not api_key:
        raise Exception("尚未配置 API Key，请先点「AI 设置」填写。")
    if provider == "openai":
        url = f"{base_url}/chat/completions"
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {api_key}"}
        body = {"model": model, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
    else:
        url = f"{base_url}/v1/messages"
        headers = {"Content-Type": "application/json",
                   "x-api-key": api_key,
                   "anthropic-version": "2023-06-01"}
        body = {"model": model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:600]
        except Exception:
            pass
        raise Exception(f"HTTP {e.code}: {detail or e.reason}")
    payload = json.loads(raw)
    if provider == "openai":
        return (payload["choices"][0]["message"]["content"] or "").strip() or "(空回复)"
    # anthropic：content 为内容块列表，拼接 text 块
    parts = [b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip() or "(空回复)"


def _ai_stock_context(code):
    """采集某股票技术面，拼成给大模型的中文上下文。返回 (name, context_text)。"""
    (name, price, change_pct, ma5, ma10, ma20, ma30, ma60,
     volumes, closes, vol_ratio) = fetch_stock_data(code)
    dif, dea = _macd(closes)
    if dif and dea:
        macd_state = "多头(DIF>DEA)" if dif[-1] > dea[-1] else "空头(DIF<DEA)"
        macd_line = f"MACD(12/26/9)：DIF={dif[-1]:.4f} DEA={dea[-1]:.4f}（{macd_state}）\n"
    else:
        macd_line = "MACD(12/26/9)：数据不足\n"
    chg20 = ((closes[-1] / closes[-21] - 1) * 100) if len(closes) > 21 else 0.0

    def rel(m):
        return "上方" if price >= m else "下方"

    above = sum(1 for m in (ma5, ma10, ma20, ma30) if price >= m)
    ctx = (
        f"股票代码：{code}　名称：{name}\n"
        f"最新价：{price:.4f}　当日涨跌幅：{change_pct:+.2f}%　近20日涨幅：{chg20:+.2f}%\n"
        f"均线：MA5={ma5:.4f}({rel(ma5)}) MA10={ma10:.4f}({rel(ma10)}) "
        f"MA20={ma20:.4f}({rel(ma20)}) MA30={ma30:.4f}({rel(ma30)}) MA60={ma60:.4f}({rel(ma60)})\n"
        f"股价位于 {above}/4 条均线(MA5/10/20/30)上方\n"
        f"量比：{'--' if vol_ratio is None else f'{vol_ratio:.2f}'}\n"
        + macd_line
    )
    return name, ctx


class AmountChartWidget(QWidget):
    """两市+创业板总成交额可视化（QPainter 手绘）：
    左侧最近 5 日柱状图（柱顶标数值、柱下标日期），右侧 5/10/20/30/60 日均值文字。
    数据由 set_data([(日期, 总成交额元), ...] 升序) 传入，单位显示为"亿"。
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
        """元 → 'xxxx亿'"""
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


class KLineChartWidget(QWidget):
    """回测收盘价折线 + 买卖点标注（QPainter 手绘）。
    series : [[date, close], ...] 升序（回测区间）
    trades : [{date, side, price, pnl}, ...]，side ∈ {'买入','卖出'}
    买点红点、卖点绿点直接画在折线上；鼠标悬停到某点时，在右上角固定位置
    显示该点的方向/日期/价格/单笔盈亏。
    """

    RED, GREEN, LINE, AXIS, TEXT = "#e74c3c", "#27ae60", "#2980b9", "#cccccc", "#555555"
    HIT_RADIUS = 8            # 鼠标距圆点多少像素内算命中

    def __init__(self, series, trades):
        super().__init__()
        self._series = series or []
        self._trades = trades or []
        self._hits = []       # 上次绘制时记录的 [(x, y, trade), ...]，供命中检测
        self._hover = None    # 当前悬停的 trade（None 表示无）
        self.setMinimumSize(680, 400)
        self.setMouseTracking(True)   # 不按键也接收 mouseMoveEvent

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        painter.fillRect(0, 0, w, h, QColor("#ffffff"))

        if len(self._series) < 2:
            painter.setPen(QColor("#999999"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "无足够价格数据")
            painter.end()
            return

        dates = [d for d, _ in self._series]
        closes = [float(c) for _, c in self._series]
        n = len(closes)
        pmin, pmax = min(closes), max(closes)
        span = (pmax - pmin) or 1.0
        pad = span * 0.08                      # 上下留白，避免曲线贴边
        pmin, pmax = pmin - pad, pmax + pad
        span = pmax - pmin

        left, right, top, bottom = 58, 16, 30, 42
        plot_w = w - left - right
        plot_h = h - top - bottom

        def px(i):
            return left + plot_w * (i / (n - 1))

        def py(price):
            return top + plot_h * (1 - (price - pmin) / span)

        # ── 标题 ──
        tf = QFont(); tf.setPointSize(9); tf.setBold(True)
        painter.setFont(tf); painter.setPen(QColor("#333333"))
        painter.drawText(8, 18, "收盘价与买卖点")

        # ── 网格 + Y 轴价格刻度（5 档）──
        gf = QFont(); gf.setPointSize(8)
        painter.setFont(gf)
        for k in range(5):
            price = pmin + span * k / 4
            y = int(py(price))
            painter.setPen(QPen(QColor(self.AXIS), 1))
            painter.drawLine(left, y, left + plot_w, y)
            painter.setPen(QColor(self.TEXT))
            painter.drawText(0, y - 7, left - 4, 14,
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             f"{price:.3f}")

        # ── X 轴日期（首/中/尾 3 处）──
        for i in (0, n // 2, n - 1):
            x = int(px(i))
            md = dates[i][5:] if len(dates[i]) >= 10 else dates[i]
            painter.setPen(QColor(self.TEXT))
            painter.drawText(x - 28, h - bottom + 6, 56, 14,
                             Qt.AlignmentFlag.AlignHCenter, md)

        # ── 收盘价折线 ──
        painter.setPen(QPen(QColor(self.LINE), 1.6))
        prev = None
        for i in range(n):
            cur = QPoint(int(px(i)), int(py(closes[i])))
            if prev is not None:
                painter.drawLine(prev, cur)
            prev = cur

        # ── 买卖点：折线上画实心圆点（买红卖绿），并记录屏幕坐标供悬停命中 ──
        idx_of = {d: i for i, d in enumerate(dates)}
        sf = QFont(); sf.setPointSize(8)
        hits = []
        for t in self._trades:
            i = idx_of.get(t["date"])
            if i is None:
                continue
            x, y = int(px(i)), int(py(float(t["price"])))
            color = QColor(self.RED if t["side"] == "买入" else self.GREEN)
            painter.setPen(QPen(color, 1))
            painter.setBrush(QBrush(color))
            painter.drawEllipse(QPoint(x, y), 4, 4)
            hits.append((x, y, t))
        self._hits = hits

        # ── 图例 ──
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setFont(sf)
        painter.setPen(QColor(self.RED))
        painter.drawText(left, 18, 90, 14, Qt.AlignmentFlag.AlignLeft, "● 买入")
        painter.setPen(QColor(self.GREEN))
        painter.drawText(left + 60, 18, 120, 14, Qt.AlignmentFlag.AlignLeft, "● 卖出")
        painter.setPen(QColor("#999999"))
        painter.drawText(left + 130, 18, 200, 14, Qt.AlignmentFlag.AlignLeft, "（鼠标移到点上看详情）")

        # ── 悬停信息框：固定在右上角 ──
        if self._hover is not None:
            self._draw_hover_box(painter, w, right, top)
        painter.end()

    def _draw_hover_box(self, painter, w, right, top):
        """在图右上角画一个信息框，显示当前悬停买卖点的方向/日期/价格/盈亏。"""
        t = self._hover
        is_buy = t["side"] == "买入"
        side_color = self.RED if is_buy else self.GREEN
        lines = [
            (t["side"], side_color),
            (f"日期 {t['date']}", self.TEXT),
            (f"价格 {float(t['price']):.4f}", self.TEXT),
        ]
        pnl = t.get("pnl")
        if pnl is not None:
            pnl_color = self.RED if pnl > 0 else (self.GREEN if pnl < 0 else "#888888")
            lines.append((f"盈亏 {pnl:+,.2f}", pnl_color))

        bf = QFont(); bf.setPointSize(9)
        painter.setFont(bf)
        box_w, line_h, pad = 150, 18, 8
        box_h = pad * 2 + line_h * len(lines)
        bx = w - right - box_w
        by = top + 4
        painter.setPen(QPen(QColor("#cccccc"), 1))
        painter.setBrush(QBrush(QColor(255, 255, 255, 235)))
        painter.drawRoundedRect(bx, by, box_w, box_h, 6, 6)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for k, (text, color) in enumerate(lines):
            painter.setPen(QColor(color))
            painter.drawText(bx + pad, by + pad + k * line_h, box_w - pad * 2, line_h,
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

    def mouseMoveEvent(self, event):
        """找离光标最近、且在 HIT_RADIUS 内的买卖点，作为悬停项。"""
        pos = event.position()
        mx, my = pos.x(), pos.y()
        best, best_d2 = None, self.HIT_RADIUS ** 2
        for x, y, t in self._hits:
            d2 = (mx - x) ** 2 + (my - y) ** 2
            if d2 <= best_d2:
                best, best_d2 = t, d2
        if best is not self._hover:
            self._hover = best
            self.update()

    def leaveEvent(self, event):
        if self._hover is not None:
            self._hover = None
            self.update()


class KLineDialog(QDialog):
    """K线买卖点弹窗：顶部回测汇总信息 + 中间 K线折线(标买卖点) + 底部交易明细表。"""

    def __init__(self, code, name, report, parent=None, on_rank=None, on_ai=None):
        super().__init__(parent)
        self.on_rank = on_rank            # 「收益排行」按钮回调（无则不显示该按钮）
        self.on_ai = on_ai                # 「AI 解读」按钮回调（无则不显示该按钮）
        self.setWindowTitle(f"K线买卖点 · {name}({code})")
        self.resize(820, 680)
        lay = QVBoxLayout(self)

        s = report["summary"]
        st = report["stats"]
        win = report.get("window", {})
        red, green, gray = "#e74c3c", "#27ae60", "#888888"

        # ── 汇总 ──
        pnl_color = red if s["total_pnl"] > 0 else (green if s["total_pnl"] < 0 else gray)
        summary = QLabel(
            f"<b>区间</b>：{win.get('first', '?')} ~ {win.get('last', '?')}　"
            f"<b>买入</b>：{st['buy_desc']}　<b>卖出</b>：{st['sell_desc']}　(10000股/笔)<br>"
            f"<b>总盈亏</b>：<span style='color:{pnl_color}'>{s['total_pnl']:+,.2f} 元</span>　"
            f"<b>收益率</b>：<span style='color:{pnl_color}'>{s['return_pct']:+.2f}%</span>　"
            f"<b>本金(首次买入)</b>：{s['principal']:,.2f} 元　"
            f"<b>期末资产</b>：{s['final_equity']:,.2f} 元"
        )
        summary.setTextFormat(Qt.TextFormat.RichText)
        summary.setWordWrap(True)
        lay.addWidget(summary)

        # ── 统计指标 ──
        hold_txt = "是（未平仓）" if st["holding"] else "否"
        stats = QLabel(
            f"交易次数(完整来回)：{st['trade_count']}　"
            f"胜率：{st['win_rate']:.1f}%　"
            f"最大回撤：{st['max_drawdown']:,.2f} 元　"
            f"期末持仓：{hold_txt}"
        )
        stats.setStyleSheet("color:#555; font-size:12px;")
        lay.addWidget(stats)

        # ── 买入并持有对照 ──
        bh = report.get("buy_hold", {"has_buy": False})
        if bh["has_buy"]:
            bh_color = red if bh["pnl"] > 0 else (green if bh["pnl"] < 0 else gray)
            diff = s["return_pct"] - bh["return_pct"]          # 策略收益率 − 持有收益率
            diff_color = red if diff > 0 else (green if diff < 0 else gray)
            diff_txt = "策略跑赢" if diff > 0 else ("策略跑输" if diff < 0 else "打平")
            buyhold = QLabel(
                f"<b>买入并持有对照</b>（首个买点 {bh['buy_date']}@{bh['buy_price']:.4f} → 期末，不执行卖出）　"
                f"总盈亏：<span style='color:{bh_color}'>{bh['pnl']:+,.2f} 元</span>　"
                f"收益率：<span style='color:{bh_color}'>{bh['return_pct']:+.2f}%</span>　"
                f"<span style='color:{diff_color}'>{diff_txt} {abs(diff):.2f}%</span>"
            )
        else:
            buyhold = QLabel("<b>买入并持有对照</b>：该区间未触发买入信号，无对照。")
        buyhold.setTextFormat(Qt.TextFormat.RichText)
        buyhold.setWordWrap(True)
        buyhold.setStyleSheet("font-size:12px;")
        lay.addWidget(buyhold)

        # ── K线折线（标买卖点，主区域）──
        trades = report["trades"]
        series = report.get("series", [])
        lay.addWidget(KLineChartWidget(series, trades), stretch=1)

        # ── 交易明细表 ──
        tbl = QTableWidget()
        tbl.setColumnCount(5)
        tbl.setHorizontalHeaderLabels(["日期", "方向", "价格", "股数", "单笔盈亏"])
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tbl.setMaximumHeight(160)
        tbl.setRowCount(len(trades))
        for r, t in enumerate(trades):
            side_color = red if t["side"] == "买入" else green
            cells = [
                (t["date"], None),
                (t["side"], QColor(side_color)),
                (f"{t['price']:.4f}", None),
                (f"{t['shares']:,}", None),
                ("--" if t["pnl"] is None else f"{t['pnl']:+,.2f}",
                 None if t["pnl"] is None else QColor(red if t["pnl"] > 0 else green)),
            ]
            for c, (text, fg) in enumerate(cells):
                item = QTableWidgetItem(text)
                if fg:
                    item.setForeground(fg)
                tbl.setItem(r, c, item)
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        lay.addWidget(tbl)
        if not trades:
            lay.addWidget(QLabel("该区间内没有触发任何买卖信号。"))

        # ── 底部按钮：收益排行 + 关闭 ──
        btn_bar = QHBoxLayout()
        if self.on_rank is not None:
            btn_rank = QPushButton("收益排行")
            btn_rank.clicked.connect(self.on_rank)
            btn_bar.addWidget(btn_rank, alignment=Qt.AlignmentFlag.AlignLeft)
        if self.on_ai is not None:
            btn_ai = QPushButton("AI 解读")
            btn_ai.setToolTip("让大模型基于当前技术面解读这只股票")
            btn_ai.clicked.connect(self.on_ai)
            btn_bar.addWidget(btn_ai, alignment=Qt.AlignmentFlag.AlignLeft)
        btn_bar.addStretch()
        btn = QPushButton("关闭")
        btn.clicked.connect(self.close)
        btn_bar.addWidget(btn)
        lay.addLayout(btn_bar)


class RankDialog(QDialog):
    """收益排行榜（非模态）：只列当前股票+当前时间段下各买卖条件组合的回测结果，
    按收益率降序。点任意行发出 row_activated(条目索引)，由外部打开对应K线窗口。
    """
    row_activated = pyqtSignal(int)
    scope_changed = pyqtSignal(str)     # 下拉切换股票/时间段作用域时发出 scope_key
    combo_requested = pyqtSignal(str)   # 点「组合排行」时发出当前作用域 scope_key
    scope_deleted = pyqtSignal(str)     # 点「删除此排行」时发出当前作用域 scope_key
    note_edited = pyqtSignal(str, str)  # 编辑备注时发出 (scope_key, 备注文本)
    open_other = pyqtSignal(str)        # 「其他股票」下拉选中时发出该作用域 scope_key（外部新开窗口）

    SORT_KEYS = [("综合评分", "score"), ("收益率", "return_pct"),
                 ("胜率", "win_rate"), ("交易次数", "trade_count")]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("收益排行")
        self.resize(820, 460)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)  # 关闭即销毁，支持多开
        self.filter_code = None   # 本窗口过滤的股票代码（None=全部）
        self.view_scope = None    # 本窗口当前查看的作用域 scope_key
        self._subtitle = ""       # 当前副标题（重排时复用）
        self._entries = []        # 当前作用域原始条目（顺序与 rank_store 一致）
        self._view = []           # 表格行 → 原始条目索引 的映射（排序/过滤后）
        lay = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("股票 / 时间段："))
        self.scope_box = QComboBox()
        self.scope_box.setMinimumWidth(340)
        self.scope_box.currentIndexChanged.connect(self._on_combo)
        top.addWidget(self.scope_box)
        self.note_btn = QPushButton("备注")
        self.note_btn.setToolTip("给当前所选排行添加/修改备注（会显示在上面的下拉框里）")
        self.note_btn.clicked.connect(self._on_note_click)
        top.addWidget(self.note_btn)
        self.del_btn = QPushButton("删除此排行")
        self.del_btn.setToolTip("删除当前所选股票/时间段的整份排行（本地一并删除）")
        self.del_btn.clicked.connect(self._on_delete_click)
        top.addWidget(self.del_btn)
        top.addStretch()
        top.addWidget(QLabel("其他股票："))
        self.other_box = QComboBox()
        self.other_box.setMinimumWidth(220)
        self.other_box.setToolTip("选择其他股票的排行，在新窗口打开")
        self.other_box.activated.connect(self._on_other_activated)
        top.addWidget(self.other_box)
        lay.addLayout(top)
        self._notes = {}   # scope_key → 备注文本（供编辑时回填输入框）

        self.subtitle = QLabel("")
        self.subtitle.setStyleSheet("color:#555; font-size:12px;")
        self.subtitle.setWordWrap(True)
        lay.addWidget(self.subtitle)

        # 排序/过滤控件：把「组合寻优」的多维排序能力带进排行窗
        sortbar = QHBoxLayout()
        sortbar.addWidget(QLabel("排序依据："))
        self.sort_combo = QComboBox()
        for label, _k in self.SORT_KEYS:
            self.sort_combo.addItem(label)
        self.sort_combo.currentIndexChanged.connect(self._render)
        sortbar.addWidget(self.sort_combo)
        self.only3 = QCheckBox("仅交易次数≥3")
        self.only3.setToolTip("过滤只成交一两笔的偶然高收益组合")
        self.only3.stateChanged.connect(self._render)
        sortbar.addWidget(self.only3)
        sortbar.addStretch()
        lay.addLayout(sortbar)

        self.tbl = QTableWidget()
        self.tbl.setColumnCount(8)
        self.tbl.setHorizontalHeaderLabels(
            ["排名", "买入策略", "卖出策略", "交易次数", "胜率%", "收益率%", "综合评分", "总盈亏"])
        self.tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tbl.cellClicked.connect(self._on_cell_clicked)
        # 列宽模式只设一次：买卖策略拉伸，其余交互式（固定，不随每次重排逐格测量）。
        # 用 ResizeToContents 会在每次填表时把整列所有行都重新量一遍，196 行时切排序很卡。
        h = self.tbl.horizontalHeader()
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for c in (0, 3, 4, 5, 6, 7):
            h.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
        self._need_resize = True   # 仅在数据集变化(refresh)后量一次列宽，纯排序/过滤不再量
        lay.addWidget(self.tbl)

        bottom = QHBoxLayout()
        self.combo_btn = QPushButton("组合寻优(196种)")
        self.combo_btn.setToolTip(OPTIMIZE_NOTE)
        self.combo_btn.clicked.connect(self._on_combo_click)
        bottom.addWidget(self.combo_btn)
        self.shot_btn = QPushButton("截图")
        self.shot_btn.setToolTip("把整个排行窗口截图保存为 PNG")
        self.shot_btn.clicked.connect(self._on_shot_click)
        bottom.addWidget(self.shot_btn)
        self.combo_note = QLabel(OPTIMIZE_NOTE)   # 常驻说明：注明本次按钮枚举了哪些条件
        self.combo_note.setStyleSheet("color:#555; font-size:12px;")
        self.combo_note.setWordWrap(True)
        bottom.addWidget(self.combo_note, 1)
        lay.addLayout(bottom)

        lay.addWidget(QLabel("点击任意行可打开该组合的K线买卖点窗口（同时只保留一个K线窗口）。"))

    def _on_combo_click(self):
        key = self.scope_box.currentData()
        if key is None:
            QMessageBox.information(
                self, "组合排行",
                "还没有可用的作用域。先在回测界面某行点「确定」跑一次，再来点组合排行。")
            return
        self.combo_requested.emit(key)

    def set_combo_running(self, running):
        """计算期间置灰按钮并显示「计算中…」，完成后恢复。"""
        self.combo_btn.setEnabled(not running)
        self.combo_btn.setText("计算中…" if running else "组合寻优(196种)")

    def _on_delete_click(self):
        idx = self.scope_box.currentIndex()
        key = self.scope_box.itemData(idx)
        if key is None:
            return
        label = self.scope_box.itemText(idx)
        if QMessageBox.question(
                self, "删除排行", f"确定删除「{label}」的整份排行吗？此操作不可撤销。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.scope_deleted.emit(key)

    def _on_combo(self, idx):
        key = self.scope_box.itemData(idx)
        if key is not None:
            self.scope_changed.emit(key)

    def _on_note_click(self):
        idx = self.scope_box.currentIndex()
        key = self.scope_box.itemData(idx)
        if key is None:
            return
        cur = self._notes.get(key, "")
        text, ok = QInputDialog.getText(self, "备注", "给这份排行添加备注：", text=cur)
        if ok:
            self.note_edited.emit(key, text.strip())

    def _on_shot_click(self):
        """截取整个排行窗口，直接保存到配置目录（与 json 同目录）。文件名用当前下拉选中的标签。"""
        pix = self.grab()
        label = self.scope_box.currentText().strip()
        for ch in '\\/:*?"<>|·':
            label = label.replace(ch, "_")
        prefix = label or "收益排行"
        name = f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}.png"
        path = os.path.join(_BASE_DIR, name)
        if pix.save(path, "PNG"):
            QMessageBox.information(self, "截图", f"已保存到：\n{path}")
        else:
            QMessageBox.warning(self, "截图", "保存失败。")

    def _on_other_activated(self, idx):
        """「其他股票」下拉当菜单用：选中即发信号新开窗口，随后复位到占位项。"""
        key = self.other_box.itemData(idx)
        self.other_box.setCurrentIndex(0)
        if key:
            self.open_other.emit(key)

    def set_others(self, items):
        """填充「其他股票」下拉。items：[(scope_key, 标签)]；首项为占位提示（data=None）。"""
        self.other_box.blockSignals(True)
        self.other_box.clear()
        self.other_box.addItem("其他股票的排行…", None)
        for key, label in items:
            self.other_box.addItem(label, key)
        self.other_box.setCurrentIndex(0)
        self.other_box.blockSignals(False)

    def set_scopes(self, items, current_key, notes=None):
        """items：[(scope_key, 显示标签)]；current_key：默认选中项；notes：scope_key→备注（用于编辑回填）。
        填充时屏蔽信号避免误触发。"""
        self._notes = dict(notes or {})
        self.scope_box.blockSignals(True)
        self.scope_box.clear()
        for key, label in items:
            self.scope_box.addItem(label, key)
        if current_key is not None:
            i = self.scope_box.findData(current_key)
            if i >= 0:
                self.scope_box.setCurrentIndex(i)
        self.scope_box.blockSignals(False)

    def refresh(self, subtitle, entries):
        """存下当前作用域条目，按窗内选定的排序依据/过滤重画表格。
        entries 顺序须与 rank_store 里一致（点行回原始索引即时重算 K线用）。"""
        self._subtitle = subtitle
        self._entries = list(entries or [])
        self._need_resize = True   # 数据集变了，重画时量一次列宽
        self._render()

    def _render(self):
        """按当前排序依据 + 「仅≥3笔」过滤重画；维护 表格行→原始索引 映射。
        纯排序/过滤（数据集不变）不重算列宽，避免 196 行逐格测量造成卡顿。"""
        key = self.SORT_KEYS[self.sort_combo.currentIndex()][1]
        idxs = [i for i, e in enumerate(self._entries)
                if not (self.only3.isChecked() and e.get("trade_count", 0) < 3)]
        idxs.sort(key=lambda i: self._entries[i].get(key, 0), reverse=True)
        self._view = idxs
        self.subtitle.setText(self._subtitle)
        red, green, gray = "#e74c3c", "#27ae60", "#888888"

        def color(v):
            return QColor(red if v > 0 else (green if v < 0 else gray))

        self.tbl.setUpdatesEnabled(False)   # 批量填充期间关重绘，填完一次性刷新
        try:
            self.tbl.setRowCount(len(idxs))
            for r, i in enumerate(idxs):
                e = self._entries[i]
                ret, pnl = e["return_pct"], e["total_pnl"]
                score = e.get("score")
                wr = e.get("win_rate")
                cells = [
                    (str(r + 1), None),
                    (e["buy_desc"], None),
                    (e["sell_desc"], None),
                    (str(e["trade_count"]), None),
                    ("--" if wr is None else f"{wr:.1f}", None),
                    (f"{ret:+.2f}", color(ret)),
                    ("--" if score is None else f"{score:+.2f}", None if score is None else color(score)),
                    (f"{pnl:+,.2f}", color(pnl)),
                ]
                for c, (text, fg) in enumerate(cells):
                    item = QTableWidgetItem(text)
                    if fg:
                        item.setForeground(fg)
                    self.tbl.setItem(r, c, item)
            if self._need_resize:   # 只在数据集变化后量一次列宽（拉伸列 1/2 不受影响）
                for c in (0, 3, 4, 5, 6, 7):
                    self.tbl.resizeColumnToContents(c)
                self._need_resize = False
        finally:
            self.tbl.setUpdatesEnabled(True)

    def _on_cell_clicked(self, r, _c):
        """把表格行号映射回原始条目索引再发出，避免排序/过滤后错位。"""
        if 0 <= r < len(self._view):
            self.row_activated.emit(self._view[r])


class AnalyzeDialog(QDialog):
    """配置分析（纯本地统计，零 token）：勾选若干作用域榜单，按「买入策略」归并。
    每种买法给出：样本数、平均收益率、胜率、最好/最差、中位数。
    胜率 = 组内条目收益率跑赢其所属榜单「区间买入持有」基准的占比。
    """

    def __init__(self, scopes, parent=None):
        # scopes：[(key, label, entry_count, hold_pct, entries)] —— 只读快照
        super().__init__(parent)
        self.setWindowTitle("配置分析 · 按买入策略")
        self.resize(840, 580)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._scopes = scopes
        lay = QVBoxLayout(self)

        lay.addWidget(QLabel("勾选要分析的排行（默认全选），点「开始分析」按买入策略归并统计（纯本地计算）："))

        tools = QHBoxLayout()
        btn_all = QPushButton("全选"); btn_all.setFixedWidth(70)
        btn_none = QPushButton("全不选"); btn_none.setFixedWidth(70)
        btn_all.clicked.connect(lambda: self._check_all(True))
        btn_none.clicked.connect(lambda: self._check_all(False))
        tools.addWidget(btn_all)
        tools.addWidget(btn_none)
        tools.addStretch()
        tools.addWidget(QLabel("归并维度："))
        self.dim_box = QComboBox()
        for label, key in (("按买入策略", "buy"), ("按卖出策略", "sell"), ("按买卖组合", "combo")):
            self.dim_box.addItem(label, key)
        self.dim_box.activated.connect(lambda _i: self._run())
        tools.addWidget(self.dim_box)
        self.run_btn = QPushButton("开始分析")
        self.run_btn.clicked.connect(self._run)
        tools.addWidget(self.run_btn)
        lay.addLayout(tools)

        # 勾选表：勾选(在第0列文字前) | 条目数 | 买入持有%
        self.pick = QTableWidget(0, 3)
        self.pick.setHorizontalHeaderLabels(["排行（股票 · 时间段）", "条目数", "买入持有%"])
        self.pick.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.pick.verticalHeader().setVisible(False)
        self.pick.setRowCount(len(scopes))
        for r, (_key, label, cnt, hold, _entries) in enumerate(scopes):
            it = QTableWidgetItem(label)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)
            self.pick.setItem(r, 0, it)
            self.pick.setItem(r, 1, QTableWidgetItem(str(cnt)))
            self.pick.setItem(r, 2, QTableWidgetItem(f"{hold:+.2f}" if hold is not None else "—"))
        ph = self.pick.horizontalHeader()
        ph.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        ph.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        ph.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        lay.addWidget(self.pick, 1)

        self.subtitle = QLabel("")
        self.subtitle.setStyleSheet("color:#555; font-size:12px;")
        self.subtitle.setWordWrap(True)
        lay.addWidget(self.subtitle)

        self.result = QTableWidget(0, 7)
        self.result.setHorizontalHeaderLabels(
            ["买入策略", "样本数", "平均收益率%", "胜率%", "最好%", "最差%", "中位数%"])
        self.result.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.result.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.result.verticalHeader().setVisible(False)
        lay.addWidget(self.result, 2)

        if scopes:
            self._run()   # 默认全选，进来直接出一版

    def _check_all(self, checked):
        st = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for r in range(self.pick.rowCount()):
            self.pick.item(r, 0).setCheckState(st)

    @staticmethod
    def _median(xs):
        s = sorted(xs)
        n = len(s)
        if n == 0:
            return 0.0
        m = n // 2
        return s[m] if n % 2 else (s[m - 1] + s[m]) / 2

    def _run(self):
        dim = self.dim_box.currentData()          # buy / sell / combo
        dim_name = {"buy": "买入策略", "sell": "卖出策略", "combo": "买卖组合"}[dim]
        # 收集勾选榜单的所有条目，带上各自「买入持有」基准判定胜负
        rows = []            # (归并键, return_pct, win_bool)
        n_scope = 0
        for r, (_key, _label, _cnt, hold, entries) in enumerate(self._scopes):
            if self.pick.item(r, 0).checkState() != Qt.CheckState.Checked:
                continue
            n_scope += 1
            base = hold if hold is not None else 0.0
            for e in entries:
                ret = e.get("return_pct", 0.0)
                buy, sell = e.get("buy_desc", "？"), e.get("sell_desc", "？")
                gkey = {"buy": buy, "sell": sell, "combo": f"{buy} → {sell}"}[dim]
                rows.append((gkey, ret, ret > base))

        groups = {}
        for gkey, ret, win in rows:
            groups.setdefault(gkey, []).append((ret, win))
        stats = []
        for gkey, items in groups.items():
            rets = [x[0] for x in items]
            wins = sum(1 for x in items if x[1])
            stats.append({
                "key": gkey, "n": len(items),
                "mean": sum(rets) / len(rets),
                "winr": 100.0 * wins / len(items),
                "best": max(rets), "worst": min(rets),
                "med": self._median(rets),
            })
        stats.sort(key=lambda s: s["mean"], reverse=True)

        self.result.setHorizontalHeaderLabels(
            [dim_name, "样本数", "平均收益率%", "胜率%", "最好%", "最差%", "中位数%"])
        self.subtitle.setText(
            f"共分析 {n_scope} 个榜单、{len(rows)} 个条目，归并出 {len(stats)} 种{dim_name}"
            f"（按平均收益率降序；胜率=跑赢各自「买入持有」基准的占比）。")

        red, green, gray = "#e74c3c", "#27ae60", "#888888"
        col = lambda v: red if v > 0 else (green if v < 0 else gray)
        self.result.setRowCount(len(stats))
        for r, s in enumerate(stats):
            cells = [
                (s["key"], None),
                (str(s["n"]), None),
                (f"{s['mean']:+.2f}", QColor(col(s["mean"]))),
                (f"{s['winr']:.0f}", None),
                (f"{s['best']:+.2f}", QColor(col(s["best"]))),
                (f"{s['worst']:+.2f}", QColor(col(s["worst"]))),
                (f"{s['med']:+.2f}", QColor(col(s["med"]))),
            ]
            for c, (text, fg) in enumerate(cells):
                it = QTableWidgetItem(text)
                if fg:
                    it.setForeground(fg)
                self.result.setItem(r, c, it)
        rh = self.result.horizontalHeader()
        rh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, 7):
            rh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)


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
            vol_lbl = QLabel("--")
            vol_lbl.setFont(self._make_font(9))
            vol_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            vol_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            row.addWidget(price_lbl)
            row.addWidget(chg_lbl)
            row.addWidget(vol_lbl)
            self._main_layout.addLayout(row)
            self._rows[code] = (price_lbl, chg_lbl, vol_lbl)
            if code in self._data:
                self._apply_data(code, *self._data[code])

        self.adjustSize()

    def _apply_data(self, code, price, change_pct, vol_ratio=None):
        """把数据写入对应行的 Label（价格 + 涨跌幅 + 量比）"""
        if code not in self._rows:
            return
        price_lbl, chg_lbl, vol_lbl = self._rows[code]
        price_lbl.setText(f"{price:.4f}")
        fc = self._font_color
        chg_lbl.setText(f"{change_pct:+.2f}%")
        chg_lbl.setStyleSheet(f"color: {fc}; background: transparent;")
        if vol_ratio is not None and vol_ratio >= 0:
            vol_lbl.setText(f"{vol_ratio:.2f}")
        else:
            vol_lbl.setText("--")


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

    def update_stock(self, code, price, change_pct, vol_ratio=None):
        self._data[code] = (price, change_pct, vol_ratio)
        self._apply_data(code, price, change_pct, vol_ratio)
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
    result signal: (code, name, price, change_pct, ma5, ma10, ma20, ma30, ma60, vols_json, closes_json, vol_ratio)
    error  signal: (code, error_msg)
    """
    result = pyqtSignal(str, str, float, float, float, float, float, float, float, str, str, float)
    error = pyqtSignal(str, str)

    def __init__(self, codes):
        super().__init__()
        self.codes = list(codes)

    def run(self):
        for code in self.codes:
            try:
                name, price, change_pct, ma5, ma10, ma20, ma30, ma60, volumes, closes, vol_ratio = fetch_stock_data(code)
                self.result.emit(code, name, price, change_pct, ma5, ma10, ma20, ma30, ma60,
                                 json.dumps(volumes), json.dumps(closes),
                                 vol_ratio if vol_ratio is not None else -1.0)
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


class BacktestWorker(QThread):
    """后台线程：拉日K + 跑回测，通过 signal 回主线程。
    result signal: (row_id, code, name, report_json)
    error  signal: (row_id, code, msg)
    """
    result = pyqtSignal(int, str, str, str)
    error = pyqtSignal(int, str, str)

    def __init__(self, row_id, code, mode, days, start, end, buy_conds, sell_conds):
        super().__init__()
        self.row_id = row_id
        self.code = code
        self.mode = mode
        self.days = days
        self.start_date = start   # 不能叫 self.start：会覆盖 QThread.start() 方法
        self.end_date = end
        self.buy_conds = buy_conds
        self.sell_conds = sell_conds

    def run(self):
        try:
            # 指标预热：取均线周期与 MACD 等效周期(34=EMA26+DEA9)的最大值，多留若干 trading day
            ma_params = [c["param"] for c in self.buy_conds + self.sell_conds
                         if c["type"] in ("ma_above", "ma_below")]
            if any(c["type"] in ("macd_golden", "macd_death")
                   for c in self.buy_conds + self.sell_conds):
                ma_params.append(34)
            pad = (max(ma_params) if ma_params else 5) * 2 + 20

            # 缓存 key 只认「时间段」（与买卖条件无关）：同一时间段重复点确定直接复用上次数据、
            # 不再联网；只有时间段变化（或隔天数据可能更新）才重新请求。
            today = time.strftime("%Y-%m-%d")
            if self.mode == "range":
                cache_key = ("range", self.code, self.start_date, self.end_date, today)
            else:
                cache_key = ("days", self.code, self.days, today)

            cached = _backtest_kline_cache.get(cache_key)
            if cached is not None:
                name, series = cached
            else:
                if self.mode == "range":
                    sd = datetime.datetime.strptime(self.start_date, "%Y-%m-%d") - datetime.timedelta(days=pad * 2)
                    name, series = fetch_kline_daily(self.code, sd.strftime("%Y-%m-%d"), self.end_date)
                else:
                    name, series = fetch_kline_daily(self.code, count=self.days + pad)
                _backtest_kline_cache[cache_key] = (name, series)
            dates, closes, start_idx = _resolve_backtest_window(series, self.mode, self.days, self.start_date)
            report = run_backtest(dates, closes, self.buy_conds, self.sell_conds, start_idx)
            report["window"] = {"first": dates[start_idx], "last": dates[-1]}
            # 回测区间的收盘序列，供 K线窗口画折线+标买卖点（不必再联网）
            report["series"] = [[dates[j], closes[j]] for j in range(start_idx, len(closes))]
            # 时间段元信息：主线程据此判定「收益排行」榜单作用域（换股票/改时间段则清空）
            report["meta"] = {"mode": self.mode, "days": self.days,
                              "start": self.start_date, "end": self.end_date}
            # 区间买入持有收益：区间首日买入、持有到截止日（纯基准，与策略无关）
            base = closes[start_idx]
            report["hold_pct"] = ((closes[-1] / base - 1) * 100) if base else 0.0
            # 买卖条件随报告带回：收益排行只持久化条件(而非 trades)，点行时据此即时重算
            report["buy_conds"] = self.buy_conds
            report["sell_conds"] = self.sell_conds
            self.result.emit(self.row_id, self.code, name, json.dumps(report))
        except Exception as e:
            self.error.emit(self.row_id, self.code, str(e))


class ComboWorker(QThread):
    """后台线程：对某作用域(股票+时间段)只拉一次日K，在内存里跑完寻优网格全部 196 种组合，一次性发回。
    联网最多发生一次（缓存命中则一次都不发）；196 次 run_backtest 全是本地纯计算。
    done  signal: (scope_key, name, hold_pct, entries_json)  entries 含收益率/胜率/回撤/综合评分+买卖条件（不含 trades/series）
    error signal: (scope_key, msg)
    """
    done = pyqtSignal(str, str, float, str)
    error = pyqtSignal(str, str)

    def __init__(self, scope_key, code, mode, days, start, end):
        super().__init__()
        self.scope_key = scope_key
        self.code = code
        self.mode = mode
        self.days = days
        self.start_date = start   # 不能叫 self.start：会覆盖 QThread.start()
        self.end_date = end

    def run(self):
        try:
            combos = enumerate_optimize_combos()
            # 预热：网格里最大均线60、MACD等效34，取足够冗余
            pad = max(60, 34) * 2 + 20
            today = time.strftime("%Y-%m-%d")
            if self.mode == "range":
                cache_key = ("range", self.code, self.start_date, self.end_date, today)
            else:
                cache_key = ("days", self.code, self.days, today)

            cached = _backtest_kline_cache.get(cache_key)
            # days 模式下若缓存序列过短（此前用小 pad 拉的），预热不足会漏算长均线 → 重取一次
            too_short = (self.mode == "days" and cached is not None
                         and len(cached[1]) < self.days + pad)
            if cached is not None and not too_short:
                name, series = cached
            else:
                if self.mode == "range":
                    sd = datetime.datetime.strptime(self.start_date, "%Y-%m-%d") - datetime.timedelta(days=pad * 2)
                    name, series = fetch_kline_daily(self.code, sd.strftime("%Y-%m-%d"), self.end_date)
                else:
                    name, series = fetch_kline_daily(self.code, count=self.days + pad)
                _backtest_kline_cache[cache_key] = (name, series)

            dates, closes, start_idx = _resolve_backtest_window(series, self.mode, self.days, self.start_date)
            base = closes[start_idx]
            hold_pct = ((closes[-1] / base - 1) * 100) if base else 0.0   # 区间买入持有收益（基准）
            # 只回摘要 + 买卖条件（不含 trades/series）：点行时据条件即时重算 K线买卖点
            entries = []
            for buy_conds, sell_conds in combos:
                rep = run_backtest(dates, closes, buy_conds, sell_conds, start_idx)
                st, s = rep["stats"], rep["summary"]
                entries.append({
                    "buy_desc": st["buy_desc"], "sell_desc": st["sell_desc"],
                    "trade_count": st["trade_count"], "win_rate": st["win_rate"],
                    "max_drawdown": st["max_drawdown"],
                    "return_pct": s["return_pct"], "total_pnl": s["total_pnl"],
                    "score": optimize_score(s["return_pct"], st["win_rate"], st["trade_count"]),
                    "buy_conds": buy_conds, "sell_conds": sell_conds,
                })
            self.done.emit(self.scope_key, name, hold_pct, json.dumps(entries))
        except Exception as e:
            self.error.emit(self.scope_key, str(e))


class AIWorker(QThread):
    """后台线程：采集技术面 + 调用大模型诊股。done(name, text) / error(msg)。"""
    done = pyqtSignal(str, str)
    error = pyqtSignal(str)

    def __init__(self, config, code, extra_context=""):
        super().__init__()
        self.config = config
        self.code = code
        self.extra_context = extra_context

    def run(self):
        try:
            name, ctx = _ai_stock_context(self.code)
            if self.extra_context:
                ctx += "\n" + self.extra_context
            text = ai_chat(self.config, AI_DIAGNOSE_SYSTEM, ctx)
            self.done.emit(name, text)
        except Exception as e:
            self.error.emit(str(e))


class NameLookupWorker(QThread):
    """后台查股票名（search_secid 有缓存），done 发出 name（查不到发 ''）。"""
    done = pyqtSignal(str)

    def __init__(self, code):
        super().__init__()
        self.code = code

    def run(self):
        try:
            _secid, name = search_secid(self.code)
        except Exception:
            name = ""
        self.done.emit(name)


class _CodeCell(QWidget):
    """股票代码单元格：左输入框，右显示股票名（输入完成后台查一次）。value() 返回代码。"""
    changed = pyqtSignal()

    def __init__(self, code=""):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(2)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("如 600519")
        self.edit.setFixedWidth(90)
        self.name_lbl = QLabel("")
        self.name_lbl.setStyleSheet("color:#666;")
        lay.addWidget(self.edit)
        lay.addWidget(self.name_lbl)
        self._worker = None
        self.edit.editingFinished.connect(self._lookup)
        self.edit.editingFinished.connect(self.changed)
        if code:                       # 从配置回填：填入并后台查一次股票名
            self.edit.setText(code)
            self._lookup()

    def _lookup(self):
        code = self.edit.text().strip().upper()
        if len(code) < 2:
            self.name_lbl.setText("")
            return
        self.name_lbl.setText("查询中…")
        self._worker = NameLookupWorker(code)
        self._worker.done.connect(self._on_name)
        self._worker.start()

    def _on_name(self, name):
        self.name_lbl.setText(name if name else "未找到")

    def set_name(self, name):
        if name:
            self.name_lbl.setText(name)

    def value(self):
        return self.edit.text().strip().upper()


class _TimeTypeCell(QWidget):
    """时间类型单元格：下拉切换「最近N天 / 起止日期」，返回 (mode, days, start, end)。"""
    changed = pyqtSignal()

    def __init__(self, mode="days", days=60, start="", end=""):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(4)
        self.combo = QComboBox()
        self.combo.addItems(["最近N天", "起止日期"])
        lay.addWidget(self.combo)

        self.stack = QStackedWidget()
        # page0: 最近N天
        page_days = QWidget()
        dl = QHBoxLayout(page_days)
        dl.setContentsMargins(0, 0, 0, 0)
        self.days_spin = QSpinBox()
        self.days_spin.setRange(5, 1000)
        self.days_spin.setValue(days or 60)
        self.days_spin.setFixedWidth(70)
        dl.addWidget(self.days_spin)
        dl.addWidget(QLabel("天"))
        # page1: 起止日期
        page_range = QWidget()
        rl = QHBoxLayout(page_range)
        rl.setContentsMargins(0, 0, 0, 0)
        today = QDate.currentDate()
        sd = QDate.fromString(start, "yyyy-MM-dd") if start else today.addMonths(-3)
        ed = QDate.fromString(end, "yyyy-MM-dd") if end else today
        self.start_edit = QDateEdit(sd if sd.isValid() else today.addMonths(-3))
        self.end_edit = QDateEdit(ed if ed.isValid() else today)
        for de in (self.start_edit, self.end_edit):
            de.setCalendarPopup(True)
            de.setDisplayFormat("yyyy-MM-dd")
        rl.addWidget(self.start_edit)
        rl.addWidget(QLabel("~"))
        rl.addWidget(self.end_edit)

        self.stack.addWidget(page_days)
        self.stack.addWidget(page_range)
        lay.addWidget(self.stack)
        self.combo.currentIndexChanged.connect(self.stack.setCurrentIndex)
        if mode == "range":
            self.combo.setCurrentIndex(1)      # 联动 stack 切到起止日期页
        # 任意变更 → 通知外层持久化
        self.combo.currentIndexChanged.connect(self.changed)
        self.days_spin.valueChanged.connect(self.changed)
        self.start_edit.dateChanged.connect(self.changed)
        self.end_edit.dateChanged.connect(self.changed)

    def value(self):
        if self.combo.currentIndex() == 0:
            return "days", self.days_spin.value(), "", ""
        return ("range", 0,
                self.start_edit.date().toString("yyyy-MM-dd"),
                self.end_edit.date().toString("yyyy-MM-dd"))


class _ConditionsCell(QWidget):
    """可增删的多条件单元格。kind='buy'/'sell' 决定可选条件类型。
    value() 返回 [{'type':..., 'param':int}, ...]，多条件为「同时满足」(AND)。
    conditions_changed 在增删条件时发出，供外层重算行高。
    """
    conditions_changed = pyqtSignal()

    def __init__(self, kind="buy", conds=None):
        super().__init__()
        self.kind = kind
        # 该 kind 可选的条件类型按「且/或」分组：[(type, 显示名), ...]
        all_types = [(t, COND_TYPES[t][1].replace("{p}", "X"))
                     for t, (owner, _tpl) in COND_TYPES.items() if owner == kind]
        self._and_types = [x for x in all_types if x[0] not in OR_TYPES]
        self._or_types = [x for x in all_types if x[0] in OR_TYPES]
        self._has_or = bool(self._or_types)   # 买入侧才有 OR 分组；卖出侧退化为单组

        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(2, 2, 2, 2)
        self._outer.setSpacing(2)

        self._rows = []   # [(row_widget, type_combo, param_spin, group)] group∈{'and','or'}

        # ── 「且」分组 ──
        if self._has_or:
            self._outer.addWidget(self._group_label("且（全部满足）"))
        self._and_box = QVBoxLayout()
        self._and_box.setSpacing(2)
        self._outer.addLayout(self._and_box)
        add_and = QPushButton("＋ 添加“且”条件" if self._has_or else "＋ 添加条件")
        add_and.setFixedHeight(22)
        add_and.clicked.connect(lambda: self._add_condition("and"))
        self._outer.addWidget(add_and)

        # ── 「或」分组（仅买入侧）──
        if self._has_or:
            line = QFrame()
            line.setFrameShape(QFrame.Shape.HLine)
            line.setStyleSheet("color:#ccc;")
            self._outer.addWidget(line)
            self._outer.addWidget(self._group_label("或（满足即买）"))
            self._or_box = QVBoxLayout()
            self._or_box.setSpacing(2)
            self._outer.addLayout(self._or_box)
            add_or = QPushButton("＋ 添加“或”条件")
            add_or.setFixedHeight(22)
            add_or.clicked.connect(lambda: self._add_condition("or"))
            self._outer.addWidget(add_or)

        if conds:                       # 从配置回填：按类型归入对应分组
            for c in conds:
                g = "or" if c.get("type") in OR_TYPES else "and"
                self._add_condition(g, default_type=c.get("type"), param=c.get("param"))
        else:                           # 默认给一条「且」条件
            self._add_condition("and", default_type=self._and_types[0][0])

    def _group_label(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet("color:#666; font-size:11px;")
        return lbl

    def _add_condition(self, group="and", default_type=None, param=None):
        types = self._or_types if group == "or" else self._and_types
        if not types:                   # 卖出侧无 OR 类型时的保护
            return
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(3)

        combo = QComboBox()
        for _t, label in types:
            combo.addItem(label)
        if default_type:
            idx = next((i for i, (t, _l) in enumerate(types) if t == default_type), 0)
            combo.setCurrentIndex(idx)

        spin = QSpinBox()
        spin.setRange(1, 250)
        spin.setValue(param if param is not None else (5 if self.kind == "buy" else 10))
        spin.setMinimumWidth(66)

        # 无参数类型（MACD 金叉/死叉、突破上次卖点）：参数框置灰禁用，值无意义
        def _sync_spin():
            ctype = types[combo.currentIndex()][0]
            spin.setEnabled(ctype not in PARAMLESS_TYPES)
        _sync_spin()

        del_btn = QPushButton("－")
        del_btn.setFixedSize(24, 22)
        del_btn.clicked.connect(lambda: self._del_condition(row))

        # 类型/参数变更也通过 conditions_changed 通知外层（既重算行高也触发持久化）
        combo.currentIndexChanged.connect(_sync_spin)
        combo.currentIndexChanged.connect(self.conditions_changed)
        spin.valueChanged.connect(self.conditions_changed)

        rl.addWidget(combo)
        rl.addWidget(spin)
        rl.addWidget(del_btn)
        rl.addStretch()

        box = self._or_box if group == "or" else self._and_box
        box.addWidget(row)
        self._rows.append((row, combo, spin, group))
        self._outer.activate()      # 立即重算布局，让 sizeHint 反映新增的条件行
        self.updateGeometry()
        self.conditions_changed.emit()

    def _del_condition(self, row):
        for i, (w, _c, _s, _g) in enumerate(self._rows):
            if w is row:
                self._rows.pop(i)
                w.setParent(None)
                w.deleteLater()
                break
        self._outer.activate()
        self.updateGeometry()
        self.conditions_changed.emit()

    def value(self):
        out = []
        for _w, combo, spin, group in self._rows:
            types = self._or_types if group == "or" else self._and_types
            ctype = types[combo.currentIndex()][0]
            out.append({"type": ctype, "param": spin.value()})
        return out


class AISettingsDialog(QDialog):
    """AI 设置：选择接口类型（Anthropic / OpenAI 兼容）、填写 base_url / api_key / model，保存到 config。"""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("AI 设置")
        self.resize(480, 260)
        lay = QVBoxLayout(self)

        row1 = QHBoxLayout()
        lb0 = QLabel("接口类型：")
        lb0.setFixedWidth(80)
        row1.addWidget(lb0)
        self.provider = QComboBox()
        self.provider.addItem("Anthropic (Claude)", "anthropic")
        self.provider.addItem("OpenAI 兼容", "openai")
        self.provider.setCurrentIndex(0 if (config.get("ai_provider") or "anthropic") == "anthropic" else 1)
        self.provider.currentIndexChanged.connect(self._on_provider)
        row1.addWidget(self.provider)
        row1.addStretch()
        lay.addLayout(row1)

        self.base_url = QLineEdit(config.get("ai_base_url", ""))
        self.api_key = QLineEdit(config.get("ai_api_key", ""))
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.model = QLineEdit(config.get("ai_model", ""))
        for label, w, tip in [
            ("Base URL：", self.base_url,
             "留空用默认。Anthropic 填到域名即可，程序自动补 /v1/messages；OpenAI兼容需含 /v1，程序补 /chat/completions"),
            ("API Key：", self.api_key, "你的密钥，仅保存在本地配置文件 stock_monitor_config.json"),
            ("模型：", self.model, "留空用默认。如 claude-opus-5 / gpt-4o / deepseek-chat / qwen-plus 等"),
        ]:
            r = QHBoxLayout()
            lb = QLabel(label)
            lb.setFixedWidth(80)
            r.addWidget(lb)
            w.setToolTip(tip)
            r.addWidget(w)
            lay.addLayout(r)

        self.hint = QLabel()
        self.hint.setStyleSheet("color:#888; font-size:11px;")
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)
        self._on_provider()

        btns = QHBoxLayout()
        btns.addStretch()
        ok = QPushButton("保存")
        ok.clicked.connect(self._save)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        lay.addLayout(btns)

    def _on_provider(self):
        p = self.provider.currentData()
        d = AI_DEFAULTS[p]
        self.base_url.setPlaceholderText(f"默认 {d['base_url']}")
        self.model.setPlaceholderText(f"默认 {d['model']}")
        if p == "anthropic":
            self.hint.setText("Anthropic：请求 {base}/v1/messages，鉴权头 x-api-key。国内直连易超时，可填中转域名。")
        else:
            self.hint.setText("OpenAI兼容：请求 {base}/chat/completions，鉴权 Bearer。支持通义/DeepSeek/智谱等兼容端点。")

    def _save(self):
        self.config["ai_provider"] = self.provider.currentData()
        self.config["ai_base_url"] = self.base_url.text().strip()
        self.config["ai_api_key"] = self.api_key.text().strip()
        self.config["ai_model"] = self.model.text().strip()
        save_config(self.config)
        self.accept()


class AIDialog(QDialog):
    """AI 诊股结果窗（非模态）：显示大模型对该股票技术面的分析，可重新分析 / 打开设置。"""

    def __init__(self, config, code, name_hint="", extra_context="", parent=None):
        super().__init__(parent)
        self.config = config
        self.code = code
        self.extra_context = extra_context
        self._worker = None
        self.setWindowTitle(f"AI 诊股 · {name_hint or code}")
        self.resize(560, 560)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        lay = QVBoxLayout(self)
        self.header = QLabel(f"{name_hint or ''}({code})")
        self.header.setStyleSheet("font-weight:bold;")
        lay.addWidget(self.header)
        self.body = QTextEdit()
        self.body.setReadOnly(True)
        lay.addWidget(self.body, stretch=1)
        disclaimer = QLabel("以上为AI基于技术指标的分析，仅供参考，不构成投资建议。")
        disclaimer.setStyleSheet("color:#c0392b; font-size:11px;")
        disclaimer.setWordWrap(True)
        lay.addWidget(disclaimer)

        btns = QHBoxLayout()
        self.btn_retry = QPushButton("重新分析")
        self.btn_retry.clicked.connect(self.start)
        btn_set = QPushButton("AI 设置")
        btn_set.clicked.connect(self._open_settings)
        btns.addWidget(self.btn_retry)
        btns.addWidget(btn_set)
        btns.addStretch()
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        btns.addWidget(btn_close)
        lay.addLayout(btns)
        self.start()

    def start(self):
        if self._worker is not None:
            return
        self.body.setPlainText("正在分析，请稍候…（首次调用大模型可能需要数秒到数十秒）")
        self.btn_retry.setEnabled(False)
        self._worker = AIWorker(self.config, self.code, self.extra_context)
        self._worker.done.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_done(self, name, text):
        if name:
            self.header.setText(f"{name}({self.code})")
            self.setWindowTitle(f"AI 诊股 · {name}")
        self.body.setPlainText(text)

    def _on_error(self, msg):
        self.body.setPlainText(
            f"分析失败：{msg}\n\n"
            "请检查「AI 设置」中的接口类型、Base URL、API Key、模型是否正确，以及网络/中转是否可用。")

    def _on_finished(self):
        self._worker = None
        self.btn_retry.setEnabled(True)

    def _open_settings(self):
        AISettingsDialog(self.config, self).exec()


class AnalysisWindow(QWidget):
    """股票分析（回测）窗口：多行表格，每行独立的买入/卖出多条件策略回测。"""

    def __init__(self, config=None):
        super().__init__()
        self.setWindowTitle("股票分析 · 策略回测")
        self.resize(1000, 460)
        self.config = config if config is not None else load_config()
        self._loading = False    # 回填已存配置时置 True，避免触发保存
        self._next_row_id = 0
        self._workers = {}       # row_id → BacktestWorker（持引用防 GC）
        self._code_cells = {}    # row_id → _CodeCell（回测返回后回填股票名）
        # 收益排行：按作用域(股票+时间段)持久化到本地 config；每只/每时间段各存一张榜
        self._rank_store = self.config.get("rank_store") or {}   # scope_key → {name, meta, entries}
        self._rank_active_scope = self.config.get("rank_last_scope")  # 最近一次回测的作用域
        self._kline_win = None   # 唯一 K线窗口（非模态单例）
        self._rank_wins = []     # 收益排行窗口（可多开）；每个窗口自带 filter_code / view_scope
        self._combo_worker = None  # 「组合排行」后台线程
        self._combo_win = None     # 发起「组合排行」的那个排行窗口（用于回收 loading 状态）
        self._open_worker = None   # 点排行某行时即时重算该组合的后台线程
        self._analyze_win = None   # 「分析配置」窗口（非模态单例）
        self._ai_wins = []            # AI 诊股窗口（可多开）
        self._save_timer = QTimer(self)      # 变更防抖：多次快速改动只落盘一次
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._save_rows)

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        btn_add = QPushButton("添加一行")
        btn_add.setFixedWidth(90)
        btn_add.clicked.connect(self._add_row)
        top.addWidget(btn_add)
        btn_analyze = QPushButton("分析配置")
        btn_analyze.setFixedWidth(90)
        btn_analyze.setToolTip("按买入策略归并统计所有排行的收益率/胜率（纯本地计算，不消耗流量）")
        btn_analyze.clicked.connect(self._open_analyze)
        top.addWidget(btn_analyze)
        top.addWidget(QLabel("每列多条件为「同时满足」；按 10000 股为买卖基数，毛收益（不计手续费）"))
        top.addStretch()
        lay.addLayout(top)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["股票代码 / 操作", "时间类型", "买入策略", "卖出策略"])
        h = self.table.horizontalHeader()
        for i in range(4):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setDefaultSectionSize(56)
        lay.addWidget(self.table)

        saved = self.config.get("analysis_rows") or []
        if saved:                        # 回填上次保存的各行
            self._loading = True
            for d in saved:
                self._add_row(d)
            self._loading = False
        else:
            self._add_row()              # 默认给一行

    def _add_row(self, data=None):
        d = data if isinstance(data, dict) else {}
        r = self.table.rowCount()
        self.table.insertRow(r)
        code_cell = _CodeCell(d.get("code", ""))
        time_cell = _TimeTypeCell(d.get("mode", "days"), d.get("days", 60),
                                  d.get("start", ""), d.get("end", ""))
        self.table.setCellWidget(r, 1, time_cell)
        buy_cell = _ConditionsCell("buy", d.get("buy"))
        sell_cell = _ConditionsCell("sell", d.get("sell"))
        buy_cell.conditions_changed.connect(self._resize_rows_deferred)
        sell_cell.conditions_changed.connect(self._resize_rows_deferred)
        # 任意单元格变更 → 防抖持久化
        code_cell.changed.connect(self._schedule_save)
        time_cell.changed.connect(self._schedule_save)
        buy_cell.conditions_changed.connect(self._schedule_save)
        sell_cell.conditions_changed.connect(self._schedule_save)
        # 记录该行当前作用域快照；股票/时间段变化时删掉旧作用域的排行（含本地）
        time_cell._rank_scope = self._row_scope(code_cell, time_cell)
        code_cell.changed.connect(lambda cc=code_cell, tc=time_cell: self._on_row_scope_maybe_changed(cc, tc))
        time_cell.changed.connect(lambda cc=code_cell, tc=time_cell: self._on_row_scope_maybe_changed(cc, tc))
        self.table.setCellWidget(r, 2, buy_cell)
        self.table.setCellWidget(r, 3, sell_cell)

        # 第0列：股票代码单元格 + 操作按钮（确定/排行/删除）合并成一列
        cell0 = QWidget()
        c0 = QVBoxLayout(cell0)
        c0.setContentsMargins(4, 2, 4, 2)
        c0.setSpacing(3)
        c0.addWidget(code_cell)
        ol = QHBoxLayout()
        ol.setContentsMargins(0, 0, 0, 0)
        ol.setSpacing(4)
        btn_run = QPushButton("确定")
        btn_run.setFixedWidth(52)
        btn_rank = QPushButton("排行")
        btn_rank.setFixedWidth(52)
        btn_del = QPushButton("删除")
        btn_del.setFixedWidth(52)
        btn_run.clicked.connect(lambda: self._run_row(cell0))
        # 从某行进排行榜：下拉只显示这只股票的各时间段（首个 checked 参数吞掉 clicked 的布尔实参）
        btn_rank.clicked.connect(lambda checked=False, cc=code_cell: self._open_leaderboard(cc.value()))
        btn_del.clicked.connect(lambda: self._del_row(cell0))
        ol.addWidget(btn_run)
        ol.addWidget(btn_rank)
        ol.addWidget(btn_del)
        ol.addStretch()
        c0.addLayout(ol)
        cell0.code_cell = code_cell   # 供各处取代码值/回填股票名
        cell0.btn_run = btn_run       # 供 _run_row 跑时置灰
        self.table.setCellWidget(r, 0, cell0)
        self.table.resizeRowsToContents()
        self._schedule_save()

    def _schedule_save(self):
        """请求持久化（防抖）；回填配置期间不保存。"""
        if not self._loading:
            self._save_timer.start()

    def _save_rows(self):
        """把当前所有行序列化写入 config.analysis_rows。"""
        rows = []
        for r in range(self.table.rowCount()):
            mode, days, start, end = self.table.cellWidget(r, 1).value()
            rows.append({
                "code": self.table.cellWidget(r, 0).code_cell.value(),
                "mode": mode, "days": days, "start": start, "end": end,
                "buy": self.table.cellWidget(r, 2).value(),
                "sell": self.table.cellWidget(r, 3).value(),
            })
        self.config["analysis_rows"] = rows
        save_config(self.config)

    def closeEvent(self, e):
        self._save_rows()                # 关窗兜底保存一次
        super().closeEvent(e)

    def _resize_rows_deferred(self):
        # 延到事件循环下一拍再量行高，确保单元格布局已重算（否则首次加条件行高不变）
        QTimer.singleShot(0, self.table.resizeRowsToContents)

    def _widget_row(self, widget):
        for r in range(self.table.rowCount()):
            if self.table.cellWidget(r, 0) is widget:
                return r
        return -1

    def _del_row(self, cell0):
        r = self._widget_row(cell0)
        if r >= 0:
            code_cell = self.table.cellWidget(r, 0).code_cell
            code = code_cell.value() if code_cell else ""
            if code and len(code) >= 2:      # 删整行股票 → 连带删掉它「所有时间段」的排行（含本地）
                self._delete_scopes_for_code(code)
            self.table.removeRow(r)
            self._schedule_save()

    def _run_row(self, cell0):
        r = self._widget_row(cell0)
        if r < 0:
            return
        code_cell = self.table.cellWidget(r, 0).code_cell
        code = code_cell.value()
        if not code or len(code) < 2:
            QMessageBox.warning(self, "提示", "请先填写股票代码")
            return
        mode, days, start, end = self.table.cellWidget(r, 1).value()
        buy_conds = self.table.cellWidget(r, 2).value()
        sell_conds = self.table.cellWidget(r, 3).value()
        if not buy_conds:
            QMessageBox.warning(self, "提示", "请至少添加一个买入条件")
            return
        if not sell_conds:
            QMessageBox.warning(self, "提示", "请至少添加一个卖出条件")
            return
        if mode == "range" and start > end:
            QMessageBox.warning(self, "提示", "起始日期不能晚于截止日期")
            return

        # 该行的「确定」按钮，跑的时候禁用
        btn_run = cell0.btn_run
        btn_run.setEnabled(False)
        btn_run.setText("...")

        rid = self._next_row_id
        self._next_row_id += 1
        worker = BacktestWorker(rid, code, mode, days, start, end, buy_conds, sell_conds)
        worker.result.connect(self._on_result)
        worker.error.connect(self._on_error)
        worker.finished.connect(lambda w=worker, b=btn_run: self._on_done(w, b))
        self._workers[rid] = worker
        self._code_cells[rid] = code_cell
        worker.start()

    def _on_done(self, worker, btn_run):
        btn_run.setEnabled(True)
        btn_run.setText("确定")
        self._workers.pop(worker.row_id, None)
        self._code_cells.pop(worker.row_id, None)

    def _on_result(self, row_id, code, name, report_json):
        cell = self._code_cells.get(row_id)
        if cell is not None:
            cell.set_name(name)   # 回填股票名（编辑时若未触发查询也能补上）
        report = json.loads(report_json)
        self._record_rank(code, name, report)          # 记入/更新收益排行
        self._open_kline(code, name, report)           # 点确定直接开K线买卖点窗口（汇总信息已并入）

    def _on_error(self, row_id, code, msg):
        QMessageBox.warning(self, "回测失败", f"{code}: {msg}")

    # ── 收益排行（按作用域持久化到本地）──
    @staticmethod
    def _scope_key(code, mode, days, start, end):
        if mode == "range":
            return f"{code}|range|{start}|{end}"
        return f"{code}|days|{days}"

    def _row_scope(self, code_cell, time_cell):
        """由某行的代码+时间段算出作用域 key；代码为空返回 None。"""
        code = code_cell.value()
        if not code or len(code) < 2:
            return None
        mode, days, start, end = time_cell.value()
        return self._scope_key(code, mode, days, start, end)

    def _scope_label(self, key):
        rec = self._rank_store.get(key, {})
        meta = rec.get("meta", {})
        name = rec.get("name", "")
        if name:                                   # 只显示股票名前4字，超出用…（不显示代码）
            who = name if len(name) <= 4 else name[:4] + "…"
        else:                                      # 没查到名字才退回用代码
            who = meta.get("code", key.split("|")[0])
        period = (f"{meta.get('start')} ~ {meta.get('end')}" if meta.get("mode") == "range"
                  else f"最近{meta.get('days')}天")
        label = f"{who} · {period}"
        note = rec.get("note")
        if note:
            label += f" · 备注：{note}"
        return label

    def _save_rank(self):
        self.config["rank_store"] = self._rank_store
        self.config["rank_last_scope"] = self._rank_active_scope
        save_config(self.config)

    def _rank_wins_alive(self):
        """当前仍存活（未被 WA_DeleteOnClose 销毁）的排行窗口列表。"""
        alive = []
        for w in list(self._rank_wins):
            try:
                w.isVisible()          # 已销毁的 C++ 对象访问会抛 RuntimeError
                alive.append(w)
            except RuntimeError:
                self._rank_wins.remove(w)
        return alive

    def _refresh_all_wins(self):
        """存储发生变化后，刷新所有打开的排行窗口（下拉+其他股票+表格）。"""
        for w in self._rank_wins_alive():
            self._populate_win(w)

    def _delete_scope(self, key):
        """删除某作用域的排行并落盘；被删的作用域若正被某窗口查看则回退。"""
        if key not in self._rank_store:
            return
        del self._rank_store[key]
        if self._rank_active_scope == key:
            self._rank_active_scope = next(iter(self._rank_store), None)
        for w in self._rank_wins_alive():
            if w.view_scope == key:
                w.view_scope = None
        self._save_rank()
        self._refresh_all_wins()

    def _set_scope_note(self, key, note):
        """给某作用域设置/清除备注并落盘，刷新所有窗口的下拉与副标题。"""
        rec = self._rank_store.get(key)
        if rec is None:
            return
        if note:
            rec["note"] = note
        else:
            rec.pop("note", None)
        self._save_rank()
        self._refresh_all_wins()

    def _delete_scopes_for_code(self, code):
        """删除某股票代码下所有时间段的排行（scope_key 均以 '{code}|' 打头）。"""
        prefix = f"{code}|"
        for key in [k for k in self._rank_store if k.startswith(prefix)]:
            self._delete_scope(key)

    def _on_row_scope_maybe_changed(self, code_cell, time_cell):
        """行的股票/时间段改变：保留旧作用域排行（可在下拉里切换/对比），不再自动删除。
        排行只在「删除整行股票」时清除（见 _del_row）。"""
        time_cell._rank_scope = self._row_scope(code_cell, time_cell)

    def _record_rank(self, code, name, report):
        """记入排行：作用域=(股票+时间段)；同一(买入,卖出)描述去重更新；按收益率降序；立即落盘。"""
        m = report.get("meta", {})
        key = self._scope_key(code, m.get("mode"), m.get("days"), m.get("start"), m.get("end"))
        rec = self._rank_store.get(key)
        if rec is None:
            rec = {"name": name,
                   "meta": {"code": code, "mode": m.get("mode"), "days": m.get("days"),
                            "start": m.get("start"), "end": m.get("end")},
                   "entries": []}
            self._rank_store[key] = rec
        rec["name"] = name
        rec["hold_pct"] = report.get("hold_pct", 0.0)   # 区间买入持有收益（基准，作用域级）
        rec.pop("series", None)   # 不再在本地存 series/trades（旧数据顺手清掉），点行时即时重算
        st, s = report["stats"], report["summary"]
        ck = (st["buy_desc"], st["sell_desc"])
        entries = [e for e in rec["entries"] if (e["buy_desc"], e["sell_desc"]) != ck]  # 去重
        entries.append({
            "buy_desc": st["buy_desc"], "sell_desc": st["sell_desc"],
            "trade_count": st["trade_count"], "win_rate": st["win_rate"],
            "max_drawdown": st["max_drawdown"],
            "return_pct": s["return_pct"], "total_pnl": s["total_pnl"],
            "score": optimize_score(s["return_pct"], st["win_rate"], st["trade_count"]),
            "buy_conds": report.get("buy_conds", []),
            "sell_conds": report.get("sell_conds", []),
        })
        entries.sort(key=lambda e: e["return_pct"], reverse=True)
        rec["entries"] = entries
        self._rank_active_scope = key
        self._save_rank()
        # 新结果所属股票的窗口切到该作用域；其余窗口只刷新下拉/其他股票
        for w in self._rank_wins_alive():
            if not w.filter_code or key.startswith(f"{w.filter_code}|"):
                w.view_scope = key
        self._refresh_all_wins()

    def _populate_win(self, w):
        """填充某个排行窗口：主下拉（本窗口作用域）+ 其他股票下拉 + 表格。"""
        keys = list(self._rank_store.keys())
        if w.filter_code:                                # 只显示该股票的各时间段
            prefix = f"{w.filter_code}|"
            shown = [k for k in keys if k.startswith(prefix)]
        else:
            shown = keys
        items = [(k, self._scope_label(k)) for k in shown]
        notes = {k: (self._rank_store[k].get("note") or "") for k in shown}
        cur = w.view_scope
        if cur not in shown:
            cur = (self._rank_active_scope if self._rank_active_scope in shown
                   else (shown[0] if shown else None))
        w.set_scopes(items, cur, notes)
        # 其他股票：不属于本窗口当前股票代码的作用域，供一键新开窗口
        cur_code = w.filter_code or (cur.split("|")[0] if cur else None)
        others = [(k, self._scope_label(k)) for k in keys
                  if not (cur_code and k.startswith(f"{cur_code}|"))]
        w.set_others(others)
        self._show_rank_scope(w, cur)

    def _show_rank_scope(self, w, key):
        w.view_scope = key
        rec = self._rank_store.get(key)
        if not rec:
            w.refresh("还没有排行记录。在某行点「确定」跑一次，收益就会进入排行。", [])
            return
        entries, meta = rec["entries"], rec["meta"]
        period = (f"{meta.get('start')} ~ {meta.get('end')}" if meta.get("mode") == "range"
                  else f"最近{meta.get('days')}天")
        subtitle = (f"{rec.get('name', '')}({meta.get('code')})　时间段：{period}　"
                    f"共 {len(entries)} 种买卖条件（按收益率降序）")
        if "hold_pct" in rec:
            subtitle += f"　区间买入持有：{rec['hold_pct']:+.2f}%"
        if rec.get("note"):
            subtitle += f"　备注：{rec['note']}"
        w.refresh(subtitle, entries)

    def _make_rank_win(self, filter_code=None, focus_scope=None):
        """新建一个排行窗口并接线；filter_code 过滤下拉，focus_scope 为默认查看的作用域。"""
        w = RankDialog(self)
        w.filter_code = (filter_code or "").strip().upper() or None
        w.view_scope = focus_scope
        w.row_activated.connect(lambda idx, ww=w: self._on_rank_row(ww, idx))
        w.scope_changed.connect(lambda key, ww=w: self._show_rank_scope(ww, key))
        w.combo_requested.connect(lambda key, ww=w: self._run_combo(ww, key))
        w.scope_deleted.connect(self._delete_scope)      # 存储级：改完刷新所有窗口
        w.note_edited.connect(self._set_scope_note)      # 存储级：改完刷新所有窗口
        w.open_other.connect(self._on_open_other)
        w.finished.connect(lambda _r, ww=w: self._rank_wins.remove(ww)
                           if ww in self._rank_wins else None)
        self._rank_wins.append(w)
        self._populate_win(w)
        w.show()
        w.raise_()
        w.activateWindow()
        return w

    def _open_leaderboard(self, code=None):
        """打开某股票的排行窗：已有同股票的窗口则前置复用，否则新建。"""
        fc = (code or "").strip().upper() or None
        for w in self._rank_wins_alive():
            if w.filter_code == fc:
                self._populate_win(w)
                w.show()
                w.raise_()
                w.activateWindow()
                return w
        return self._make_rank_win(filter_code=fc)

    def _on_open_other(self, scope_key):
        """「其他股票」下拉选中某作用域：以该股票为过滤、聚焦该作用域，新开一个窗口。"""
        code = scope_key.split("|")[0]
        w = self._make_rank_win(filter_code=code, focus_scope=scope_key)
        return w

    def _on_rank_row(self, w, idx):
        rec = self._rank_store.get(w.view_scope)
        if not (rec and 0 <= idx < len(rec["entries"])):
            return
        e = rec["entries"][idx]
        # 旧数据兼容：早期条目内嵌了完整 report（含 trades），直接打开
        if e.get("report"):
            rep = dict(e["report"])
            if not rep.get("series"):
                rep["series"] = rec.get("series", [])
            self._open_kline(rec["meta"].get("code"), rec.get("name", ""), rep)
            return
        # 新数据：本地只存了条件 → 用缓存行情即时重算这一组合的买卖点再开 K线
        m = rec["meta"]
        name = rec.get("name", "")
        self._open_worker = BacktestWorker(-1, m.get("code"), m.get("mode"), m.get("days"),
                                           m.get("start"), m.get("end"),
                                           e.get("buy_conds", []), e.get("sell_conds", []))
        self._open_worker.result.connect(
            lambda _rid, code, nm, rj: self._open_kline(code, nm or name, json.loads(rj)))
        self._open_worker.error.connect(
            lambda _rid, _code, msg: QMessageBox.warning(self, "打开失败", msg))
        self._open_worker.start()

    # ── 组合寻优：枚举全部 196 种买卖组合、跑回测、并入该作用域榜单 ──
    def _run_combo(self, w, scope_key):
        rec = self._rank_store.get(scope_key)
        if not rec:
            QMessageBox.information(self, "组合排行", "该作用域还没有数据。")
            return
        if self._combo_worker is not None:
            QMessageBox.information(self, "组合排行", "已有一个组合排行在计算，请稍候。")
            return
        m = rec["meta"]
        self._combo_win = w
        self._combo_worker = ComboWorker(scope_key, m.get("code"), m.get("mode"),
                                         m.get("days"), m.get("start"), m.get("end"))
        self._combo_worker.done.connect(self._on_combo_done)
        self._combo_worker.error.connect(self._on_combo_error)
        w.set_combo_running(True)
        self._combo_worker.start()

    def _combo_stop_loading(self):
        """结束「组合排行」：清 loading（发起窗口若还在）、释放 worker 引用。"""
        if self._combo_win is not None and self._combo_win in self._rank_wins_alive():
            self._combo_win.set_combo_running(False)
        self._combo_win = None
        self._combo_worker = None

    def _on_combo_done(self, scope_key, name, hold_pct, entries_json):
        self._combo_stop_loading()
        rec = self._rank_store.get(scope_key)
        if rec is None:      # 作用域可能已被删除（改时间段/删行），忽略这批结果
            return
        new_entries = json.loads(entries_json)
        if name:
            rec["name"] = name
        rec["hold_pct"] = hold_pct       # 区间买入持有收益（基准）
        rec.pop("series", None)          # 不在本地存 series/trades
        # 并入现有 entries：同一(买入,卖出)描述去重更新（新条目已是摘要+条件）
        merged = {(e["buy_desc"], e["sell_desc"]): e for e in rec["entries"]}
        for e in new_entries:
            merged[(e["buy_desc"], e["sell_desc"])] = e
        entries = list(merged.values())
        entries.sort(key=lambda e: e["return_pct"], reverse=True)
        rec["entries"] = entries
        self._rank_active_scope = scope_key
        for w in self._rank_wins_alive():
            if not w.filter_code or scope_key.startswith(f"{w.filter_code}|"):
                w.view_scope = scope_key
        self._save_rank()
        self._refresh_all_wins()

    def _on_combo_error(self, scope_key, msg):
        self._combo_stop_loading()
        QMessageBox.warning(self, "组合排行失败", msg)

    def _open_analyze(self):
        """打开「配置分析」窗口：把当前所有作用域榜单快照传入，按买入策略归并统计（离线）。"""
        if not self._rank_store:
            QMessageBox.information(self, "分析配置",
                                    "还没有任何排行数据。先在某行点「确定」跑一次回测再来分析。")
            return
        scopes = [(k, self._scope_label(k), len(rec.get("entries", [])),
                   rec.get("hold_pct"), rec.get("entries", []))
                  for k, rec in self._rank_store.items()]
        if self._analyze_win is not None:      # 单例：重开先关旧的，避免数据过期
            self._analyze_win.close()
        self._analyze_win = AnalyzeDialog(scopes, self)
        self._analyze_win.finished.connect(lambda _r: setattr(self, "_analyze_win", None))
        self._analyze_win.show()
        self._analyze_win.raise_()
        self._analyze_win.activateWindow()

    def _open_kline(self, code, name, report):
        """非模态单例：开新K线窗前先关掉旧的，保证同时只存在一个K线窗口。"""
        if self._kline_win is not None:
            self._kline_win.close()
        self._kline_win = KLineDialog(code, name, report, self,
                                      on_rank=lambda checked=False, c=code: self._open_leaderboard(c),
                                      on_ai=lambda checked=False, c=code, n=name: self._open_ai(c, n))
        self._kline_win.finished.connect(lambda _r: setattr(self, "_kline_win", None))
        self._kline_win.show()
        self._kline_win.raise_()
        self._kline_win.activateWindow()

    def _open_ai(self, code, name=""):
        win = AIDialog(self.config, code, name, parent=self)
        win.finished.connect(lambda _r, ww=win: self._ai_wins.remove(ww) if ww in self._ai_wins else None)
        self._ai_wins.append(win)
        win.show()
        win.raise_()
        win.activateWindow()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("股票监控工具")
        self.resize(960, 520)
        self.config = load_config()
        self.worker = None
        self._analysis_win = None       # 股票分析（回测）窗口
        self._ai_wins = []              # AI 诊股窗口（可多开）
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
        self.analysis_btn = QPushButton("股票分析")
        self.analysis_btn.setFixedWidth(80)
        self.analysis_btn.clicked.connect(self._open_analysis)
        top.addWidget(self.analysis_btn)
        self.ai_btn = QPushButton("AI 设置")
        self.ai_btn.setFixedWidth(80)
        self.ai_btn.setToolTip("配置 AI 诊股用的接口类型 / 密钥 / 模型")
        self.ai_btn.clicked.connect(self._open_ai_settings)
        top.addWidget(self.ai_btn)
        layout.addLayout(top)

        # 数据表格，列：代码/名称/最新价/涨跌幅/量比/均线状态/趋势/活跃度/趋势做T策略/我的做T策略/排序
        self.table = QTableWidget()
        self.table.setColumnCount(11)
        self.table.setHorizontalHeaderLabels(["代码", "名称", "最新价", "涨跌幅", "量比", "均线状态", "趋势", "活跃度(N/M)", "趋势做T策略", "我的做T策略", "排序"])
        h = self.table.horizontalHeader()
        for i in range(11):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        tooltips = {
            4: "量比 = 当日每分钟均量 / 过去5日每分钟均量\n>2 放量(红) | 1~2 正常(灰) | <1 缩量(绿)",
            6: "趋势（算法4）：综合均线位置(40%)、MA20斜率(30%)、价格结构(30%)加权评分\n强势↑ ≥0.85 | 偏多↗ 0.60~0.85 | 震荡→ 0.40~0.60 | 偏空↘ 0.15~0.40 | 弱势↓ <0.15",
            7: "活跃度 = N日均量 / M日均量\n≥2.0x 明显放量(红) | 1.2~2.0x 轻微放量(橙) | 1.0~1.2x 正常(灰) | <1.0x 缩量(绿)",
            8: "趋势做T策略（自动）：股价 ≥ MA10 → 积极买进(红)\n股价 < MA10 → 积极卖出(绿)",
            9: "我的做T策略：手动下拉选择\n开盘回踩买进(强势,红) | 拉高卖出下跌买入(震荡分歧,黑) | 开盘拉高卖出(弱势,绿)",
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

    def _open_analysis(self):
        """打开股票分析（回测）窗口，已开则前置。"""
        if self._analysis_win is None:
            self._analysis_win = AnalysisWindow(self.config)
        self._analysis_win.show()
        self._analysis_win.raise_()
        self._analysis_win.activateWindow()

    def _open_ai_settings(self):
        AISettingsDialog(self.config, self).exec()

    def _open_ai_diagnose(self, code, name=""):
        win = AIDialog(self.config, code, name, parent=self)
        win.finished.connect(lambda _r, ww=win: self._ai_wins.remove(ww) if ww in self._ai_wins else None)
        self._ai_wins.append(win)
        win.show()
        win.raise_()
        win.activateWindow()

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
        # 文本列 0~8（代码+7个数据列+趋势做T策略），第9列下拉框，第10列排序按钮
        for c, text in enumerate([code, "--", "--", "--", "--", "--", "--", "--", "--"]):
            self.table.setItem(r, c, QTableWidgetItem(text))
        self._set_t_strategy_combo(r, code)
        self._set_sort_buttons(r)

    def _set_t_strategy_combo(self, row, code):
        """第9列"我的做T策略"下拉框，选项文本带对应字体色，选择后按股票代码持久化"""
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
        self.table.setCellWidget(row, 9, combo)

    def _apply_combo_color(self, combo, idx):
        """把下拉框当前选项的字体色应用到显示"""
        if 0 <= idx < len(T_STRATEGY_OPTIONS):
            color = T_STRATEGY_OPTIONS[idx][1]
            combo.setStyleSheet(f"QComboBox {{ color: {color}; }}")

    def _on_t_strategy_changed(self, combo, idx):
        self._apply_combo_color(combo, idx)
        # 定位该下拉框所在行的股票代码并持久化
        for r in range(self.table.rowCount()):
            if self.table.cellWidget(r, 9) is combo:
                code = self.table.item(r, 0).text()
                self.config.setdefault("t_strategy", {})[code] = idx
                save_config(self.config)
                break

    def _forget_t_strategy(self, code):
        """删除股票时清理其"我的做T策略"持久化，避免残留"""
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
        self.table.setCellWidget(row, 10, w)

    def _widget_row(self, widget):
        for r in range(self.table.rowCount()):
            if self.table.cellWidget(r, 10) == widget:
                return r
        return -1

    def _move_row(self, row, direction):
        target = row + direction
        if target < 0 or target >= self.table.rowCount():
            return
        # 交换文本列（0~8）
        for c in range(9):
            a = self.table.takeItem(row, c)
            b = self.table.takeItem(target, c)
            self.table.setItem(row, c, b)
            self.table.setItem(target, c, a)
        # 交换"我的做T策略"下拉框的选择（第9列是 cellWidget，不能 takeItem）
        cb_a = self.table.cellWidget(row, 9)
        cb_b = self.table.cellWidget(target, 9)
        if cb_a is not None and cb_b is not None:
            ia, ib = cb_a.currentIndex(), cb_b.currentIndex()
            cb_a.setCurrentIndex(ib)
            cb_b.setCurrentIndex(ia)
        self.table.setCurrentCell(target, 0)
        self._save_stocks()

    def _drag_move_row(self, src, dst):
        if src == dst:
            return
        # 取出源行文本列（0~8）和"我的做T策略"下拉框的选择值
        items = [self.table.takeItem(src, c) for c in range(9)]
        cb = self.table.cellWidget(src, 9)
        t_idx = cb.currentIndex() if cb is not None else 0
        code = items[0].text() if items[0] else ""
        self.table.removeRow(src)
        # 往下拖时，删除源行会使目标行上移一位，插入点需 -1；往上拖不受影响
        insert_at = dst if dst < src else dst - 1
        self.table.insertRow(insert_at)
        for c, item in enumerate(items):
            self.table.setItem(insert_at, c, item)
        self._set_t_strategy_combo(insert_at, code)
        new_cb = self.table.cellWidget(insert_at, 9)
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

    def _on_result(self, code, name, price, change_pct, ma5, ma10, ma20, ma30, ma60, vols_json, closes_json, vol_ratio_raw):
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).text() != code:
                continue

            closes = json.loads(closes_json)
            vols   = json.loads(vols_json)
            vol_ratio_rt = vol_ratio_raw if vol_ratio_raw >= 0 else None  # -1 表示取不到

            # ── 算法4：综合趋势强度 ──────────────────────────────
            ma_list = [ma5, ma10, ma20, ma30]
            ma_score = sum(1 for v in ma_list if price >= v) / 4.0

            if len(closes) >= 25:
                ma20_now  = sum(closes[-20:]) / 20
                ma20_prev = sum(closes[-25:-5]) / 20
                slope_score = 1.0 if ma20_now > ma20_prev else 0.0
            else:
                slope_score = 0.5

            if len(closes) >= 15:
                seg = closes[-15:]
                structure_score = 1.0 if seg[-1] - seg[0] > 0 else 0.0
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

            # ── 量比（实时接口） ──────────────────────────────────
            if vol_ratio_rt is not None:
                vr_text = f"{vol_ratio_rt:.2f}"
                if vol_ratio_rt >= 2.0:
                    vr_fg = QColor("#e74c3c")
                elif vol_ratio_rt >= 1.0:
                    vr_fg = QColor("#888888")
                else:
                    vr_fg = QColor("#27ae60")
            else:
                vr_text, vr_fg = "--", None

            # ── 活跃度：N日均量 / M日均量 ──────────────────────
            vol_n = self.vol_n_spin.value()
            vol_m = self.vol_m_spin.value()
            def vol_avg(n):
                return sum(vols[-n:]) / min(n, len(vols)) if vols else 0
            avg_n = vol_avg(vol_n)
            avg_m = vol_avg(vol_m)
            if avg_m > 0:
                activity = avg_n / avg_m
                act_text = f"{activity:.2f}x"
                if activity >= 2.0:
                    act_fg = QColor("#e74c3c")
                elif activity >= 1.2:
                    act_fg = QColor("#e67e22")
                elif activity >= 1.0:
                    act_fg = QColor("#888888")
                else:
                    act_fg = QColor("#27ae60")
            else:
                act_text, act_fg = "--", None

            # ── 均线状态 ─────────────────────────────────────────
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

            # ── 做T策略 ───────────────────────────────────────────
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
                4: (vr_text, vr_fg),
                5: (ma_status, ma_fg),
                6: (trend_text, trend_fg),
                7: (act_text, act_fg),
                8: (t_text, t_fg),
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
                self._float_win.update_stock(code, price, change_pct, vol_ratio_rt)
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
        act_ai = menu.addAction(f"AI 诊股：{name}")
        menu.addSeparator()
        act_del = menu.addAction(f"删除 {code}")
        act = menu.exec(QCursor.pos())
        if act == act_ai:
            self._open_ai_diagnose(code, name)
        elif act == act_pin:
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
                self.table.setItem(r, 1, item)   # 写入"名称"列（文本列），不覆盖数据列
                for c in range(2, 9):            # 数据列(2~8)清空；第9列是用户手动策略，不动
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
