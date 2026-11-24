#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目描述中文化 · 翻译与缓存工具
================================

扫描两个 CSV 中的「项目描述」，把其中的英文文本翻译成中文，结果缓存到
descriptions_zh.json。原始 CSV 全程只读，不做任何修改。

缓存以「英文原文 -> 中文译文」为键值，程序(app.py)在网页展示时查表替换，
因此翻译只需做一次，之后完全离线可用。重复运行只会补齐缺失条目（增量）。

用法:
    python translate.py                  增量翻译所有缺失条目
    python translate.py --all            清空缓存并全量重译
    python translate.py --limit 200      只处理前 200 条（按出现频次降序）
    python translate.py --workers 12     并发数（默认 8）
    python translate.py --status         只查看进度，不翻译
    python translate.py --backend mymemory|google|baidu|youdao|custom
    python translate.py --init-config    生成一份可编辑的配置文件

可选配置 translate_config.json（不存在则用内置默认值）:
    {
      "backends": ["mymemory"],      // 按顺序尝试，失败自动降级
      "workers": 8,
      "email": "",                   // MyMemory 注册邮箱，可提升每日额度
      "baidu":  {"appid": "", "key": ""},
      "youdao": {"appKey": "", "appSecret": ""},
      "custom": {"url": "", "headers": {}}
    }

custom 后端约定：POST 到 url，JSON 体 {"q": "<原文>", "from": "en", "to": "zh"}，
返回纯文本，或 JSON 中含 text / translation / result 字段。
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import random
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = BASE_DIR.parent
CACHE_PATH = BASE_DIR / "descriptions_zh.json"
CONFIG_PATH = BASE_DIR / "translate_config.json"

CSV_FILES = ["每日新增收藏前20.csv", "工具与基础设施累计下载前20.csv"]
FIELD = "项目描述"

DEFAULT_CONFIG = {
    "backends": ["mymemory"],
    "workers": 8,
    "email": "",
    "baidu": {"appid": "", "key": ""},
    "youdao": {"appKey": "", "appSecret": ""},
    "custom": {"url": "", "headers": {}},
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

class TranslateError(Exception):
    pass


class QuotaExceeded(TranslateError):
    """免费额度用尽：应停止本轮，保留已完成的部分，下次继续。"""


class RateLimited(TranslateError):
    """接口返回 429：需要退避后重试，或降低并发。"""


def is_chinese(text: str) -> bool:
    """判断文本是否已经含有足够的中文（中英混排的技术描述也算中文）。"""
    zh = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return zh >= 6 and zh / max(1, len(text)) >= 0.15


def _open(req: urllib.request.Request, timeout: int) -> str:
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RateLimited("接口返回 429（请求过于频繁）") from exc
        raise


def http_get(url: str, timeout: int = 15, headers: dict | None = None) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    return _open(req, timeout)


def http_post(url: str, payload: bytes, timeout: int = 15,
              headers: dict | None = None) -> str:
    req = urllib.request.Request(url, data=payload,
                                 headers={"User-Agent": UA, **(headers or {})})
    return _open(req, timeout)


# --------------------------------------------------------------------------- #
# 翻译后端
# --------------------------------------------------------------------------- #

class MyMemoryBackend:
    name = "mymemory"

    def __init__(self, config: dict):
        self.email = (config.get("email") or "").strip()

    def translate(self, text: str) -> str:
        params = {"q": text, "langpair": "en|zh-CN"}
        if self.email:
            params["de"] = self.email
        url = "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(params)
        raw = http_get(url, timeout=20)
        data = json.loads(raw)
        if data.get("quotaFinished"):
            raise QuotaExceeded("MyMemory 当日免费额度已用尽")
        out = (data.get("responseData") or {}).get("translatedText") or ""
        out = html.unescape(out).strip()
        upper = out.upper()
        if "MYMEMORY WARNING" in upper or "QUERY LENGTH LIMIT" in upper or "INVALID" in upper:
            if "ALL AVAILABLE FREE TRANSLATIONS" in upper or "LIMIT" in upper:
                raise QuotaExceeded(out[:120])
            raise TranslateError(out[:120] or "空结果")
        if not out or out.casefold() == text.casefold():
            raise TranslateError("返回结果为空")
        return out


class GoogleBackend:
    name = "google"

    def translate(self, text: str) -> str:
        url = ("https://translate.googleapis.com/translate_a/single?client=gtx"
               "&sl=en&tl=zh-CN&dt=t&q=" + urllib.parse.quote(text))
        data = json.loads(http_get(url, timeout=12))
        out = "".join(part[0] for part in data[0] if part and part[0])
        if not out:
            raise TranslateError("返回结果为空")
        return out.strip()


class BaiduBackend:
    """百度翻译开放平台（需在配置中填 appid / key）。"""
    name = "baidu"

    def __init__(self, config: dict):
        cfg = config.get("baidu") or {}
        self.appid = cfg.get("appid", "")
        self.key = cfg.get("key", "")

    def translate(self, text: str) -> str:
        if not self.appid or not self.key:
            raise TranslateError("未配置百度翻译 appid/key")
        salt = str(random.randint(32768, 65536))
        sign = hashlib.md5((self.appid + text + salt + self.key).encode()).hexdigest()
        params = {"q": text, "from": "en", "to": "zh",
                  "appid": self.appid, "salt": salt, "sign": sign}
        url = "https://fanyi-api.baidu.com/api/trans/vip/translate?" + urllib.parse.urlencode(params)
        data = json.loads(http_get(url, timeout=15))
        if "error_code" in data:
            raise TranslateError(f"百度返回错误 {data['error_code']}: {data.get('error_msg')}")
        items = data.get("trans_result") or []
        if not items:
            raise TranslateError("返回结果为空")
        return "\n".join(i.get("dst", "") for i in items)


class YoudaoBackend:
    """有道智云翻译（需在配置中填 appKey / appSecret）。"""
    name = "youdao"

    def __init__(self, config: dict):
        cfg = config.get("youdao") or {}
        self.app_key = cfg.get("appKey", "")
        self.secret = cfg.get("appSecret", "")

    def translate(self, text: str) -> str:
        if not self.app_key or not self.secret:
            raise TranslateError("未配置有道翻译 appKey/appSecret")
        salt = str(uuid.uuid4())
        curtime = str(int(time.time()))
        inp = text if len(text) <= 20 else text[:10] + str(len(text)) + text[-10:]
        sign = hashlib.sha256(
            (self.app_key + inp + salt + curtime + self.secret).encode()).hexdigest()
        params = {"q": text, "from": "en", "to": "zh-CHS", "appKey": self.app_key,
                  "salt": salt, "sign": sign, "signType": "v3", "curtime": curtime}
        url = "https://openapi.youdao.com/api?" + urllib.parse.urlencode(params)
        data = json.loads(http_get(url, timeout=15))
        out = (data.get("translation") or [""])[0]
        if not out:
            raise TranslateError(f"有道返回错误 {data.get('errorCode')}")
        return out


class CustomBackend:
    """自定义 HTTP 翻译接口。"""
    name = "custom"

    def __init__(self, config: dict):
        cfg = config.get("custom") or {}
        self.url = cfg.get("url", "")
        self.headers = cfg.get("headers") or {}

    def translate(self, text: str) -> str:
        if not self.url:
            raise TranslateError("未配置 custom.url")
        body = json.dumps({"q": text, "from": "en", "to": "zh"}).encode("utf-8")
        headers = {"Content-Type": "application/json", **self.headers}
        raw = http_post(self.url, body, timeout=30, headers=headers).strip()
        if not raw:
            raise TranslateError("返回结果为空")
        if raw.startswith("{"):
            data = json.loads(raw)
            for key in ("text", "translation", "result", "data"):
                val = data.get(key)
                if isinstance(val, str) and val:
                    return val
                if isinstance(val, list) and val:
                    return str(val[0])
            raise TranslateError("JSON 中未找到译文字段")
        return raw


BACKENDS = {
    "mymemory": MyMemoryBackend,
    "google": GoogleBackend,
    "baidu": BaiduBackend,
    "youdao": YoudaoBackend,
    "custom": CustomBackend,
}


class Translator:
    def __init__(self, config: dict):
        self.config = config
        names = config.get("backends") or ["mymemory"]
        self.chain = []
        for name in names:
            cls = BACKENDS.get(name)
            if cls:
                self.chain.append(cls(config))
        if not self.chain:
            self.chain = [MyMemoryBackend(config)]
        self.primary = self.chain[0].name

    def translate(self, text: str) -> str:
        errors = []
        for backend in self.chain:
            for attempt in range(3):
                try:
                    return backend.translate(text)
                except QuotaExceeded:
                    raise
                except RateLimited as exc:
                    errors.append(f"{backend.name}: {exc}")
                    time.sleep(1.5 * (2 ** attempt))  # 429 指数退避：1.5s / 3s / 6s
                except Exception as exc:
                    errors.append(f"{backend.name}: {exc}")
                    time.sleep(0.4 * (attempt + 1))
        raise TranslateError(" | ".join(errors[-3:]))


# --------------------------------------------------------------------------- #
# 语料收集与缓存
# --------------------------------------------------------------------------- #

def collect_texts(data_dir: Path) -> list[str]:
    """按出现频次降序返回所有需要翻译的唯一「项目描述」。"""
    import csv
    counts: dict[str, int] = {}
    for filename in CSV_FILES:
        path = Path(data_dir) / filename
        if not path.is_file():
            continue
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                val = (row.get(FIELD) or "").strip()
                if val and not is_chinese(val):
                    counts[val] = counts.get(val, 0) + 1
    return [t for t, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def load_cache(path: Path = CACHE_PATH) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        items = data.get("items", {}) if isinstance(data, dict) else {}
        return {k: v for k, v in items.items() if isinstance(k, str) and isinstance(v, str)}
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict[str, str], path: Path = CACHE_PATH,
               merge: bool = True) -> None:
    """原子写入，避免 app.py 读到写了一半的文件。

    默认会与磁盘上已有的条目合并：这样即使有多个进程/多次运行，也不会把
    别的进程刚写好的译文覆盖掉（以本次内存中的译文为准）。
    """
    items = dict(cache)
    if merge and path.is_file():
        try:
            existing = load_cache(path)
            existing.update(items)
            items = existing
        except (OSError, ValueError):
            pass
    payload = {
        "version": 1,
        "field": FIELD,
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(items),
        "items": items,
    }
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.is_file():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                user = json.load(fh)
            if isinstance(user, dict):
                for k, v in user.items():
                    if k.startswith("_"):
                        continue
                    cfg[k] = v
        except (OSError, ValueError):
            pass
    return cfg


# --------------------------------------------------------------------------- #
# 批量翻译
# --------------------------------------------------------------------------- #

def translate_pending(texts: list[str], cache: dict[str, str], config: dict,
                      *, workers: int | None = None, limit: int | None = None,
                      progress=None, stop_event: threading.Event | None = None,
                      save_every: int = 10) -> dict:
    """并发翻译所有缺失条目。返回统计字典。

    progress(done, total, message) 会被周期性调用；stop_event 可用于中途停止。
    """
    translator = Translator(config)
    workers = workers or config.get("workers") or 8
    pending = [t for t in texts if t not in cache]
    if limit:
        pending = pending[:limit]
    total = len(pending)
    result = {"total": total, "done": 0, "failed": 0, "quota": False,
              "backend": translator.primary, "message": ""}

    if total == 0:
        if progress:
            progress(0, 0, "缓存已是最新，无需翻译")
        return result

    lock = threading.Lock()
    done = 0
    failed = 0
    quota_hit = False
    dirty = 0
    last_error = ""

    def work(text: str):
        nonlocal done, failed, quota_hit, dirty, last_error
        if stop_event and stop_event.is_set():
            return
        try:
            zh = translator.translate(text)
        except QuotaExceeded as exc:
            with lock:
                quota_hit = True
                last_error = str(exc)
            return
        except Exception as exc:
            with lock:
                failed += 1
                last_error = str(exc)
            return
        with lock:
            cache[text] = zh
            done += 1
            dirty += 1
            if dirty >= save_every:
                dirty = 0
                save_cache(cache)
        if progress and done % 5 == 0:
            progress(done, total, f"正在翻译 {done}/{total}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, t) for t in pending]
        for _ in as_completed(futures):
            if quota_hit or (stop_event and stop_event.is_set()):
                break

    save_cache(cache)
    result.update(done=done, failed=failed, quota=quota_hit)
    if quota_hit:
        result["message"] = f"额度用尽，已完成 {done} 条，下次运行可继续补齐：{last_error}"
    elif stop_event and stop_event.is_set():
        result["message"] = f"已停止，完成 {done} 条"
    else:
        result["message"] = f"完成 {done} 条，失败 {failed} 条"
    if progress:
        progress(done, total, result["message"])
    return result


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="把 CSV 中的英文项目描述翻译成中文并缓存")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--cache", default=str(CACHE_PATH))
    parser.add_argument("--all", action="store_true", help="清空缓存全量重译")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条")
    parser.add_argument("--workers", type=int, default=0, help="并发数")
    parser.add_argument("--backend", default="", help="指定单个后端")
    parser.add_argument("--status", action="store_true", help="只查看进度")
    parser.add_argument("--init-config", action="store_true", help="生成配置文件模板")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if args.init_config:
        template = {"_说明": "删除本行以外的字段可保留默认值；backends 按顺序尝试，失败自动降级",
                    **DEFAULT_CONFIG}
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(template, fh, ensure_ascii=False, indent=2)
        print(f"已生成配置文件：{CONFIG_PATH}")
        return

    config = load_config()
    if args.backend:
        config["backends"] = [args.backend]
    cache_path = Path(args.cache)
    if args.all:
        cache_path.unlink(missing_ok=True)  # 全量重译时先清掉旧缓存
    texts = collect_texts(Path(args.data_dir))
    cache = {} if args.all else load_cache(cache_path)
    missing = sum(1 for t in texts if t not in cache)

    print(f"需翻译的唯一描述：{len(texts)} 条 | 已缓存：{len(cache)} 条 | 待翻译：{missing} 条")
    print(f"翻译后端：{' -> '.join(config.get('backends') or ['mymemory'])} | 并发：{args.workers or config.get('workers')}")
    if args.status:
        return

    def progress(done, total, msg):
        if total:
            print(f"\r  [{done}/{total}] {msg[:60]:<60}", end="", flush=True)
        else:
            print("  " + msg)

    t0 = time.time()
    res = translate_pending(texts, cache, config,
                            workers=args.workers or None,
                            limit=args.limit or None,
                            progress=progress)
    print(f"\n完成：{res['message']} | 耗时 {time.time() - t0:.1f}s")
    print(f"缓存文件：{cache_path}（共 {len(cache)} 条）")
    if res["quota"]:
        print("提示：明天再运行一次本脚本即可继续补齐剩余条目。")


if __name__ == "__main__":
    main()
