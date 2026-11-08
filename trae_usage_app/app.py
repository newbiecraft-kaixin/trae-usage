#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TRAE 积分消耗 · 桌面版。

独立窗口（PySide6），单次抓取：按用户选择的日期区间直接调用 TRAE 用量接口，
每天抓完即停，不循环、不轮询。登录态复用 trae_usage_data/profile/。

用法：
    pythonw app.py                    # 双击运行（无控制台）
    python app.py                     # 调试运行
    python app.py --cli --start 2026-11-08 --end 2026-11-08
                                      # 命令行跑一轮（验证用）

产物：trae_usage_data/usage_<起>_<止>.json / .csv
      trae_usage_data/raw/requests_<时间>.jsonl（请求 URL + body，便于排查）
"""

from __future__ import annotations

import json
import logging
import math
import struct
import sys
import threading
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from trae_usage import DATA_DIR, PROFILE_DIR, RAW_DIR, sanitize_url  # noqa: E402

APP_DIR = Path(__file__).resolve().parent
ICON_FILE = APP_DIR / "app.ico"
LOG_FILE = DATA_DIR / "run.log"

DASHBOARD_URL = "https://www.trae.cn/dashboard#usage"
ENDPOINT = "https://api.trae.cn/trae/api/v1/pay/query_user_usage_group_by_session"
PAGE_SIZE = 50               # 接口上限（>50 返回 400）
MAX_PAGES_PER_DAY = 20
REQUEST_GAP = 2.0            # 每次请求之间的最小间隔（秒）
LOGIN_TIMEOUT = 300.0        # 有头模式下等待手动登录的秒数
TZ = timezone(timedelta(hours=8))
LOGIN_URL_MARKERS = ("login", "passport", "signin", "sign-in", "sso")

JS_TOKEN_SCAN = r"""
() => {
  const values = [];
  for (const store of [localStorage, sessionStorage]) {
    for (let i = 0; i < store.length; i++) {
      try { values.push(String(store.getItem(store.key(i)) || '')); } catch (e) {}
    }
  }
  for (const v of values) {
    const full = v.match(/Cloud-IDE-JWT\s+[A-Za-z0-9_.\-]+/);
    if (full) return full[0];
  }
  for (const v of values) {
    const jwt = v.match(/eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+/);
    if (jwt) return 'Cloud-IDE-JWT ' + jwt[0];
  }
  return '';
}
"""

JS_GO_USAGE = "() => { if (!location.hash.includes('usage')) { location.hash = '#usage'; } }"


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def day_span(start: str, end: str) -> list[str]:
    current = datetime.strptime(start, "%Y-%m-%d").date()
    last = datetime.strptime(end, "%Y-%m-%d").date()
    days: list[str] = []
    while current <= last:
        days.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return days


def day_bounds(day: str) -> tuple[int, int]:
    """[当天 00:00:00, 当天 23:59:59] 的 +08 时区秒级时间戳。"""
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp()) - 1


def fmt_num(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}"


def nice_max(value: float) -> float:
    if value <= 0:
        return 1.0
    exp = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * exp:
            return step * exp
    return 10 * exp


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

LOGGER = logging.getLogger("trae_usage_desktop")
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False


def setup_logger() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if LOGGER.handlers:
        return
    handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                           "%Y-%m-%d %H:%M:%S"))
    LOGGER.addHandler(handler)


# ---------------------------------------------------------------------------
# 图标：手写 PNG 后嵌进 ICO，避免额外依赖
# ---------------------------------------------------------------------------

def _png_bytes(width: int, height: int, pixels: bytearray) -> bytes:
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)
        raw.extend(pixels[y * stride:(y + 1) * stride])

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def ensure_icon() -> Path:
    """生成深色圆角 + 蓝色柱状图的图标（仅首次）。"""
    if ICON_FILE.exists():
        return ICON_FILE
    size, radius = 64, 16
    pixels = bytearray(size * size * 4)
    bars = [(13, 38, 8, 14), (26, 30, 8, 22), (39, 20, 8, 32), (52, 28, 8, 24)]
    for y in range(size):
        for x in range(size):
            # 圆角矩形判定
            cx = min(max(x, radius), size - radius - 1)
            cy = min(max(y, radius), size - radius - 1)
            if (x - cx) ** 2 + (y - cy) ** 2 > radius * radius:
                continue
            color = (19, 26, 42, 255)
            if x < 2 or y < 2 or x >= size - 2 or y >= size - 2:
                color = (38, 49, 73, 255)
            for bx, by, bw, bh in bars:
                if bx <= x < bx + bw and by <= y < by + bh:
                    color = (79, 140, 255, 255)
            pixels[(y * size + x) * 4:(y * size + x) * 4 + 4] = bytes(color)
    png = _png_bytes(size, size, pixels)
    ico = (struct.pack("<HHH", 0, 1, 1)
           + struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(png), 22)
           + png)
    ICON_FILE.write_bytes(ico)
    return ICON_FILE


# ---------------------------------------------------------------------------
# 抓取线程（单次，不循环）
# ---------------------------------------------------------------------------

try:
    from PySide6.QtCore import QThread, Signal
except ImportError:  # pragma: no cover - 首次运行提示
    raise SystemExit(
        "缺少 PySide6，请先执行：python -m pip install PySide6"
    )


class Stopped(Exception):
    pass


class AuthExpired(Exception):
    pass


class FetchError(Exception):
    pass


class FetchWorker(QThread):
    """后台线程：打开页面取登录凭证，然后按天直接调用接口。"""

    sig_log = Signal(str)
    sig_phase = Signal(str)
    sig_day = Signal(dict)
    sig_finished = Signal(dict)
    sig_failed = Signal(str)

    def __init__(self, start: str, end: str, headless: bool, parent=None) -> None:
        super().__init__(parent)
        self.start_day = start
        self.end_day = end
        self.headless = headless
        self._stop = threading.Event()
        self._auth = ""
        self._request_count = 0
        self._stamp = time.strftime("%Y%m%d_%H%M%S")
        self._req_path = RAW_DIR / f"requests_{self._stamp}.jsonl"
        self._results: list[dict[str, Any]] = []

    # ---------- 基础 ----------

    def stop(self) -> None:
        self._stop.set()
        self.requestInterruption()

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise Stopped()

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            remain = deadline - time.monotonic()
            if remain <= 0:
                return
            time.sleep(min(0.1, remain))
        raise Stopped()

    def _log(self, message: str, level: int = logging.INFO) -> None:
        LOGGER.log(level, message)
        self.sig_log.emit(f"{time.strftime('%H:%M:%S')}  {message}")

    def _phase(self, text: str) -> None:
        self.sig_phase.emit(text)

    # ---------- 请求记录 ----------

    def _record(self, day: str, page_num: int, body: dict, status: int,
                total: int, rows: int) -> None:
        record = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "day": day,
            "url": ENDPOINT,
            "method": "POST",
            "post_data": body,
            "status": status,
            "total": total,
            "rows": rows,
        }
        try:
            with self._req_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            LOGGER.warning("写请求记录失败：%s", exc)

    # ---------- 登录凭证 ----------

    def _wait_auth(self, page, timeout: float = LOGIN_TIMEOUT) -> str:
        deadline = time.monotonic() + timeout
        last_notice = 0.0
        while not self._stop.is_set():
            if self._auth:
                return self._auth
            url = ""
            try:
                url = (page.url or "").lower()
            except Exception:
                pass
            if any(marker in url for marker in LOGIN_URL_MARKERS):
                if self.headless:
                    raise FetchError(
                        "当前未登录。请勾选「显示浏览器窗口」后重新抓取，"
                        "在弹出的 TRAE 窗口中登录一次即可（登录态会保存）。"
                    )
                self._phase("等待登录")
                if time.monotonic() - last_notice > 15:
                    last_notice = time.monotonic()
                    self._log("检测到登录页：请在浏览器窗口中登录 TRAE，登录后自动继续")
            if time.monotonic() > deadline:
                break
            try:
                page.wait_for_timeout(300)
            except Exception:
                break

        try:
            token = page.evaluate(JS_TOKEN_SCAN)
        except Exception:
            token = ""
        if token:
            self._log("已从页面存储中读取登录凭证")
            return str(token)
        self._check_stop()
        raise FetchError(
            "未能获取登录凭证。请勾选「显示浏览器窗口」，确认已登录 TRAE 后重试。"
        )

    def _reload_auth(self, page) -> None:
        self._phase("刷新登录凭证")
        self._auth = ""
        try:
            page.reload(wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1200)
            page.evaluate(JS_GO_USAGE)
        except Exception as exc:
            LOGGER.warning("刷新页面失败：%s", exc)
        self._auth = self._wait_auth(page, timeout=60)

    # ---------- 单次 POST ----------

    def _post(self, context, day: str, page_num: int,
              start: int, end: int) -> tuple[list[dict], int]:
        body = {
            "start_time": start,
            "end_time": end,
            "page_size": PAGE_SIZE,
            "page_num": page_num,
            "usage_type": [7],
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.trae.cn",
            "Referer": "https://www.trae.cn/",
            "authorization": self._auth,
        }
        self._check_stop()
        response = context.request.post(ENDPOINT, data=json.dumps(body),
                                        headers=headers, timeout=30000)
        self._request_count += 1
        text = response.text()
        try:
            payload = json.loads(text)
        except Exception:
            payload = {}
        rows = payload.get("user_usage_group_by_sessions") or []
        total = int(payload.get("total") or 0)
        self._record(day, page_num, body, response.status, total, len(rows))
        self._log(f"请求 {day} 第 {page_num} 页 → HTTP {response.status}，"
                  f"total={total}，本页 {len(rows)} 条")
        if response.status == 401:
            raise AuthExpired()
        if response.status != 200:
            raise FetchError(
                f"{day} 第 {page_num} 页请求失败：HTTP {response.status} {text[:120]}"
            )
        return rows, total

    # ---------- 单日 ----------

    def _fetch_day(self, context, page, day: str) -> dict[str, Any]:
        self._phase(f"抓取 {day}")
        start, end = day_bounds(day)
        rows: list[dict] = []
        total = 0
        page_num = 1
        while True:
            try:
                batch, total = self._post(context, day, page_num, start, end)
            except AuthExpired:
                self._log("登录凭证已失效，刷新后重试")
                self._reload_auth(page)
                batch, total = self._post(context, day, page_num, start, end)

            rows.extend(batch)
            if not batch or len(rows) >= total or page_num >= MAX_PAGES_PER_DAY:
                if page_num >= MAX_PAGES_PER_DAY and len(rows) < total:
                    self._log(f"{day} 达到单日分页上限 {MAX_PAGES_PER_DAY} 页，"
                              f"结果可能不完整（{len(rows)}/{total}）", logging.WARNING)
                break
            page_num += 1
            self._sleep(REQUEST_GAP)

        valid: list[dict] = []
        for row in rows:
            try:
                ts = int(row.get("usage_time") or 0)
            except (TypeError, ValueError):
                continue
            if start <= ts <= end:
                valid.append(row)

        credits = sum(float(r.get("credits_float") or r.get("amount_float") or 0)
                      for r in valid)
        cost = sum(float(r.get("cost_money_float") or 0) for r in valid)
        result = {
            "date": day,
            "credits": round(credits, 4),
            "cost": round(cost, 6),
            "sessions": len(valid),
        }
        self._log(f"{day} 完成：{len(valid)} 个会话，积分 {fmt_num(credits)}，"
                  f"金额 {fmt_num(cost)}")
        self._results.append(result)
        self.sig_day.emit(dict(result))
        return result

    # ---------- 落盘 ----------

    def _write_outputs(self) -> tuple[Path, Path]:
        payload = {
            "start": self.start_day,
            "end": self.end_day,
            "updated_at": now_text(),
            "endpoint": ENDPOINT,
            "daily": self._results,
            "total_credits": round(sum(d["credits"] for d in self._results), 4),
            "total_cost": round(sum(d["cost"] for d in self._results), 6),
            "total_sessions": sum(d["sessions"] for d in self._results),
            "requests": self._request_count,
            "request_log": str(self._req_path),
        }
        json_path = DATA_DIR / f"usage_{self.start_day}_{self.end_day}.json"
        csv_path = DATA_DIR / f"usage_{self.start_day}_{self.end_day}.csv"
        tmp = json_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(json_path)

        tmp_csv = csv_path.with_suffix(".csv.tmp")
        with tmp_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            handle.write("date,credits,cost,sessions\n")
            for item in self._results:
                handle.write(f"{item['date']},{item['credits']:.4f},"
                             f"{item['cost']:.6f},{item['sessions']}\n")
        tmp_csv.replace(csv_path)
        return json_path, csv_path

    def _result_payload(self, stopped: bool, json_path: str, csv_path: str) -> dict:
        return {
            "start": self.start_day,
            "end": self.end_day,
            "daily": self._results,
            "total_credits": round(sum(d["credits"] for d in self._results), 4),
            "total_cost": round(sum(d["cost"] for d in self._results), 6),
            "total_sessions": sum(d["sessions"] for d in self._results),
            "requests": self._request_count,
            "json_path": json_path,
            "csv_path": csv_path,
            "stopped": stopped,
        }

    # ---------- 入口 ----------

    def run(self) -> None:
        context = None
        try:
            setup_logger()
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            RAW_DIR.mkdir(parents=True, exist_ok=True)
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)

            days = day_span(self.start_day, self.end_day)
            self._log(f"开始单次抓取：{self.start_day} ~ {self.end_day}"
                      f"（{len(days)} 天），请求间隔 {REQUEST_GAP:.0f} 秒")
            self._phase("启动浏览器")

            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                try:
                    context = playwright.chromium.launch_persistent_context(
                        user_data_dir=str(PROFILE_DIR),
                        headless=self.headless,
                        locale="zh-CN",
                        viewport={"width": 1440, "height": 900},
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                except Exception as exc:
                    message = str(exc)
                    if "Executable doesn't exist" in message:
                        raise FetchError(
                            "Chromium 未安装，请先执行：python -m playwright install chromium"
                        ) from exc
                    raise FetchError(f"浏览器启动失败：{message[:160]}") from exc

                context.set_default_timeout(30000)
                page = context.pages[0] if context.pages else context.new_page()
                page.on("request", self._on_request)

                try:
                    page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(1500)
                    page.evaluate(JS_GO_USAGE)
                except Exception as exc:
                    raise FetchError(
                        f"打开积分页失败：{sanitize_url(str(exc))[:160]}"
                    ) from exc

                self._phase("检查登录态")
                self._auth = self._wait_auth(page)
                self._log("已获取登录凭证，开始按天抓取")

                for index, day in enumerate(days, start=1):
                    self._check_stop()
                    self._log(f"[{index}/{len(days)}] 开始抓取 {day}")
                    self._fetch_day(context, page, day)
                    if index < len(days):
                        self._sleep(REQUEST_GAP)

                json_path, csv_path = self._write_outputs()
                self._log(f"全部完成：{len(self._results)} 天，"
                          f"{self._request_count} 次请求，结果已保存")
                self.sig_finished.emit(
                    self._result_payload(False, str(json_path), str(csv_path))
                )
        except Stopped:
            try:
                json_path, csv_path = self._write_outputs()
            except Exception:
                json_path, csv_path = Path(""), Path("")
            self._log("已手动停止，已完成的部分结果已保存")
            self.sig_finished.emit(
                self._result_payload(True, str(json_path), str(csv_path))
            )
        except FetchError as exc:
            self._log(str(exc), logging.ERROR)
            self.sig_failed.emit(str(exc))
        except Exception as exc:  # 兜底
            LOGGER.exception("抓取线程异常")
            self.sig_failed.emit(f"抓取失败：{str(exc)[:160]}")
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            LOGGER.info("抓取线程结束（%d 次请求）", self._request_count)

    def _on_request(self, request) -> None:
        try:
            auth = request.headers.get("authorization") or ""
            if "Cloud-IDE-JWT" in auth:
                self._auth = auth
            if "group_by_session" in request.url:
                self._log(f"监听页面请求：{request.method} {sanitize_url(request.url)}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 界面
# ---------------------------------------------------------------------------

def build_ui():
    from PySide6.QtCore import QDate, QLocale, Qt
    from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPen
    from PySide6.QtCore import QUrl
    from PySide6.QtWidgets import (
        QAbstractItemView, QApplication, QCheckBox, QDateEdit, QFrame, QHBoxLayout,
        QHeaderView, QLabel, QMainWindow, QPlainTextEdit, QPushButton,
        QSizePolicy, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
    )

    ACCENT = "#4f8cff"
    MUTED = "#8b97ad"
    TEXT = "#e8edf7"
    LINE = "#263149"

    STYLE = f"""
    QMainWindow, QWidget#root {{ background: #0d1220; }}
    QFrame#card {{ background: #151c2e; border: 1px solid {LINE}; border-radius: 14px; }}
    QLabel {{ color: {TEXT}; }}
    QLabel#h1 {{ font-size: 19px; font-weight: 700; }}
    QLabel#sub {{ color: {MUTED}; font-size: 12px; }}
    QLabel#key {{ color: {MUTED}; font-size: 11px; }}
    QLabel#big {{ font-size: 42px; font-weight: 700; color: {TEXT}; }}
    QLabel#metric {{ font-size: 15px; font-weight: 600; }}
    QLabel#dot {{ border-radius: 5px; background: #5b6579; }}
    QLabel#phase {{ color: {MUTED}; font-size: 12px; }}
    QLabel#error {{
        color: #ffb3b3; background: rgba(255,107,107,.10);
        border: 1px solid rgba(255,107,107,.35); border-radius: 8px;
        padding: 8px 12px; font-size: 12px;
    }}
    QPushButton {{
        background: #1b2438; color: {TEXT}; border: 1px solid {LINE};
        border-radius: 8px; padding: 8px 16px; font-size: 13px; font-weight: 600;
    }}
    QPushButton:hover {{ background: #222d45; }}
    QPushButton:disabled {{ color: #5b6579; background: #161d2d; border-color: #212b40; }}
    QPushButton#primary {{ background: {ACCENT}; border-color: {ACCENT}; color: #ffffff; }}
    QPushButton#primary:hover {{ background: #629bff; }}
    QPushButton#primary:disabled {{ background: #2b3a55; border-color: #2b3a55; color: #7c8aa5; }}
    QPushButton#ghost {{ padding: 6px 12px; font-size: 12px; font-weight: 500; }}
    QDateEdit {{
        background: #1b2438; border: 1px solid {LINE}; border-radius: 8px;
        padding: 7px 10px; color: {TEXT}; font-size: 13px;
        selection-background-color: {ACCENT};
    }}
    QDateEdit:focus {{ border-color: {ACCENT}; }}
    QDateEdit::drop-down {{ border: none; width: 22px; }}
    QCheckBox {{ color: {TEXT}; font-size: 12px; spacing: 7px; }}
    QCheckBox::indicator {{
        width: 15px; height: 15px; border-radius: 4px;
        border: 1px solid {LINE}; background: #1b2438;
    }}
    QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
    QTableWidget {{
        background: transparent; border: none; color: {TEXT}; font-size: 13px;
        selection-background-color: rgba(79,140,255,.20);
        alternate-background-color: rgba(27,36,56,.55);
    }}
    QTableWidget::item {{ padding: 6px 10px; border-bottom: 1px solid rgba(38,49,73,.55); }}
    QHeaderView::section {{
        background: #151c2e; color: {MUTED}; border: none;
        border-bottom: 1px solid {LINE}; padding: 8px 10px;
        font-size: 11px; font-weight: 600;
    }}
    QTableCornerButton::section {{ background: #151c2e; border: none; }}
    QPlainTextEdit {{
        background: #0b101c; border: 1px solid {LINE}; border-radius: 8px;
        color: #a9e8c9; font: 12px "Cascadia Mono", Consolas, monospace; padding: 8px;
    }}
    QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: #2c3a55; border-radius: 4px; min-height: 24px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QToolButton {{ color: {MUTED}; border: none; font-size: 12px; }}
    QToolButton:hover {{ color: {TEXT}; }}
    QCalendarWidget QWidget {{ background: #151c2e; color: {TEXT}; }}
    QCalendarWidget QAbstractItemView {{
        background: #151c2e; color: {TEXT}; selection-background-color: {ACCENT};
        selection-color: #ffffff; border: none;
    }}
    QCalendarWidget QToolButton {{ color: {TEXT}; font-size: 12px; }}
    """

    class ChartWidget(QWidget):
        """按天柱状图；悬停显示日期 / 积分 / 金额 / 会话数。"""

        def __init__(self) -> None:
            super().__init__()
            self.items: list[dict] = []
            self._hover = -1
            self._bars: list[tuple[float, float, float, float]] = []
            self.setMouseTracking(True)
            self.setFixedHeight(190)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        def set_items(self, items: list[dict]) -> None:
            self.items = items
            self._hover = -1
            self.update()

        def paintEvent(self, event) -> None:  # noqa: N802
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing, True)
            width, height = self.width(), self.height()
            pad_l, pad_r, pad_t, pad_b = 58, 16, 22, 36
            plot_w = max(1, width - pad_l - pad_r)
            plot_h = max(1, height - pad_t - pad_b)

            painter.setPen(QPen(QColor(139, 151, 173, 38), 1))
            for tick in range(5):
                y = pad_t + plot_h - plot_h * tick / 4
                painter.drawLine(pad_l, int(y), width - pad_r, int(y))

            if not self.items:
                painter.setPen(QColor(MUTED))
                painter.setFont(QFont("Microsoft YaHei UI", 9))
                painter.drawText(0, 0, width, height, Qt.AlignCenter,
                                 "暂无数据，选择日期后点「开始抓取」")
                return

            values = [max(0.0, float(item.get("credits") or 0)) for item in self.items]
            top = nice_max(max(values) or 1.0)
            count = len(self.items)
            slot = plot_w / count
            bar_w = min(56.0, max(8.0, slot * 0.58))
            self._bars = []

            painter.setFont(QFont("Microsoft YaHei UI", 8))
            for tick in range(5):
                y = pad_t + plot_h - plot_h * tick / 4
                painter.setPen(QColor(MUTED))
                painter.drawText(0, int(y) - 8, pad_l - 10, 16,
                                 Qt.AlignRight | Qt.AlignVCenter,
                                 fmt_num(top * tick / 4, 0))

            label_step = max(1, math.ceil(count / 10))
            for index, item in enumerate(self.items):
                value = values[index]
                bar_h = (value / top) * plot_h if top > 0 else 0.0
                x = pad_l + slot * index + (slot - bar_w) / 2
                y = pad_t + plot_h - bar_h
                color = QColor("#7aa7ff") if index == self._hover else QColor(ACCENT)
                if value <= 0:
                    color = QColor("#39445c")
                painter.setBrush(color)
                painter.setPen(Qt.NoPen)
                painter.drawRoundedRect(int(x), int(y), int(bar_w),
                                        max(2, int(bar_h)), 5, 5)
                if value > 0 and count <= 14:
                    inside = y - 16 <= pad_t
                    painter.setPen(QColor("#ffffff") if inside else QColor(MUTED))
                    label_y = y + 4 if inside else y - 16
                    painter.drawText(int(x - 12), int(label_y), int(bar_w + 24), 14,
                                     Qt.AlignHCenter | Qt.AlignVCenter,
                                     fmt_num(value, 0))
                self._bars.append((x, y, bar_w, bar_h))
                if index % label_step == 0 or index == count - 1:
                    painter.setPen(QColor(MUTED))
                    painter.drawText(int(pad_l + slot * index), height - 22,
                                     int(slot), 16, Qt.AlignHCenter | Qt.AlignVCenter,
                                     str(item.get("date", ""))[5:])

            if 0 <= self._hover < len(self.items) and self._bars:
                item = self.items[self._hover]
                x, y, bar_w, _ = self._bars[self._hover]
                text = (f"{item.get('date')}    积分 {fmt_num(float(item.get('credits') or 0))}"
                        f"    金额 {fmt_num(float(item.get('cost') or 0))}"
                        f"    {item.get('sessions', 0)} 个会话")
                metrics = painter.fontMetrics()
                box_w = metrics.horizontalAdvance(text) + 20
                box_h = 26
                bx = min(max(4, int(x + bar_w / 2 - box_w / 2)), max(4, width - box_w - 4))
                by = max(4, int(y - box_h - 8))
                painter.setBrush(QColor("#070b14"))
                painter.setPen(QPen(QColor(LINE), 1))
                painter.drawRoundedRect(bx, by, int(box_w), box_h, 6, 6)
                painter.setPen(QColor(TEXT))
                painter.drawText(bx, by, int(box_w), box_h, Qt.AlignCenter, text)

        def mouseMoveEvent(self, event) -> None:  # noqa: N802
            pos = event.position()
            hover = -1
            for index, (x, y, w, h) in enumerate(self._bars):
                if x - 4 <= pos.x() <= x + w + 4 and y - 6 <= pos.y() <= y + h + 6:
                    hover = index
                    break
            if hover != self._hover:
                self._hover = hover
                self.update()

        def leaveEvent(self, event) -> None:  # noqa: N802
            self._hover = -1
            self.update()

    class MainWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.worker: FetchWorker | None = None
            self.days: dict[str, dict] = {}
            self.setWindowTitle("TRAE 积分消耗 · 桌面版")
            self.resize(1100, 920)
            self.setMinimumSize(920, 760)
            try:
                self.setWindowIcon(QIcon(str(ensure_icon())))
            except Exception:
                pass
            self._build()

        def _card(self) -> tuple[QFrame, QVBoxLayout]:
            frame = QFrame()
            frame.setObjectName("card")
            layout = QVBoxLayout(frame)
            layout.setContentsMargins(18, 16, 18, 16)
            layout.setSpacing(10)
            return frame, layout

        def _label(self, text: str, name: str = "") -> QLabel:
            label = QLabel(text)
            if name:
                label.setObjectName(name)
            return label

        def _build(self) -> None:
            root = QWidget()
            root.setObjectName("root")
            self.setCentralWidget(root)
            outer = QVBoxLayout(root)
            outer.setContentsMargins(22, 20, 22, 18)
            outer.setSpacing(12)

            head = QHBoxLayout()
            title_box = QVBoxLayout()
            title_box.setSpacing(2)
            title_box.addWidget(self._label("TRAE 积分消耗", "h1"))
            title_box.addWidget(self._label("桌面版 · 单次抓取 · 积分口径同官网「积分消耗」列", "sub"))
            head.addLayout(title_box)
            head.addStretch(1)
            self.dot = self._label("", "dot")
            self.dot.setFixedSize(10, 10)
            head.addWidget(self.dot)
            self.phase_label = self._label("就绪", "phase")
            head.addWidget(self.phase_label)
            outer.addLayout(head)

            control, control_layout = self._card()
            row1 = QHBoxLayout()
            row1.setSpacing(10)
            row1.addWidget(self._label("起始日期", "key"))
            self.start_edit = QDateEdit(QDate.currentDate())
            self.end_edit = QDateEdit(QDate.currentDate())
            today = QDate.currentDate()
            for edit in (self.start_edit, self.end_edit):
                edit.setDisplayFormat("yyyy-MM-dd")
                edit.setCalendarPopup(True)
                edit.setMinimumDate(QDate(2024, 1, 1))
                edit.setMaximumDate(today)
                edit.setFixedWidth(140)
            row1.addWidget(self.start_edit)
            row1.addWidget(self._label("结束日期", "key"))
            row1.addWidget(self.end_edit)
            for text, back in (("今天", 0), ("昨天", 1), ("近 7 天", 6)):
                btn = QPushButton(text)
                btn.setObjectName("ghost")
                btn.clicked.connect(lambda _=False, d=back: self._quick_range(d))
                row1.addWidget(btn)
            row1.addStretch(1)
            self.show_browser = QCheckBox("显示浏览器窗口（登录时勾选）")
            row1.addWidget(self.show_browser)
            control_layout.addLayout(row1)

            row2 = QHBoxLayout()
            row2.setSpacing(10)
            self.btn_start = QPushButton("开始抓取")
            self.btn_start.setObjectName("primary")
            self.btn_start.clicked.connect(self._start)
            self.btn_stop = QPushButton("停止")
            self.btn_stop.setEnabled(False)
            self.btn_stop.clicked.connect(self._stop)
            self.btn_folder = QPushButton("打开数据文件夹")
            self.btn_folder.setObjectName("ghost")
            self.btn_folder.clicked.connect(self._open_folder)
            row2.addWidget(self.btn_start)
            row2.addWidget(self.btn_stop)
            row2.addWidget(self.btn_folder)
            row2.addStretch(1)
            self.summary = self._label("等待开始", "sub")
            row2.addWidget(self.summary)
            control_layout.addLayout(row2)

            self.error_label = self._label("", "error")
            self.error_label.hide()
            control_layout.addWidget(self.error_label)
            outer.addWidget(control)

            stats, stats_layout = self._card()
            big_row = QHBoxLayout()
            big_row.setSpacing(26)
            big_box = QVBoxLayout()
            big_box.setSpacing(0)
            big_box.addWidget(self._label("区间消耗积分", "key"))
            self.big_value = self._label("—", "big")
            big_box.addWidget(self.big_value)
            self.big_hint = self._label("选择日期后开始抓取", "sub")
            big_box.addWidget(self.big_hint)
            big_row.addLayout(big_box)

            metrics = QVBoxLayout()
            metrics.setSpacing(8)
            self.metric_days = self._label("—", "metric")
            self.metric_sessions = self._label("—", "metric")
            self.metric_cost = self._label("—", "metric")
            for title, value_label in (("天数", self.metric_days),
                                       ("会话数", self.metric_sessions),
                                       ("消费金额", self.metric_cost)):
                item = QHBoxLayout()
                key = self._label(title, "key")
                key.setFixedWidth(56)
                item.addWidget(key)
                item.addWidget(value_label)
                item.addStretch(1)
                metrics.addLayout(item)
            big_row.addLayout(metrics)
            big_row.addStretch(1)
            stats_layout.addLayout(big_row)
            self.chart = ChartWidget()
            stats_layout.addWidget(self.chart)
            outer.addWidget(stats, 1)

            table_card, table_layout = self._card()
            title_row = QHBoxLayout()
            title_row.addWidget(self._label("每日明细", "metric"))
            title_row.addStretch(1)
            self.table_hint = self._label("", "sub")
            title_row.addWidget(self.table_hint)
            table_layout.addLayout(title_row)
            self.table = QTableWidget(0, 4)
            self.table.setHorizontalHeaderLabels(["日期", "积分消耗", "消费金额", "会话数"])
            self.table.verticalHeader().setVisible(False)
            self.table.setShowGrid(False)
            self.table.setAlternatingRowColors(True)
            self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
            self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            self.table.setMinimumHeight(120)
            table_layout.addWidget(self.table)
            outer.addWidget(table_card, 1)

            log_card, log_layout = self._card()
            toggle_row = QHBoxLayout()
            self.log_toggle = QToolButton()
            self.log_toggle.setText("▸ 运行日志")
            self.log_toggle.setCheckable(True)
            self.log_toggle.clicked.connect(self._toggle_log)
            toggle_row.addWidget(self.log_toggle)
            toggle_row.addStretch(1)
            self.request_hint = self._label("", "sub")
            toggle_row.addWidget(self.request_hint)
            log_layout.addLayout(toggle_row)
            self.log_view = QPlainTextEdit()
            self.log_view.setReadOnly(True)
            self.log_view.setMaximumHeight(140)
            self.log_view.hide()
            log_layout.addWidget(self.log_view)
            outer.addWidget(log_card)

            self._set_status("idle", "就绪")

        # ---------- 交互 ----------

        def _quick_range(self, back: int) -> None:
            end = QDate.currentDate()
            self.start_edit.setDate(end.addDays(-back))
            self.end_edit.setDate(end)

        def _toggle_log(self) -> None:
            visible = self.log_toggle.isChecked()
            self.log_view.setVisible(visible)
            self.log_toggle.setText("▾ 运行日志" if visible else "▸ 运行日志")

        def _append_log(self, line: str) -> None:
            self.log_view.appendPlainText(line)
            bar = self.log_view.verticalScrollBar()
            bar.setValue(bar.maximum())

        def _set_status(self, kind: str, text: str) -> None:
            colors = {"idle": "#5b6579", "run": "#38d39f", "wait": "#f5b14c",
                      "err": "#ff6b6b", "done": "#4f8cff"}
            self.dot.setStyleSheet(
                f"border-radius:5px; background:{colors.get(kind, '#5b6579')};")
            self.phase_label.setText(text)

        def _set_busy(self, busy: bool) -> None:
            self.btn_start.setEnabled(not busy)
            self.btn_stop.setEnabled(busy)
            self.start_edit.setEnabled(not busy)
            self.end_edit.setEnabled(not busy)
            self.show_browser.setEnabled(not busy)

        def _open_folder(self) -> None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(DATA_DIR)))

        def _show_error(self, message: str) -> None:
            self.error_label.setText(message)
            self.error_label.show()

        def _start(self) -> None:
            start = self.start_edit.date().toString("yyyy-MM-dd")
            end = self.end_edit.date().toString("yyyy-MM-dd")
            if start > end:
                self._show_error("起始日期不能晚于结束日期")
                return
            if self.worker is not None and self.worker.isRunning():
                self.worker.wait(3000)
            self.error_label.hide()
            self.days.clear()
            self.table.setRowCount(0)
            self.chart.set_items([])
            self.big_value.setText("—")
            self.big_hint.setText("抓取中…")
            self.metric_days.setText("—")
            self.metric_sessions.setText("—")
            self.metric_cost.setText("—")
            self.summary.setText(f"{start} ~ {end}")
            self.request_hint.setText("")
            self.log_view.clear()

            self.worker = FetchWorker(start, end, not self.show_browser.isChecked())
            self.worker.sig_log.connect(self._append_log)
            self.worker.sig_phase.connect(lambda text: self._set_status("run", text))
            self.worker.sig_day.connect(self._on_day)
            self.worker.sig_finished.connect(self._on_finished)
            self.worker.sig_failed.connect(self._on_failed)
            self._set_busy(True)
            self._set_status("run", "启动中")
            self.btn_start.setText("抓取中…")
            self.worker.start()

        def _stop(self) -> None:
            if self.worker is not None:
                self.btn_stop.setEnabled(False)
                self._set_status("wait", "停止中…")
                self.worker.stop()

        def _refresh_summary(self, stopped: bool = False) -> None:
            items = [self.days[key] for key in sorted(self.days)]
            if not items:
                self.big_value.setText("—")
                self.big_hint.setText("没有拿到数据")
                self.chart.set_items([])
                return
            total_credits = sum(float(item["credits"]) for item in items)
            total_cost = sum(float(item["cost"]) for item in items)
            total_sessions = sum(int(item["sessions"]) for item in items)
            self.big_value.setText(fmt_num(total_credits))
            if len(items) == 1:
                self.big_hint.setText(f"{items[0]['date']} · {items[0]['sessions']} 个会话")
            else:
                self.big_hint.setText(f"{items[0]['date']} ~ {items[-1]['date']}")
            self.metric_days.setText(str(len(items)))
            self.metric_sessions.setText(str(total_sessions))
            self.metric_cost.setText(fmt_num(total_cost))
            self.chart.set_items(items)
            self.summary.setText(
                f"{len(items)} 天 · 积分 {fmt_num(total_credits)} · "
                f"金额 {fmt_num(total_cost)}" + ("（已停止）" if stopped else ""))

        def _on_day(self, item: dict) -> None:
            self.days[item["date"]] = item
            self._rebuild_table()
            self._refresh_summary()

        def _rebuild_table(self) -> None:
            rows = [self.days[key] for key in sorted(self.days)]
            self.table.setRowCount(len(rows))
            for index, item in enumerate(rows):
                values = [
                    item["date"],
                    fmt_num(float(item["credits"])),
                    fmt_num(float(item["cost"])),
                    str(item["sessions"]),
                ]
                for column, text in enumerate(values):
                    cell = QTableWidgetItem(text)
                    if column > 0:
                        cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    self.table.setItem(index, column, cell)
            self.table_hint.setText(f"共 {len(rows)} 行" if rows else "")

        def _on_finished(self, result: dict) -> None:
            self._set_busy(False)
            self.btn_start.setText("重新抓取")
            stopped = bool(result.get("stopped"))
            self._set_status("idle" if stopped else "done",
                             "已停止" if stopped else "完成")
            self._refresh_summary(stopped=stopped)
            self.request_hint.setText(
                f"{result.get('requests', 0)} 次请求 · "
                f"已保存 usage_{result['start']}_{result['end']}.json")

        def _on_failed(self, message: str) -> None:
            self._set_busy(False)
            self.btn_start.setText("重新抓取")
            self._set_status("err", "出错")
            self._show_error(message)
            self.big_hint.setText("抓取失败，请查看日志")

        def closeEvent(self, event) -> None:  # noqa: N802
            if self.worker is not None and self.worker.isRunning():
                self.worker.stop()
                if not self.worker.wait(5000):
                    self._set_status("wait", "正在停止，请稍候…")
                    event.ignore()
                    return
            event.accept()

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("TRAE 积分消耗")
    QLocale.setDefault(QLocale(QLocale.Chinese, QLocale.China))
    app.setFont(QFont("Microsoft YaHei UI", 9))
    app.setStyleSheet(STYLE)
    window = MainWindow()
    window.show()
    return app, window


# ---------------------------------------------------------------------------
# 命令行模式（验证用，和界面共用同一套抓取逻辑）
# ---------------------------------------------------------------------------

def run_cli(start: str, end: str, headless: bool) -> int:
    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication(sys.argv)
    code = {"value": 0}

    worker = FetchWorker(start, end, headless)
    worker.sig_log.connect(lambda line: print(line, flush=True))
    worker.sig_phase.connect(lambda text: print(f"[阶段] {text}", flush=True))
    worker.sig_day.connect(lambda item: print(f"[完成] {json.dumps(item, ensure_ascii=False)}",
                                             flush=True))

    def on_finished(result: dict) -> None:
        print("[结果] " + json.dumps(result, ensure_ascii=False), flush=True)
        app.quit()

    def on_failed(message: str) -> None:
        print("[失败] " + message, flush=True)
        code["value"] = 1
        app.quit()

    worker.sig_finished.connect(on_finished)
    worker.sig_failed.connect(on_failed)
    worker.start()
    app.exec()
    worker.wait(10000)
    return code["value"]


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="TRAE 积分消耗桌面版")
    parser.add_argument("--cli", action="store_true", help="命令行跑一轮后退出")
    parser.add_argument("--start", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--show-browser", action="store_true", help="有头模式（登录用）")
    args = parser.parse_args()

    setup_logger()
    if args.cli:
        return run_cli(args.start, args.end, headless=not args.show_browser)
    app, _window = build_ui()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
