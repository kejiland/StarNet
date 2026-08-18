# -*- coding: utf-8 -*-
"""抓取日志：每次抓取（成功/失败）都记录到本地文件，方便回溯与排查。

日志写入 output/logs/抓取日志.log（追加模式），内容为人可读文本 + 一行 JSON，
便于程序或人工后续分析。所有写盘失败都会被静默忽略，不影响抓取本身。
"""

import json
import os
from datetime import datetime

import config as config_mod


def log_dir(cfg=None):
    """日志目录（默认 output/logs）。"""
    if cfg is not None:
        try:
            base = config_mod.get_output_dir(cfg)
        except Exception:
            base = "output"
    else:
        base = "output"
    if not os.path.isabs(base):
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), base)
    d = os.path.join(base, "logs")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def log_file(cfg=None):
    """抓取日志文件路径。"""
    return os.path.join(log_dir(cfg), "抓取日志.log")


# 记录字段用的优先表（京东字段在前，通用字段在后）
_FIELD_KEYS = (
    "商品名称", "产品名称", "商品详情", "设备介绍", "商品参数", "设备参数",
    "商品价格", "用途", "性能特点", "参考图", "参考图片",
)


def _field_summary(row):
    """从抓取行的字段里，挑出已成功填写（非空）的字段名。"""
    got = []
    for k in _FIELD_KEYS:
        v = (row or {}).get(k)
        if isinstance(v, str) and v.strip():
            got.append(k)
        elif v:
            got.append(k)
    return got


def log_scrape(url, category="普通", mode="direct", ok=True, duration_ms=0,
               row=None, error="", logs=None, cfg=None):
    """记录一次抓取结果到本地日志文件。

    参数：
        url        抓取的网址
        category   数据类别（普通/京东/淘宝/拼多多）
        mode       抓取模式（direct/auto/ai）
        ok         是否成功
        duration_ms 耗时毫秒
        row        抓取结果行（用于统计已获取字段）
        error      失败原因
        logs       抓取过程中的日志行（最近若干条）
        cfg        配置（用于定位输出目录）
    返回写入的日志文件路径；写失败返回空字符串。
    """
    try:
        now = datetime.now()
        entry = {
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "url": str(url or ""),
            "category": str(category or ""),
            "mode": str(mode or ""),
            "ok": bool(ok),
            "duration_ms": int(duration_ms or 0),
            "fields": _field_summary(row),
            "error": str(error or ""),
            "logs": [str(x) for x in (logs or [])][-40:],
        }
        path = log_file(cfg)
        with open(path, "a", encoding="utf-8") as f:
            f.write("=" * 74 + "\n")
            f.write("时间：%s\n" % entry["time"])
            f.write("类别：%s    模式：%s\n" % (entry["category"], entry["mode"]))
            f.write("网址：%s\n" % entry["url"])
            f.write("结果：%s    耗时：%d ms\n" % ("成功" if entry["ok"] else "失败",
                                                   entry["duration_ms"]))
            f.write("已获取字段：%s\n" % ("、".join(entry["fields"]) if entry["fields"] else "（无）"))
            if entry["error"]:
                f.write("错误：%s\n" % entry["error"])
            if entry["logs"]:
                f.write("过程日志：\n")
                for ln in entry["logs"]:
                    f.write("  %s\n" % ln)
            f.write("JSON：" + json.dumps(entry, ensure_ascii=False) + "\n")
            f.write("\n")
        return path
    except Exception:
        return ""


def read_recent(cfg=None, limit=50):
    """读取最近 N 条抓取日志（用于界面展示），失败返回空列表。"""
    try:
        path = log_file(cfg)
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        jl = [ln for ln in content.splitlines() if ln.startswith("JSON：")]
        out = []
        for ln in jl[-limit:]:
            try:
                out.append(json.loads(ln[len("JSON："):].strip()))
            except Exception:
                continue
        return out
    except Exception:
        return []
