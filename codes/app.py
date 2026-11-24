#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub 榜单数据浏览器
=====================

读取上级目录中的两个 CSV 文件，启动一个仅依赖 Python 标准库的本地 Web 服务，
并自动在浏览器中打开一个可分页、可检索、可排序的数据列表页面。

数据文件（默认位于本脚本所在目录的上一级）:
    每日新增收藏前20.csv              2024 年以来每天新增 Star 前 20 的项目
    工具与基础设施累计下载前20.csv     工具与基础设施项目累计下载前 20

用法:
    python app.py                        启动服务并自动打开浏览器
    python app.py --port 9000            指定端口（被占用时自动顺延）
    python app.py --port 0               由系统分配空闲端口
    python app.py --no-browser           只启动服务，不打开浏览器
    python app.py --data-dir "D:/data"   指定 CSV 所在目录
    python app.py --verbose              打印每条请求日志

「项目描述」默认显示中文：中文对照表保存在 codes/descriptions_zh.json，由
translate.py 生成，原始 CSV 全程只读、不做任何修改。页面可切换「中文 / 英文原文」，
也可在「汉化全部」里触发后台翻译补齐缓存。命令行方式：
    python translate.py                增量翻译缺失的英文描述
    python translate.py --status      查看汉化进度
    python translate.py --init-config 生成翻译接口配置（可填自己的密钥）

按 Ctrl+C 停止服务。页面中的「重新读取」按钮可重载 CSV 与中文对照表。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import translate  # 同目录下的翻译模块（仅依赖标准库）

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = BASE_DIR.parent
ZH_CACHE_PATH = BASE_DIR / "descriptions_zh.json"

STARS_FILE = "每日新增收藏前20.csv"
DOWNLOADS_FILE = "工具与基础设施累计下载前20.csv"


# --------------------------------------------------------------------------- #
# 数据集定义：列名、展示标签、类型、默认是否显示、列宽、是否可排序
# --------------------------------------------------------------------------- #

def col(key, label, type_="text", visible=True, width=160,
        sortable=False, emphasis=False, plus=False):
    """构造一个列配置。type_ ∈ {text, int, date, link, url}"""
    return {
        "key": key,
        "label": label,
        "type": type_,
        "visible": visible,
        "width": width,
        "sortable": sortable,
        "emphasis": emphasis,  # 高亮徽标样式
        "plus": plus,          # 数值前是否带 "+"
    }


STARS_COLUMNS = [
    col("日期（北京时间）", "日期", "date", True, 118, True),
    col("当日排名", "排名", "int", True, 72, True),
    col("事件记录的仓库名称", "仓库", "link", True, 250, True),
    col("当日新增Star事件数", "当日新增Star", "int", True, 138, True, True, True),
    col("当日有Star记录的归档小时数", "归档小时数", "int", False, 118, True),
    col("Release附件累计下载次数", "Release累计下载", "int", True, 158, True),
    col("下载量来源与精度", "下载来源/精度", "text", True, 230),
    col("可见版本下载次数约值（覆盖未核实）", "可见版本下载约值", "int", False, 168, True),
    col("累计Star快照", "累计Star", "int", True, 122, True),
    col("累计Star来源", "累计Star来源", "text", False, 176),
    col("项目描述", "项目描述", "text", True, 360),
    col("项目功能或用途", "功能/用途", "text", False, 420),
    col("功能说明来源", "功能说明来源", "text", False, 150),
    col("仓库链接", "仓库链接", "url", False, 250),
    col("描述来源", "描述来源", "text", False, 130),
]

DOWNLOADS_COLUMNS = [
    col("排名（已核算候选项目）", "排名", "int", True, 72, True),
    col("项目", "项目", "link", True, 250, True),
    col("Release附件累计下载次数", "Release累计下载", "int", True, 172, True, True),
    col("纳入统计的附件数", "附件数", "int", True, 112, True),
    col("累计Star快照", "累计Star", "int", True, 122, True),
    col("累计Star来源", "累计Star来源", "text", False, 176),
    col("项目描述", "项目描述", "text", True, 330),
    col("项目功能或用途", "功能/用途", "text", True, 400),
    col("功能说明来源", "功能说明来源", "text", False, 150),
    col("仓库链接", "仓库链接", "url", False, 250),
    col("描述来源", "描述来源", "text", False, 130),
    col("下载数据接口", "下载数据接口", "url", False, 330),
]

DATASET_SPECS = {
    "stars": {
        "id": "stars",
        "file": STARS_FILE,
        "title": "每日新增收藏 Top 20",
        "subtitle": "按当日新增 Star 事件数降序，每天取前 20 名（北京时间）",
        "name_col": "事件记录的仓库名称",
        "link_col": "仓库链接",
        "date_col": "日期（北京时间）",
        "search_cols": ["事件记录的仓库名称", "项目描述", "项目功能或用途", "下载量来源与精度"],
        "default_sort": [("日期（北京时间）", "desc"), ("当日排名", "asc")],
        "columns": STARS_COLUMNS,
    },
    "downloads": {
        "id": "downloads",
        "file": DOWNLOADS_FILE,
        "title": "累计下载 Top 20",
        "subtitle": "工具与基础设施项目，按公开 Release 附件累计下载次数排序",
        "name_col": "项目",
        "link_col": "仓库链接",
        "date_col": "",
        "search_cols": ["项目", "项目描述", "项目功能或用途"],
        "default_sort": [("排名（已核算候选项目）", "asc")],
        "columns": DOWNLOADS_COLUMNS,
    },
}


# --------------------------------------------------------------------------- #
# 数据读取与元数据
# --------------------------------------------------------------------------- #

def to_int(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def build_stats(dataset_id: str, rows: list[dict], spec: dict) -> list[dict]:
    if dataset_id == "stars":
        dates = sorted({r.get(spec["date_col"], "") for r in rows if r.get(spec["date_col"])})
        names = {r.get(spec["name_col"], "") for r in rows if r.get(spec["name_col"])}
        total_new = sum(to_int(r.get("当日新增Star事件数")) for r in rows)
        return [
            {"label": "记录总数", "value": f"{len(rows):,}"},
            {"label": "覆盖天数", "value": f"{len(dates):,}"},
            {"label": "日期范围", "value": f"{dates[0]} ~ {dates[-1]}" if dates else "—"},
            {"label": "涉及仓库", "value": f"{len(names):,}"},
            {"label": "新增Star事件合计", "value": f"{total_new:,}"},
        ]
    total_dl = sum(to_int(r.get("Release附件累计下载次数")) for r in rows)
    total_assets = sum(to_int(r.get("纳入统计的附件数")) for r in rows)
    top = rows[0].get("项目", "—") if rows else "—"
    return [
        {"label": "榜单项目数", "value": f"{len(rows):,}"},
        {"label": "附件下载合计", "value": f"{total_dl:,}"},
        {"label": "纳入统计附件数", "value": f"{total_assets:,}"},
        {"label": "榜首项目", "value": top},
    ]


class Store:
    """线程安全地持有两个数据集，支持运行时重新读取 CSV。"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self._lock = threading.Lock()
        self._rows: dict[str, list[dict]] = {}
        self._meta: dict = {}
        self.reload()

    def reload(self) -> dict:
        loaded: dict[str, list[dict]] = {}
        for dataset_id, spec in DATASET_SPECS.items():
            path = self.data_dir / spec["file"]
            if not path.is_file():
                raise FileNotFoundError(f"找不到数据文件：{path}")
            with open(path, "r", encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                headers = reader.fieldnames or []
                missing = [c["key"] for c in spec["columns"] if c["key"] not in headers]
                if missing:
                    raise ValueError(f"{path.name} 缺少预期字段：{', '.join(missing)}")
                loaded[dataset_id] = [dict(row) for row in reader]

        meta = {"dataDir": str(self.data_dir), "datasets": []}
        for dataset_id, spec in DATASET_SPECS.items():
            rows = loaded[dataset_id]
            ds = {
                "id": dataset_id,
                "file": spec["file"],
                "title": spec["title"],
                "subtitle": spec["subtitle"],
                "nameCol": spec["name_col"],
                "linkCol": spec["link_col"],
                "hasDate": bool(spec["date_col"]),
                "total": len(rows),
                "columns": spec["columns"],
                "sortOptions": [
                    {"key": c["key"], "label": c["label"]}
                    for c in spec["columns"] if c["sortable"]
                ],
                "stats": build_stats(dataset_id, rows, spec),
            }
            if spec["date_col"]:
                dates = sorted({r.get(spec["date_col"], "") for r in rows
                                if r.get(spec["date_col"])})
                ds["dateMin"] = dates[0] if dates else ""
                ds["dateMax"] = dates[-1] if dates else ""
                ds["dateCount"] = len(dates)
            meta["datasets"].append(ds)

        with self._lock:
            self._rows = loaded
            self._meta = meta
        return meta

    @property
    def meta(self) -> dict:
        with self._lock:
            return self._meta

    def rows(self, dataset_id: str) -> list[dict]:
        with self._lock:
            return self._rows.get(dataset_id, [])


# --------------------------------------------------------------------------- #
# 查询：筛选 -> 排序 -> 分页
# --------------------------------------------------------------------------- #

def sort_value(row: dict, key: str, col_type: str, blank_flag: int):
    raw = row.get(key)
    value = "" if raw is None else str(raw).strip()
    if col_type == "int":
        if not value:
            return (blank_flag, 0)
        try:
            return (0, int(value))
        except ValueError:
            return (blank_flag, 0)
    if not value:
        return (blank_flag, "")
    return (0, value.casefold())


def clamp_int(raw, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(raw)))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# 中文对照缓存：按文件修改时间热加载，翻译脚本写入后可即时生效
# --------------------------------------------------------------------------- #

class ZhCache:
    def __init__(self, path: Path, data_dir: Path):
        self.path = Path(path)
        self.data_dir = Path(data_dir)
        self._lock = threading.Lock()
        self._items: dict[str, str] = {}
        self._mtime = 0.0
        self._total = 0
        self.reload(force=True)
        self._total = len(translate.collect_texts(self.data_dir))

    def reload(self, force: bool = False) -> None:
        try:
            mtime = self.path.stat().st_mtime if self.path.is_file() else 0.0
        except OSError:
            mtime = 0.0
        if not force and mtime == self._mtime:
            return
        items = translate.load_cache(self.path)
        with self._lock:
            self._items = items
            self._mtime = mtime

    def get(self, text: str) -> str:
        with self._lock:
            return self._items.get(text, "")

    @property
    def items(self) -> dict[str, str]:
        with self._lock:
            return self._items

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def total(self) -> int:
        return self._total

    def status(self) -> dict:
        self.reload()
        total = self.total
        cached = min(self.count, total)
        return {"total": total, "cached": cached,
                "percent": round(cached * 100 / total, 1) if total else 100.0}


TRANSLATE_LOCK = threading.Lock()
TRANSLATE_STATE = {"running": False, "done": 0, "message": "", "quota": False}


def start_translate_job() -> bool:
    """在后台线程增量翻译所有缺失条目；已在运行则返回 False。"""
    with TRANSLATE_LOCK:
        if TRANSLATE_STATE["running"]:
            return False
        TRANSLATE_STATE.update(running=True, done=0, message="正在准备…", quota=False)

    texts = translate.collect_texts(ZH_CACHE.data_dir)
    config = translate.load_config()
    items = dict(ZH_CACHE.items)  # 独立副本，避免与读请求共享同一 dict

    def progress(done, total, msg):
        with TRANSLATE_LOCK:
            TRANSLATE_STATE.update(done=done, message=msg)

    def run():
        try:
            res = translate.translate_pending(texts, items, config, progress=progress)
            with TRANSLATE_LOCK:
                TRANSLATE_STATE.update(running=False, done=res["done"],
                                       message=res["message"], quota=res["quota"])
        except Exception as exc:
            with TRANSLATE_LOCK:
                TRANSLATE_STATE.update(running=False, message=f"翻译出错：{exc}")
        finally:
            ZH_CACHE.reload(force=True)

    threading.Thread(target=run, daemon=True, name="zh-translate").start()
    return True


def get_zh_status() -> dict:
    status = ZH_CACHE.status()
    with TRANSLATE_LOCK:
        status.update(running=TRANSLATE_STATE["running"],
                      done=TRANSLATE_STATE["done"],
                      message=TRANSLATE_STATE["message"],
                      quota=TRANSLATE_STATE["quota"])
    status["backend"] = (translate.load_config().get("backends") or ["mymemory"])[0]
    return status


def query_rows(store: Store, params: dict) -> dict:
    dataset_id = (params.get("dataset") or ["stars"])[0]
    if dataset_id not in DATASET_SPECS:
        dataset_id = "stars"
    spec = DATASET_SPECS[dataset_id]
    col_types = {c["key"]: c["type"] for c in spec["columns"]}
    rows = store.rows(dataset_id)

    # 关键词筛选：仓库名 / 描述 / 功能 / 下载来源
    # 「项目描述」同时匹配英文原文和中文译文，切换语言后搜索结果保持一致
    q = ((params.get("q") or [""])[0] or "").strip().casefold()
    if q:
        search_cols = spec["search_cols"]
        ZH_CACHE.reload()

        def matches(row: dict) -> bool:
            for key in search_cols:
                value = str(row.get(key) or "")
                if q in value.casefold():
                    return True
                if key == translate.FIELD and value:
                    zh = ZH_CACHE.get(value)
                    if zh and q in zh.casefold():
                        return True
            return False

        rows = [r for r in rows if matches(r)]

    # 日期筛选（仅每日收藏榜有日期列）
    if spec["date_col"]:
        date_col = spec["date_col"]
        exact = ((params.get("date") or [""])[0] or "").strip()
        date_from = ((params.get("date_from") or [""])[0] or "").strip()
        date_to = ((params.get("date_to") or [""])[0] or "").strip()
        if exact:
            rows = [r for r in rows if str(r.get(date_col) or "") == exact]
        if date_from:
            rows = [r for r in rows if str(r.get(date_col) or "") >= date_from]
        if date_to:
            rows = [r for r in rows if str(r.get(date_col) or "") <= date_to]

    # 排序：优先使用前端指定列，否则用数据集默认排序（多关键字稳定排序）
    sort_key = ((params.get("sort") or [""])[0] or "").strip()
    order = ((params.get("order") or [""])[0] or "").strip().lower()
    if sort_key in col_types and order in ("asc", "desc"):
        sort_plan = [(sort_key, col_types[sort_key], order)]
    else:
        sort_plan = [(k, col_types[k], o) for k, o in spec["default_sort"]]
        sort_key, order = "", ""

    for key, col_type, direction in reversed(sort_plan):
        blank_flag = 1 if direction == "asc" else -1
        rows = sorted(
            rows,
            key=lambda r, k=key, t=col_type, b=blank_flag: sort_value(r, k, t, b),
            reverse=(direction == "desc"),
        )

    total = len(rows)
    size = clamp_int((params.get("size") or [None])[0], 20, 1, 200)
    pages = max(1, math.ceil(total / size))
    page = clamp_int((params.get("page") or [None])[0], 1, 1, pages)
    start = (page - 1) * size
    # 必须复制 dict：中文替换不能污染 Store 中持有的原始数据
    chunk = [dict(r) for r in rows[start:start + size]]

    lang = ((params.get("lang") or ["zh"])[0] or "zh").strip().lower()
    if lang not in ("zh", "en"):
        lang = "zh"
    if lang == "zh":
        ZH_CACHE.reload()
        for r in chunk:
            value = str(r.get(translate.FIELD) or "")
            if value:
                zh = ZH_CACHE.get(value)
                if zh:
                    r[translate.FIELD] = zh

    return {
        "dataset": dataset_id,
        "total": total,
        "page": page,
        "pages": pages,
        "size": size,
        "from": start + 1 if total else 0,
        "to": min(start + size, total),
        "sort": sort_key,
        "order": order,
        "lang": lang,
        "rows": chunk,
    }


# --------------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------------- #

STORE: Store | None = None
ZH_CACHE: ZhCache | None = None
VERBOSE = False


class Handler(BaseHTTPRequestHandler):
    server_version = "GitHubListViewer/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/index.html"):
                self._serve_file(BASE_DIR / "index.html", "text/html; charset=utf-8")
            elif path == "/api/meta":
                self._json(STORE.meta)
            elif path == "/api/rows":
                self._json(query_rows(STORE, parse_qs(parsed.query)))
            elif path == "/api/reload":
                ZH_CACHE.reload(force=True)
                self._json({"ok": True, "meta": STORE.reload()})
            elif path == "/api/zh-status":
                self._json(get_zh_status())
            elif path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_error(404, "Not Found")
        except Exception as exc:  # 把异常以 JSON 返回，便于前端提示
            self._json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)  # 丢弃请求体
            if parsed.path == "/api/translate/start":
                started = start_translate_job()
                self._json({"ok": True, "started": started,
                            "message": "已开始汉化" if started else "汉化任务已在运行中"})
            elif parsed.path == "/api/reload":
                ZH_CACHE.reload(force=True)
                self._json({"ok": True, "meta": STORE.reload()})
            else:
                self.send_error(404, "Not Found")
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(404, f"缺少页面文件：{path}")
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._write(body)

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._write(body)

    def _write(self, body: bytes) -> None:
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 浏览器提前取消请求，忽略即可

    def log_message(self, fmt: str, *args) -> None:
        if VERBOSE:
            super().log_message(fmt, *args)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="读取 GitHub 榜单 CSV 并在网页中以分页列表展示")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR),
                        help="两个 CSV 所在目录（默认：本脚本目录的上一级）")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址（默认 127.0.0.1，仅本机可访问）")
    parser.add_argument("--port", type=int, default=8765,
                        help="端口（默认 8765；被占用时自动顺延；0 表示系统分配）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--verbose", action="store_true", help="打印请求日志")
    args = parser.parse_args()

    global STORE, ZH_CACHE, VERBOSE
    VERBOSE = args.verbose

    # Windows 控制台可能是 GBK，统一按 UTF-8 输出，无法编码的字符降级显示
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    try:
        STORE = Store(Path(args.data_dir))
        ZH_CACHE = ZhCache(ZH_CACHE_PATH, Path(args.data_dir))
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"[错误] 数据读取失败：{exc}")
        print("       请用 --data-dir 指定包含两个 CSV 文件的目录。")
        sys.exit(1)

    server = None
    port = args.port
    for _ in range(30):
        try:
            server = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError:
            if args.port == 0:
                raise
            port += 1
    if server is None:
        print("[错误] 没有找到可用端口，请用 --port 指定其他端口。")
        sys.exit(1)

    server.daemon_threads = True
    actual_port = server.server_address[1]
    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    url = f"http://{display_host}:{actual_port}/"

    print("=" * 62)
    print("  GitHub 榜单数据浏览器")
    print("=" * 62)
    print(f"  数据目录 : {STORE.data_dir}")
    for ds in STORE.meta["datasets"]:
        print(f"    - {ds['file']}  ({ds['total']:,} 条)")
    zh = ZH_CACHE.status()
    print(f"  中文描述 : 已缓存 {zh['cached']:,} / {zh['total']:,} 条 ({zh['percent']}%)")
    if zh["cached"] < zh["total"]:
        print("               未汉化的条目仍显示英文原文，可在页面点「汉化全部」补齐")
    print(f"  访问地址 : {url}")
    if not args.no_browser:
        print("  已自动打开浏览器；若未弹出，请手动复制上面的地址。")
    print("  按 Ctrl+C 停止服务。")
    print("=" * 62)

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
