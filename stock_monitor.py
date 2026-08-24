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
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QSpinBox, QMenu, QSystemTrayIcon, QColorDialog,
    QComboBox, QStackedWidget, QDateEdit, QDialog
)
from PyQt6.QtCore import QThread, pyqtSignal, QTimer, Qt, QPoint, QEvent, QDate
from PyQt6.QtGui import QColor, QFont, QCursor, QIcon, QPixmap, QPainter, QPen, QPolygon, QBrush

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
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=k&param={param}"
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
    series = _fetch_kline_daily_tencent(tc, start, end, count)
    if series is None:
        series = _fetch_kline_daily_eastmoney(secid, start, end, count)
    if series is None:
        raise Exception("无K线数据（代码有误或停牌）")
    return name, series


# 条件类型：type → (归属, 显示模板)。param 为条件参数（日线周期 / 天数）
COND_TYPES = {
    "ma_above":    ("buy",  "站上{p}日线"),        # 收盘 > MA(param)
    "cooldown":    ("buy",  "距上次卖出>{p}天"),    # 距上次卖出超过 param 个交易日
    "macd_golden": ("buy",  "MACD金叉"),           # DIF 上穿 DEA（12/26/9，param 无意义）
    "ma_below":    ("sell", "跌破{p}日线"),         # 收盘 < MA(param)
    "macd_death":  ("sell", "MACD死叉"),           # DIF 下穿 DEA（12/26/9，param 无意义）
}

# 无参数条件：这些类型的 param 数字框无意义，UI 置灰、回测忽略其值
PARAMLESS_TYPES = {"macd_golden", "macd_death"}


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


def _cond_met(cond, closes, i, last_sell_idx, macd=None):
    """单个条件在第 i 根K线是否成立。
    macd 为 (dif, dea) 预算结果，仅 MACD 类条件需要（由 run_backtest 传入）。
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
    return False


def conds_desc(conds):
    """把条件列表拼成中文描述，如 '站上5日线 且 距上次卖出>3天'。"""
    parts = []
    for c in conds:
        tpl = COND_TYPES.get(c["type"], (None, "?"))[1]
        parts.append(tpl.format(p=c["param"]))
    return " 且 ".join(parts) if parts else "（无条件）"


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
    equity_curve = []         # 逐日盯市权益（相对本金的浮动，用于最大回撤）

    # MACD 只在有 MACD 类条件时预算一次（DIF/DEA），避免逐根重算
    macd = None
    if any(c["type"] in ("macd_golden", "macd_death") for c in buy_conds + sell_conds):
        macd = _macd(closes)

    n = len(closes)
    for i in range(start_idx, n):
        price = closes[i]
        # 当日盯市权益（已实现 + 持仓浮盈）
        floating = (price - buy_price) * shares if holding else 0.0
        equity_curve.append(realized + floating)

        if not holding:
            if buy_conds and all(_cond_met(c, closes, i, last_sell_idx, macd) for c in buy_conds):
                holding = True
                buy_price = price
                trades.append({"date": dates[i], "side": "买入", "price": price,
                               "shares": shares, "pnl": None})
        else:
            if sell_conds and all(_cond_met(c, closes, i, last_sell_idx, macd) for c in sell_conds):
                pnl = (price - buy_price) * shares
                realized += pnl
                round_trips += 1
                if pnl > 0:
                    wins += 1
                holding = False
                last_sell_idx = i
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
    买点红色 ▲ 标日期；卖点绿色 ▼ 标日期 + 单笔盈亏。
    """

    RED, GREEN, LINE, AXIS, TEXT = "#e74c3c", "#27ae60", "#2980b9", "#cccccc", "#555555"

    def __init__(self, series, trades):
        super().__init__()
        self._series = series or []
        self._trades = trades or []
        self.setMinimumSize(680, 400)

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

        # ── 买卖点标注 ──
        idx_of = {d: i for i, d in enumerate(dates)}
        sf = QFont(); sf.setPointSize(8)
        for t in self._trades:
            i = idx_of.get(t["date"])
            if i is None:
                continue
            x, y = int(px(i)), int(py(float(t["price"])))
            is_buy = t["side"] == "买入"
            color = QColor(self.RED if is_buy else self.GREEN)
            painter.setPen(QPen(color, 1))
            painter.setBrush(QBrush(color))
            painter.setFont(sf)
            if is_buy:                          # 点下方向上三角 ▲，下方标日期
                tip = QPolygon([QPoint(x, y + 6), QPoint(x - 5, y + 15), QPoint(x + 5, y + 15)])
                painter.drawPolygon(tip)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(color)
                painter.drawText(x - 30, y + 17, 60, 13,
                                 Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                                 t["date"][5:])
            else:                               # 点上方向下三角 ▼，上方标日期 + 单笔盈亏
                tip = QPolygon([QPoint(x, y - 6), QPoint(x - 5, y - 15), QPoint(x + 5, y - 15)])
                painter.drawPolygon(tip)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(color)                       # 日期用卖点绿色
                painter.drawText(x - 30, y - 41, 60, 13,
                                 Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                                 t["date"][5:])
                pnl = t.get("pnl")
                if pnl is not None:                          # 盈亏：正红 负绿 零灰
                    pnl_color = self.RED if pnl > 0 else (self.GREEN if pnl < 0 else "#888888")
                    painter.setPen(QColor(pnl_color))
                    painter.drawText(x - 30, y - 28, 60, 13,
                                     Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                                     f"{pnl:+,.0f}")

        # ── 图例 ──
        painter.setFont(sf)
        painter.setPen(QColor(self.RED))
        painter.drawText(left, 18, 90, 14, Qt.AlignmentFlag.AlignLeft, "▲ 买入")
        painter.setPen(QColor(self.GREEN))
        painter.drawText(left + 60, 18, 120, 14, Qt.AlignmentFlag.AlignLeft, "▼ 卖出(标盈亏)")
        painter.end()


class KLineDialog(QDialog):
    """K线买卖点弹窗：内嵌 KLineChartWidget + 关闭按钮。"""

    def __init__(self, code, name, series, trades, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"K线买卖点 · {name}({code})")
        self.resize(780, 480)
        lay = QVBoxLayout(self)
        lay.addWidget(KLineChartWidget(series, trades))
        btn = QPushButton("关闭")
        btn.clicked.connect(self.accept)
        lay.addWidget(btn, alignment=Qt.AlignmentFlag.AlignRight)


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
            if self.mode == "range":
                sd = datetime.datetime.strptime(self.start_date, "%Y-%m-%d") - datetime.timedelta(days=pad * 2)
                name, series = fetch_kline_daily(self.code, sd.strftime("%Y-%m-%d"), self.end_date)
            else:
                name, series = fetch_kline_daily(self.code, count=self.days + pad)
            dates, closes, start_idx = _resolve_backtest_window(series, self.mode, self.days, self.start_date)
            report = run_backtest(dates, closes, self.buy_conds, self.sell_conds, start_idx)
            report["window"] = {"first": dates[start_idx], "last": dates[-1]}
            # 回测区间的收盘序列，供 K线窗口画折线+标买卖点（不必再联网）
            report["series"] = [[dates[j], closes[j]] for j in range(start_idx, len(closes))]
            self.result.emit(self.row_id, self.code, name, json.dumps(report))
        except Exception as e:
            self.error.emit(self.row_id, self.code, str(e))


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
        # 该 kind 可选的条件类型：[(type, 显示名), ...]
        self._types = [(t, COND_TYPES[t][1].replace("{p}", "X"))
                       for t, (owner, _tpl) in COND_TYPES.items() if owner == kind]

        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(2, 2, 2, 2)
        self._outer.setSpacing(2)
        self._rows_box = QVBoxLayout()
        self._rows_box.setSpacing(2)
        self._outer.addLayout(self._rows_box)

        add_btn = QPushButton("＋ 添加条件")
        add_btn.setFixedHeight(22)
        add_btn.clicked.connect(lambda: self._add_condition())
        self._outer.addWidget(add_btn)

        self._rows = []   # [(row_widget, type_combo, param_spin)]
        if conds:                       # 从配置回填已保存的条件
            for c in conds:
                self._add_condition(default_type=c.get("type"), param=c.get("param"))
        else:                           # 默认给一条条件
            self._add_condition(default_type=self._types[0][0])

    def _add_condition(self, default_type=None, param=None):
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(3)

        combo = QComboBox()
        for _t, label in self._types:
            combo.addItem(label)
        if default_type:
            idx = next((i for i, (t, _l) in enumerate(self._types) if t == default_type), 0)
            combo.setCurrentIndex(idx)

        spin = QSpinBox()
        spin.setRange(1, 250)
        spin.setValue(param if param is not None else (5 if self.kind == "buy" else 10))
        spin.setMinimumWidth(66)

        # 无参数类型（MACD 金叉/死叉）：参数框置灰禁用，值无意义
        def _sync_spin():
            ctype = self._types[combo.currentIndex()][0]
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

        self._rows_box.addWidget(row)
        self._rows.append((row, combo, spin))
        self._outer.activate()      # 立即重算布局，让 sizeHint 反映新增的条件行
        self.updateGeometry()
        self.conditions_changed.emit()

    def _del_condition(self, row):
        for i, (w, _c, _s) in enumerate(self._rows):
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
        for _w, combo, spin in self._rows:
            ctype = self._types[combo.currentIndex()][0]
            out.append({"type": ctype, "param": spin.value()})
        return out


class BacktestResultDialog(QDialog):
    """回测结果弹窗：汇总 + 交易明细表 + 统计指标。"""

    def __init__(self, code, name, report, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"回测结果 · {name}({code})")
        self.resize(560, 520)
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
            f"<b>收益率</b>：<span style='color:{pnl_color}'>{s['return_pct']:+.2f}%</span><br>"
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
                f"<b>买入并持有对照</b>（首个买点 {bh['buy_date']}@{bh['buy_price']:.4f} → 期末，不执行卖出）<br>"
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

        # ── 交易明细表 ──
        trades = report["trades"]
        tbl = QTableWidget()
        tbl.setColumnCount(5)
        tbl.setHorizontalHeaderLabels(["日期", "方向", "价格", "股数", "单笔盈亏"])
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
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

        # ── 底部按钮：查看K线买卖点 + 关闭 ──
        self._code, self._name = code, name
        self._series = report.get("series", [])
        self._trades = trades
        btn_row = QHBoxLayout()
        kline_btn = QPushButton("查看K线买卖点")
        kline_btn.setEnabled(len(self._series) >= 2)   # 无价格数据则不可点
        kline_btn.clicked.connect(self._show_kline)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btn_row.addStretch()
        btn_row.addWidget(kline_btn)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

    def _show_kline(self):
        KLineDialog(self._code, self._name, self._series, self._trades, self).exec()


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
        top.addWidget(QLabel("每列多条件为「同时满足」；按 10000 股为买卖基数，毛收益（不计手续费）"))
        top.addStretch()
        lay.addLayout(top)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["股票代码", "时间类型", "买入策略", "卖出策略", "操作"])
        h = self.table.horizontalHeader()
        for i in range(5):
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
        self.table.setCellWidget(r, 0, code_cell)
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
        self.table.setCellWidget(r, 2, buy_cell)
        self.table.setCellWidget(r, 3, sell_cell)

        op = QWidget()
        ol = QHBoxLayout(op)
        ol.setContentsMargins(2, 2, 2, 2)
        ol.setSpacing(4)
        btn_run = QPushButton("确定")
        btn_run.setFixedWidth(56)
        btn_del = QPushButton("删除")
        btn_del.setFixedWidth(56)
        btn_run.clicked.connect(lambda: self._run_row(op))
        btn_del.clicked.connect(lambda: self._del_row(op))
        ol.addWidget(btn_run)
        ol.addWidget(btn_del)
        self.table.setCellWidget(r, 4, op)
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
                "code": self.table.cellWidget(r, 0).value(),
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
            if self.table.cellWidget(r, 4) is widget:
                return r
        return -1

    def _del_row(self, op_widget):
        r = self._widget_row(op_widget)
        if r >= 0:
            self.table.removeRow(r)
            self._schedule_save()

    def _run_row(self, op_widget):
        r = self._widget_row(op_widget)
        if r < 0:
            return
        code_cell = self.table.cellWidget(r, 0)
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

        # 找到该行的「确定」按钮，跑的时候禁用
        btn_run = op_widget.layout().itemAt(0).widget()
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
        dlg = BacktestResultDialog(code, name, report, self)
        dlg.exec()

    def _on_error(self, row_id, code, msg):
        QMessageBox.warning(self, "回测失败", f"{code}: {msg}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("股票监控工具")
        self.resize(960, 520)
        self.config = load_config()
        self.worker = None
        self._analysis_win = None       # 股票分析（回测）窗口
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
