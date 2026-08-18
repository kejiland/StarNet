# -*- coding: utf-8 -*-
"""配置管理：加载 / 保存 config.json"""

import json
import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULT_CONFIG = {
    "ai": {
        "api_key": "",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "temperature": 0.2,
        "timeout": 120,
        "max_chars": 20000,
        "vision": False
    },
    "scraper": {
        "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "timeout": 15,
        "max_retries": 2,
        "verify_ssl": True,
        "max_workers": 4,
        "js_render": "auto",
        "js_wait_ms": 5000,
        "js_timeout": 60000,
        "proxy": "",
        "ocr": True
    },
    "export": {
        "output_dir": "output",
        "download_images": False,
        "image_width_px": 130,
        "image_height_px": 95,
        "image_max_px": 800
    },
    "providers": [],
    "default_provider": "",
    "last_preset": "",
    "appearance": "dark"
}


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=None):
    """读取配置，缺失项自动补默认值。"""
    path = path or CONFIG_PATH
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                cfg = _deep_merge(cfg, json.load(f))
        except Exception:
            pass
    return cfg


def save_config(cfg, path=None):
    path = path or CONFIG_PATH
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return path


def apply_default_provider(cfg):
    """启动时应用“默认配置”：若已设置 default_provider，
    则把该已保存配置应用到当前 AI 配置，并写回 config.json。

    返回 (是否应用, 配置名或 None)。
    """
    name = (cfg.get("default_provider") or "").strip()
    if not name:
        return False, None
    for p in cfg.get("providers") or []:
        if (p.get("name") == name and (p.get("api_key") or "").strip()
                and (p.get("base_url") or "").strip()):
            ai = cfg["ai"]
            ai.update({
                "api_key": p["api_key"].strip(),
                "base_url": p["base_url"].strip(),
                "model": (p.get("model") or "").strip(),
            })
            try:
                save_config(cfg)
            except Exception:
                pass
            return True, name
    return False, name


def auto_apply_saved_provider(cfg):
    """启动自检：当前 AI 配置的 Key 或请求地址为空时，
    自动从“已保存配置”中恢复第一个完整可用的配置，并写回 config.json。

    返回 (是否修复, 配置名或 None)。
    """
    ai = cfg.get("ai") or {}
    need_fix = not (ai.get("api_key") or "").strip() or not (ai.get("base_url") or "").strip()
    if not need_fix:
        return False, None
    for p in cfg.get("providers") or []:
        if (p.get("api_key") or "").strip() and (p.get("base_url") or "").strip():
            ai.update({
                "api_key": p["api_key"].strip(),
                "base_url": p["base_url"].strip(),
                "model": (p.get("model") or ai.get("model") or "").strip(),
            })
            try:
                save_config(cfg)
            except Exception:
                pass
            return True, p.get("name")
    return False, None


def get_output_dir(cfg):
    """返回（并创建）输出目录的绝对路径。"""
    out = cfg["export"].get("output_dir") or "output"
    if not os.path.isabs(out):
        out = os.path.join(APP_DIR, out)
    os.makedirs(out, exist_ok=True)
    return out
