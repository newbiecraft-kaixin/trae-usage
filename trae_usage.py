#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TRAE 每日积分消耗抓取工具。

使用 Playwright 持久化用户目录（首次运行手动登录，之后免登录）打开
https://www.trae.cn/dashboard#usage ，通过监听网络响应采集 usage 类 JSON，
尽力解析出「日期 -> 当日积分消耗」序列，并每 2 秒刷新一次。

示例：
    python trae_usage.py                 # 首次运行：弹窗手动登录，之后持续抓取
    python trae_usage.py --once          # 只抓一次，用于验证
    python trae_usage.py --headless      # 已登录过，无窗口运行
    python trae_usage.py --csv out.csv   # 指定 CSV 输出路径

所有运行产物都在 trae_usage_data/ 下：
    profile/   浏览器用户目录（含登录态，切勿外传）
    raw/       命中的原始 JSON 响应（已脱敏）
    run.log    运行日志（不含 cookie / token）
    trae_usage.csv  解析后的每日消耗
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import Page, Response, sync_playwright
except ImportError:  # pragma: no cover - 仅用于给出清晰提示
    print("缺少 playwright，请先执行：python -m pip install playwright", file=sys.stderr)
    raise SystemExit(2)


TOOLS_DIR = Path(__file__).resolve().parent
DATA_DIR = TOOLS_DIR / "trae_usage_data"
RAW_DIR = DATA_DIR / "raw"
PROFILE_DIR = DATA_DIR / "profile"
LOG_FILE = DATA_DIR / "run.log"
DEFAULT_CSV = DATA_DIR / "trae_usage.csv"

DASHBOARD_URL = "https://www.trae.cn/dashboard#usage"
INSTALL_HINT = "python -m playwright install chromium"

# 接口 URL / 响应体关键字（小写匹配）
KEYWORD_RE = re.compile(r"credit|usage|point|quota|balance|consume|billing", re.I)
# 脱敏字段：命中则不写入 raw 文件
SENSITIVE_KEY_RE = re.compile(
    r"(token|cookie|password|passwd|secret|authorization|credential|session)", re.I
)
LOGIN_URL_MARKERS = ("login", "passport", "signin", "sign-in", "sso")

DATE_KEY_TOKENS = (
    "date", "day", "time", "period", "dt", "ts", "timestamp",
    "日期", "时间",
)
# 越靠前优先级越高：优先取「消耗」而不是「累计 / 余额」
VALUE_KEYS = (
    "consumeCredits", "consume_credits", "consumedCredits", "consumed_credits",
    "usedCredits", "used_credits", "creditCost", "credit_cost",
    "creditUsage", "credit_usage", "usageCredits", "usage_credits",
    "pointCost", "point_cost", "pointsCost", "points_cost",
    "cost", "credits", "credit", "points", "point", "consume", "consumed",
    "usage", "used", "amount", "value", "score",
    "total", "balance", "quota",
)

NO_CHANGE_LIMIT = 5          # 连续无变化多少次后降低打印频率
RELOAD_MIN_GAP = 120.0       # 整页 reload 降级策略的最小间隔（秒）


# ---------------------------------------------------------------------------
# 日志 / 脱敏
# ---------------------------------------------------------------------------

def setup_logger(verbose: bool = False) -> logging.Logger:
    """控制台 INFO + 文件 DEBUG；日志里绝不写 cookie / token。"""
    logger = logging.getLogger("trae_usage")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)
    return logger


def sanitize_url(url: str) -> str:
    """去掉 query / fragment，避免把 token、用户 id 等写进日志或 raw 元数据。"""
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return "<invalid-url>"


def redact(value: Any) -> Any:
    """递归脱敏响应中的 token / cookie 等字段，raw 文件仍保留业务数据。"""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if SENSITIVE_KEY_RE.search(str(key)):
                cleaned[key] = "***"
            else:
                cleaned[key] = redact(item)
        return cleaned
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def describe_shape(payload: Any) -> str:
    """只描述字段名，不输出字段值，便于定位需要补充的字段映射。"""
    if isinstance(payload, dict):
        keys = [str(k) for k in list(payload.keys())[:20]]
        return "object{keys=" + ",".join(keys) + "}"
    if isinstance(payload, list):
        first = payload[0] if payload else None
        inner = describe_shape(first) if first is not None else ""
        return f"array(len={len(payload)})" + (f" first={inner}" if inner else "")
    return type(payload).__name__


# ---------------------------------------------------------------------------
# 网络响应采集
# ---------------------------------------------------------------------------

@dataclass
class Capture:
    captured_at: float
    url: str
    payload: Any
    path: Path | None = None
    series: list[tuple[str, float]] | None = None
    parse_attempted: bool = False


class UsageCollector:
    """监听页面响应，保存命中关键字的 JSON，并缓存解析结果。"""

    def __init__(self, raw_dir: Path, logger: logging.Logger) -> None:
        self.raw_dir = raw_dir
        self.logger = logger
        self.captures: list[Capture] = []
        self._last_hash: dict[str, str] = {}
        self._logged_urls: set[str] = set()
        self._seq = 0

    def attach(self, page: Page) -> None:
        page.on("response", self._on_response)

    def _on_response(self, response: Response) -> None:
        # 响应体读取失败（重定向 / 页面关闭 / 二进制）属正常现象，直接忽略
        try:
            self._handle(response)
        except Exception:
            pass

    def _handle(self, response: Response) -> None:
        try:
            request = response.request
        except Exception:
            return

        resource_type = (request.resource_type or "").lower()
        headers = response.headers or {}
        content_type = str(headers.get("content-type", "")).lower()
        if resource_type not in ("xhr", "fetch") and "json" not in content_type:
            return
        if response.status >= 400:
            return

        url = response.url
        if url.startswith(("data:", "blob:", "ws:", "wss:")):
            return

        body = response.text()
        if not body or len(body) > 8_000_000:
            return

        if not KEYWORD_RE.search(url) and not KEYWORD_RE.search(body):
            return

        try:
            payload = json.loads(body)
        except Exception:
            return

        safe_url = sanitize_url(url)
        digest = hashlib.sha1(body.encode("utf-8", "ignore")).hexdigest()
        # 同一接口内容没变就不重复落盘，避免 2 秒轮询把磁盘写满
        if self._last_hash.get(safe_url) == digest:
            return
        self._last_hash[safe_url] = digest

        self._seq += 1
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{stamp}_{self._seq:04d}_{digest[:8]}.json"
        path = self.raw_dir / filename
        record = {
            "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "url": safe_url,
            "data": redact(payload),
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

        self.captures.append(Capture(time.time(), safe_url, payload, path))
        if safe_url not in self._logged_urls:
            self._logged_urls.add(safe_url)
            self.logger.info("捕获接口: %s", safe_url)
        else:
            self.logger.debug("接口内容更新: %s", safe_url)

    def has_captures(self) -> bool:
        return bool(self.captures)

    def has_usage_captures(self) -> bool:
        """只有 URL 本身命中 usage 类关键字的接口才算登录态证据。"""
        return any(KEYWORD_RE.search(capture.url) for capture in self.captures)

    def latest_path(self) -> str:
        if not self.captures:
            return "<无>"
        latest = self.captures[-1].path
        return str(latest) if latest else "<无>"


# ---------------------------------------------------------------------------
# 解析：日期 + 当日消耗
# ---------------------------------------------------------------------------

def normalize_date(value: Any) -> str | None:
    """把常见日期形状统一成 YYYY-MM-DD，失败返回 None。"""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e12:          # 毫秒时间戳
            number /= 1000.0
        if 1e9 <= number <= 4e9:   # 秒时间戳
            try:
                return datetime.fromtimestamp(number).strftime("%Y-%m-%d")
            except (OverflowError, OSError, ValueError):
                return None
        text = str(int(number))
        if len(text) == 8:
            try:
                return datetime.strptime(text, "%Y%m%d").strftime("%Y-%m-%d")
            except ValueError:
                return None
        return None

    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    match = re.match(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return None

    match = re.match(r"^(\d{4})(\d{2})(\d{2})$", text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return None

    match = re.match(r"^(\d{1,2})[-/.](\d{1,2})$", text)
    if match:
        month, day = (int(part) for part in match.groups())
        try:
            return datetime(datetime.now().year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        if 2000 <= parsed.year <= 2100:
            return parsed.strftime("%Y-%m-%d")
    except ValueError:
        pass
    return None


def _compact(key: Any) -> str:
    return str(key).lower().replace("_", "").replace("-", "")


def _is_date_key(key: Any) -> bool:
    lowered = str(key).lower()
    if lowered in ("ts", "dt"):
        return True
    return any(token in lowered for token in DATE_KEY_TOKENS if token not in ("ts", "dt"))


def extract_number(value: Any) -> float | None:
    """从数字 / 数字字符串 / 嵌套对象中取出一个数值。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("，", "")
        if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
            try:
                return float(text)
            except ValueError:
                return None
        return None
    if isinstance(value, dict):
        return _find_value_field(value)
    return None


def _find_value_field(obj: dict) -> float | None:
    # 先精确匹配优先级高的字段，再做包含匹配
    compact_map = {_compact(key): (key, value) for key, value in obj.items()}
    for candidate in VALUE_KEYS:
        key = _compact(candidate)
        if key in compact_map:
            number = extract_number(compact_map[key][1])
            if number is not None:
                return number
    for candidate in VALUE_KEYS:
        key = _compact(candidate)
        for compact_key, (_, value) in compact_map.items():
            if key in compact_key:
                number = extract_number(value)
                if number is not None:
                    return number
    return None


def _find_date_field(obj: dict) -> str | None:
    for key, value in obj.items():
        if _is_date_key(key):
            date = normalize_date(value)
            if date:
                return date
    # 字段名猜不出来时，退化为「值本身像日期」
    for value in obj.values():
        if isinstance(value, (str, int, float)):
            date = normalize_date(value)
            if date:
                return date
    return None


def _build_bucket(obj: dict) -> dict[str, float]:
    """按天分桶：{"2026-11-01": 12, "2026-11-02": {"credits": 8}}。"""
    bucket: dict[str, float] = {}
    for key, value in obj.items():
        date = normalize_date(key)
        if not date:
            continue
        number = extract_number(value)
        if number is not None:
            bucket[date] = number
    return bucket


def _series_score(points: dict[str, float]) -> tuple[int, int, int]:
    dates = sorted(points)
    span = 0
    if len(dates) >= 2:
        try:
            span = (datetime.fromisoformat(dates[-1]) - datetime.fromisoformat(dates[0])).days
        except ValueError:
            span = 0
    non_zero = sum(1 for value in points.values() if abs(value) > 1e-12)
    return len(points), non_zero, span


def parse_daily_usage(payload: Any) -> list[tuple[str, float]]:
    """尽力从任意 JSON 中提取「日期 + 当日积分消耗」。

    支持：
      - 数组 [{date/day: ..., credits/points/cost/value/amount: ...}, ...]
      - 数组元素再嵌套一层对象（取第一个日期 + 第一个数值字段）
      - 按天分桶的对象 {"2026-11-01": 12, "2026-11-02": {"credits": 8}}
      - 上述结构的任意嵌套（data / list / result ...）

    解析不出来时返回空列表，绝不抛异常。
    """
    candidates: list[dict[str, float]] = []
    visited: set[int] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if id(node) in visited:
                return
            visited.add(id(node))

            bucket = _build_bucket(node)
            if bucket:
                candidates.append(bucket)

            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            if id(node) in visited:
                return
            visited.add(id(node))

            series: dict[str, float] = {}
            for item in node:
                if not isinstance(item, dict):
                    continue
                date = _find_date_field(item)
                if not date:
                    continue
                number = _find_value_field(item)
                if number is not None:
                    series[date] = number
            if series:
                candidates.append(series)

            for item in node:
                visit(item)

    try:
        visit(payload)
    except Exception:
        return []

    if not candidates:
        return []

    best = max(candidates, key=_series_score)
    return sorted(best.items(), key=lambda item: item[0])


def latest_series(
    collector: UsageCollector, lookback: int = 40
) -> tuple[list[tuple[str, float]], Capture | None]:
    """从最新响应往回找，返回最近一次可解析的序列（带缓存）。"""
    for capture in reversed(collector.captures[-lookback:]):
        if not capture.parse_attempted:
            capture.series = parse_daily_usage(capture.payload)
            capture.parse_attempted = True
        if capture.series:
            return capture.series, capture
    return [], None


# ---------------------------------------------------------------------------
# CSV 输出
# ---------------------------------------------------------------------------

def read_existing_csv(path: Path) -> dict[str, float]:
    merged: dict[str, float] = {}
    if not path.exists():
        return merged
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                date = (row.get("date") or "").strip()
                raw_value = (row.get("credits") or "").strip()
                if date and raw_value:
                    merged[date] = float(raw_value)
    except Exception:
        return {}
    return merged


def write_csv(path: Path, series: list[tuple[str, float]], logger: logging.Logger) -> None:
    """把新序列合并进已有 CSV（同日期以新值为准），原子替换写入。"""
    if not series:
        return
    try:
        merged = read_existing_csv(path)
        for date, value in series:
            merged[date] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["date", "credits"])
            for date in sorted(merged):
                writer.writerow([date, format_number(merged[date])])
        tmp.replace(path)
        logger.info("CSV 已更新: %s", path)
    except Exception as exc:
        logger.warning("写 CSV 失败: %s", exc)


# ---------------------------------------------------------------------------
# 页面交互
# ---------------------------------------------------------------------------

JS_CLICK_REFRESH = r"""
() => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const nodes = Array.from(document.querySelectorAll('button, [role=button], a'));
  for (const el of nodes) {
    if (!visible(el) || el.disabled) continue;
    const text = ((el.innerText || el.textContent || '').trim());
    const label = ((el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')).trim();
    const isRefresh = /^(刷新|刷新数据|刷新列表|refresh)$/i.test(text)
      || /(刷新|refresh)/i.test(label);
    if (isRefresh) {
      el.click();
      return (text || label || 'refresh').slice(0, 20);
    }
  }
  return '';
}
"""

JS_CLICK_USAGE_NAV = r"""
() => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const nodes = Array.from(document.querySelectorAll('a, button, [role=tab], [role=menuitem]'));
  const re = /(积分|用量|消耗|账单|usage|credits?)/i;
  for (const el of nodes) {
    if (!visible(el) || el.disabled) continue;
    const text = ((el.innerText || el.textContent || '').trim());
    if (text && text.length <= 20 && re.test(text)) {
      el.click();
      return text.slice(0, 20);
    }
  }
  return '';
}
"""

JS_LOGIN_STATE = r"""
() => {
  const txt = (document.body && document.body.innerText) ? document.body.innerText : '';
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const nodes = Array.from(document.querySelectorAll('button, a, [role=button]'));
  const loginBtn = nodes.some(el => {
    if (!visible(el)) return false;
    const text = ((el.innerText || el.textContent || '').trim());
    return /^(登录|立即登录|登录\/注册|注册\/登录|Sign in|Log in)$/i.test(text);
  });
  const loginInputs = Array.from(document.querySelectorAll('input')).some(el => {
    if (!visible(el)) return false;
    const hint = (el.placeholder || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.type || '');
    return /(手机号|验证码|邮箱|password)/i.test(hint);
  });
  return {
    url: location.href,
    title: document.title || '',
    loginWords: /(扫码登录|手机号登录|密码登录|验证码登录|获取验证码|输入手机号|输入验证码|登录后即可|请先登录|我已阅读并同意|抖音登录|苹果登录|企业账号登录)/.test(txt),
    loginBtn: loginBtn,
    loginInputs: loginInputs,
    usageWords: /(积分|用量|消耗|credit|usage)/i.test(txt),
    textLen: txt.length
  };
}
"""


def detect_login_state(page: Page, collector: UsageCollector) -> str:
    """返回 logged_in / login_required / unknown。"""
    try:
        url = (page.url or "").lower()
    except Exception:
        return "unknown"

    if any(marker in url for marker in LOGIN_URL_MARKERS):
        return "login_required"

    try:
        info = page.evaluate(JS_LOGIN_STATE)
    except Exception:
        info = {}

    title = str(info.get("title") or "")
    if "登录" in title or "login" in title.lower():
        return "login_required"
    if info.get("loginWords") or info.get("loginInputs"):
        return "login_required"

    # 真正抓到 usage 类接口，说明已登录且页面已开始加载数据
    if collector.has_usage_captures():
        return "logged_in"

    if "/dashboard" in url and info.get("usageWords") and not info.get("loginBtn"):
        return "logged_in"
    if info.get("loginBtn") and not info.get("usageWords"):
        return "login_required"
    return "unknown"


def wait_for_login(
    page: Page,
    collector: UsageCollector,
    headless: bool,
    login_timeout: float,
    logger: logging.Logger,
) -> bool:
    """轮询检测登录态，不做固定等待。"""
    start = time.monotonic()
    last_notice = 0.0
    unknown_since = start
    logged_streak = 0

    while time.monotonic() - start < login_timeout:
        state = detect_login_state(page, collector)
        if state == "logged_in":
            # 连续两次确认，避免把 SPA 跳转登录页前的瞬间误判为已登录；
            # 已捕获到 usage 接口则属于强证据，直接通过。
            if collector.has_usage_captures():
                logger.info("已检测到登录态")
                return True
            logged_streak += 1
            if logged_streak >= 2:
                logger.info("已检测到登录态")
                return True
        else:
            logged_streak = 0

        if state == "login_required":
            if headless:
                logger.error(
                    "当前处于未登录状态，--headless 无法手动登录。"
                    "请先去掉 --headless 运行一次并完成登录。"
                )
                return False
            now = time.monotonic()
            if now - last_notice > 20:
                print(
                    "检测到未登录：请在打开的浏览器窗口中完成 TRAE 登录，"
                    "脚本会自动继续（Ctrl+C 退出）。",
                    flush=True,
                )
                logger.info("等待手动登录中…")
                last_notice = now
        else:
            # 页面还在加载或登录弹窗尚未渲染：给一次温和提示，然后继续轮询
            now = time.monotonic()
            if not headless and now - unknown_since > 8 and now - last_notice > 20:
                print(
                    "如果浏览器窗口里显示登录页，请手动登录；登录成功后脚本会自动继续。",
                    flush=True,
                )
                last_notice = now

        try:
            page.wait_for_timeout(1000)
        except Exception:
            logger.error("浏览器窗口已关闭")
            return False

    logger.error("等待登录超时（%.0f 秒），当前页面: %s", login_timeout, sanitize_url(page.url))
    return False


def ensure_usage_view(page: Page) -> None:
    """确保当前 hash 指向 #usage。"""
    try:
        if "#usage" not in page.url:
            page.evaluate("() => { location.hash = '#usage'; }")
            page.wait_for_timeout(800)
    except Exception:
        pass


def wait_for_new_capture(
    page: Page,
    collector: UsageCollector,
    baseline: int,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + max(0.2, timeout)
    while time.monotonic() < deadline:
        if len(collector.captures) > baseline:
            return True
        try:
            page.wait_for_timeout(100)
        except Exception:
            return False
    return False


def trigger_refresh(
    page: Page,
    round_no: int,
    last_data_round: int,
    state: dict[str, float],
) -> str:
    """优先点刷新按钮；否则切 hash / 触发 hashchange；必要时降级 reload。"""
    try:
        clicked = page.evaluate(JS_CLICK_REFRESH)
        if clicked:
            return f"刷新按钮({clicked})"
    except Exception:
        pass

    try:
        if "#usage" not in page.url:
            page.evaluate("() => { location.hash = '#usage'; }")
            return "跳转 #usage"
    except Exception:
        pass

    if round_no - last_data_round >= 3:
        try:
            nav = page.evaluate(JS_CLICK_USAGE_NAV)
            if nav:
                return f"用量入口({nav})"
        except Exception:
            pass

    # 长时间完全没有新数据时才做整页 reload，避免高频全页刷新
    if (
        round_no - last_data_round >= 8
        and time.monotonic() - state.get("last_reload", 0.0) >= RELOAD_MIN_GAP
    ):
        try:
            page.reload(wait_until="domcontentloaded", timeout=45000)
            state["last_reload"] = time.monotonic()
            return "整页重载(降级)"
        except Exception:
            state["last_reload"] = time.monotonic()

    try:
        page.evaluate(
            "() => window.dispatchEvent(new HashChangeEvent('hashchange'))"
        )
    except Exception:
        pass
    return "hashchange"


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def format_number(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def print_header() -> None:
    print("日期        当日消耗   累计消耗   较上次    刷新", flush=True)
    print("-" * 58, flush=True)


def print_row(
    date: str,
    today_value: float,
    total: float,
    delta: float | None,
    round_no: int,
    strategy: str,
) -> None:
    delta_text = "-" if delta is None else f"{delta:+g}"
    print(
        f"{date}  {format_number(today_value):>8}  "
        f"{format_number(total):>9}  {delta_text:>7}  "
        f"#{round_no} {strategy}",
        flush=True,
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取 TRAE 每日积分消耗（Playwright 持久化会话）"
    )
    parser.add_argument(
        "--interval", type=float, default=2.0,
        help="刷新间隔秒数，默认 2，最小 1",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="只抓取一次并退出，用于验证",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="无窗口运行（需先在有窗口模式下登录过一次）",
    )
    parser.add_argument(
        "--csv", default=str(DEFAULT_CSV),
        help=f"CSV 输出路径（默认 {DEFAULT_CSV}）",
    )
    parser.add_argument(
        "--login-timeout", type=float, default=900,
        help="等待手动登录的最长秒数，默认 900",
    )
    parser.add_argument(
        "--once-timeout", type=float, default=30,
        help="--once 等待 usage 接口的最长秒数，默认 30",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="控制台输出调试日志",
    )
    return parser.parse_args(argv)


def open_dashboard(page: Page, logger: logging.Logger, attempts: int = 3) -> bool:
    for attempt in range(1, attempts + 1):
        try:
            page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=60000)
            # 给 SPA 一点时间完成首屏渲染 / 登录重定向，再开始判定登录态
            page.wait_for_timeout(1500)
            logger.info("已打开 %s", DASHBOARD_URL)
            return True
        except PlaywrightError as exc:
            logger.warning("打开 dashboard 失败（第 %d/%d 次）: %s", attempt, attempts, exc)
            try:
                page.wait_for_timeout(2000)
            except Exception:
                return False
    return False


def wait_for_series(
    page: Page,
    collector: UsageCollector,
    timeout: float,
) -> tuple[list[tuple[str, float]], Capture | None]:
    deadline = time.monotonic() + timeout
    last_seen = 0
    while time.monotonic() < deadline:
        series, capture = latest_series(collector)
        if series:
            return series, capture
        # 有新响应就继续解析；没有就等一会儿，避免空转
        if len(collector.captures) != last_seen:
            last_seen = len(collector.captures)
            continue
        try:
            page.wait_for_timeout(300)
        except Exception:
            break
    return latest_series(collector)


def report_parse_failure(collector: UsageCollector, logger: logging.Logger) -> None:
    path = collector.latest_path()
    logger.warning("已保存原始响应，待补充字段映射。最近文件：%s", path)
    if collector.captures:
        logger.info("最近响应结构：%s", describe_shape(collector.captures[-1].payload))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    logger = setup_logger(args.verbose)
    interval = max(1.0, float(args.interval))
    if interval != args.interval:
        logger.warning("刷新间隔不能小于 1 秒，已调整为 %.1f 秒", interval)

    csv_path = Path(args.csv).expanduser()
    if not csv_path.is_absolute():
        csv_path = Path.cwd() / csv_path

    logger.info("数据目录: %s", DATA_DIR)
    logger.info("CSV 输出: %s", csv_path)

    context = None
    last_series: list[tuple[str, float]] = []

    try:
        with sync_playwright() as playwright:
            try:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(PROFILE_DIR),
                    headless=args.headless,
                    locale="zh-CN",
                    viewport={"width": 1440, "height": 900},
                    args=["--disable-blink-features=AutomationControlled"],
                )
            except PlaywrightError as exc:
                message = str(exc)
                if "Executable doesn't exist" in message or "playwright install" in message:
                    logger.error("Chromium 未安装，请先执行：%s", INSTALL_HINT)
                else:
                    logger.error("浏览器启动失败: %s", message)
                return 2

            context.set_default_timeout(30000)
            collector = UsageCollector(RAW_DIR, logger)

            pages = context.pages
            page = pages[0] if pages else context.new_page()
            for existing in pages:
                collector.attach(existing)
            context.on("page", collector.attach)

            if not open_dashboard(page, logger):
                logger.error("无法打开 dashboard，请检查网络后重试")
                return 3

            if not wait_for_login(
                page, collector, args.headless, args.login_timeout, logger
            ):
                return 2

            ensure_usage_view(page)

            if args.once:
                trigger_refresh(page, 1, 0, {})
                series, capture = wait_for_series(page, collector, args.once_timeout)
                if not collector.has_captures():
                    logger.error(
                        "未捕获到 usage 类接口，当前页面: %s",
                        sanitize_url(page.url),
                    )
                    return 3
                if not series:
                    report_parse_failure(collector, logger)
                    return 4
                last_series = series
                today_date, today_value = series[-1]
                total = sum(value for _, value in series)
                print_header()
                print_row(today_date, today_value, total, None, 1, "首次抓取")
                if capture and capture.path:
                    logger.info("数据来源: %s", capture.path)
                write_csv(csv_path, series, logger)
                return 0

            # ---------------- 长跑循环 ----------------
            print_header()
            round_no = 0
            prev_today: float | None = None
            no_change = 0
            last_data_round = 0
            last_notice_path = ""
            last_csv_series: list[tuple[str, float]] = []
            refresh_state: dict[str, float] = {"last_reload": 0.0}

            try:
                while True:
                    round_start = time.monotonic()
                    round_no += 1
                    strategy = "等待"

                    try:
                        if detect_login_state(page, collector) == "login_required":
                            logger.warning("登录态已失效，请在窗口中重新登录…")
                            if not wait_for_login(
                                page, collector, args.headless,
                                args.login_timeout, logger,
                            ):
                                break
                            ensure_usage_view(page)

                        baseline = len(collector.captures)
                        strategy = trigger_refresh(
                            page, round_no, last_data_round, refresh_state
                        )
                        got_new = wait_for_new_capture(
                            page, collector, baseline,
                            timeout=min(1.6, max(0.4, interval * 0.8)),
                        )
                        if got_new:
                            last_data_round = round_no

                        series, capture = latest_series(collector)
                        if series:
                            last_series = series
                            today_date, today_value = series[-1]
                            total = sum(value for _, value in series)
                            delta = None if prev_today is None else today_value - prev_today
                            changed = (
                                prev_today is None
                                or abs(today_value - prev_today) > 1e-9
                            )

                            if changed:
                                no_change = 0
                            else:
                                no_change += 1

                            # 连续无变化后降低打印频率，但 2 秒轮询保持不变
                            should_print = (
                                changed
                                or no_change <= 1
                                or no_change % NO_CHANGE_LIMIT == 0
                            )
                            if should_print:
                                print_row(
                                    today_date, today_value, total, delta,
                                    round_no, strategy,
                                )
                                logger.info(
                                    "最新日期=%s 当日=%s 累计=%s 变化=%s 来源=%s",
                                    today_date,
                                    format_number(today_value),
                                    format_number(total),
                                    "-" if delta is None else f"{delta:+g}",
                                    sanitize_url(page.url),
                                )
                                if series != last_csv_series:
                                    write_csv(csv_path, series, logger)
                                    last_csv_series = series
                            prev_today = today_value
                        else:
                            notice = collector.latest_path()
                            if collector.has_captures() and notice != last_notice_path:
                                last_notice_path = notice
                                report_parse_failure(collector, logger)
                            elif not collector.has_captures() and round_no % 10 == 0:
                                logger.warning(
                                    "尚未捕获到 usage 类接口，当前页面: %s",
                                    sanitize_url(page.url),
                                )
                    except PlaywrightError as exc:
                        # 单次 502 / 超时 / 页面跳转不影响下一轮
                        logger.warning("本轮刷新异常（继续）：%s", exc)
                    except Exception as exc:
                        logger.warning("本轮处理异常（继续）：%s", exc)

                    remain = interval - (time.monotonic() - round_start)
                    if remain > 0:
                        try:
                            page.wait_for_timeout(int(remain * 1000))
                        except Exception:
                            logger.error("浏览器页面已关闭，退出循环")
                            break
            except KeyboardInterrupt:
                print("\n收到 Ctrl+C，正在退出…", flush=True)
                logger.info("收到 Ctrl+C，准备退出并落盘")

            if last_series:
                write_csv(csv_path, last_series, logger)
                today_date, today_value = last_series[-1]
                logger.info(
                    "退出前最后一笔：%s 当日=%s 累计=%s",
                    today_date,
                    format_number(today_value),
                    format_number(sum(value for _, value in last_series)),
                )
            return 0
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
