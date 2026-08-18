# -*- coding: utf-8 -*-
"""环境智能检测/安装脚本（新电脑部署用）。

- 逐个检测运行依赖：已安装的直接跳过，缺失的才安装；
- 检测 Playwright 浏览器内核是否已下载：已有则跳过；
- pip 默认源失败时自动切换清华镜像，Playwright 下载失败时切换国内镜像；
- 全部就绪时输出提示，不做多余操作。

用法：python env_setup.py              （自动补装缺失部分）
      python env_setup.py --check-only （只检测不安装）
"""

import importlib.util
import os
import subprocess
import sys

# Windows 控制台默认 GBK 编码，强制 UTF-8 避免特殊字符（✓ 等）打印报错
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 依赖清单：pip 包名 -> import 模块名
REQUIRED = {
    "requests": "requests",
    "beautifulsoup4": "bs4",
    "openpyxl": "openpyxl",
    "Pillow": "PIL",
    "customtkinter": "customtkinter",
    "playwright": "playwright",
    "rapidocr": "rapidocr",
}

# pip 源列表：先默认源，失败后自动切换清华镜像
PIP_INDEXES = (None, "https://pypi.tuna.tsinghua.edu.cn/simple")
# Playwright 浏览器内核国内镜像
PLAYWRIGHT_MIRROR = "https://npmmirror.com/mirrors/playwright/"


def check_installed(mod_name):
    """检测模块是否可用。"""
    try:
        spec = importlib.util.find_spec(mod_name)
        if spec is not None:
            return True
    except Exception:
        pass
    return False


def pip_install(packages):
    """安装指定的 pip 包列表，默认源失败时自动切换清华镜像。"""
    # 极少数精简版 Python 没有 pip，先补上
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "--version"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        print("> pip 缺失，先执行 ensurepip …")
        try:
            subprocess.check_call([sys.executable, "-m", "ensurepip", "--upgrade"])
        except Exception as exc:
            print("[错误] 无法启用 pip：%s" % exc)
            raise

    last_exc = None
    for index in PIP_INDEXES:
        cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
               "--upgrade"] + packages
        if index:
            cmd += ["-i", index]
        print(">", " ".join(cmd))
        try:
            subprocess.check_call(cmd)
            return
        except Exception as exc:
            last_exc = exc
            if index is None:
                print("> 默认源安装失败，自动改用清华镜像重试…")
            else:
                print("> 清华镜像也失败，请检查网络 / 防火墙后重试。")
    raise last_exc


def chromium_exists():
    """检测 Playwright 的 Chromium 浏览器内核是否已下载。"""
    local = os.environ.get("LOCALAPPDATA", "")
    pw_dir = os.path.join(local, "ms-playwright")
    if not os.path.isdir(pw_dir):
        return False
    try:
        names = os.listdir(pw_dir)
    except OSError:
        # 目录存在但无权限读取，视为未就绪（提示下载更安全）
        return False
    return any("chromium" in d.lower() for d in names)


def install_chromium():
    """下载 Playwright Chromium 内核，默认源失败时切换国内镜像。"""
    try:
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
        return True
    except Exception:
        pass
    print("> 默认下载源失败，改用国内镜像重试…")
    env = dict(os.environ)
    env["PLAYWRIGHT_DOWNLOAD_HOST"] = PLAYWRIGHT_MIRROR
    try:
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"], env=env)
        return True
    except Exception as exc:
        print("> 浏览器内核下载失败（不影响直接抓取），可稍后重跑本脚本补装。")
        print(str(exc))
        return False


def main():
    import argparse
    ap = argparse.ArgumentParser(description="环境智能检测/安装")
    ap.add_argument("--check-only", action="store_true",
                    help="只检测并输出状态，不安装任何东西")
    args = ap.parse_args()

    print("=" * 56)
    print("  环境智能检测（已装跳过，缺失才安装）")
    print("=" * 56)

    # 1) 检测依赖库
    missing = []
    for pkg, mod in REQUIRED.items():
        if check_installed(mod):
            print(f"  [已装] {pkg}")
        else:
            print(f"  [缺失] {pkg}")
            missing.append(pkg)

    if missing:
        missing_text = ", ".join(missing)
        if args.check_only:
            print(f"\n> 检测结果：缺失 {len(missing)} 个依赖：{missing_text}")
            print("> （--check-only 模式，不执行安装）")
            return 2
        print(f"\n> 缺失 {len(missing)} 个依赖，正在安装：{missing_text}")
        try:
            pip_install(missing)
        except Exception as exc:
            print(f"\n[错误] 依赖安装失败，请检查网络后重试。\n{exc}")
            return 1
        print("> 依赖安装完成")
        # 安装后复查
        still = [pkg for pkg, mod in REQUIRED.items() if not check_installed(mod)]
        if still:
            still_text = ", ".join(still)
            print(f"[警告] 以下依赖仍未就绪：{still_text}")
            return 1
    else:
        print("\n> 全部依赖已存在，跳过安装")

    # 2) 检测 Playwright 浏览器内核
    print("\n> 检测 Playwright 浏览器内核…")
    if chromium_exists():
        print("  [已装] Chromium 浏览器内核已存在，跳过下载")
    elif args.check_only:
        print("  [缺失] 未检测到 Chromium 浏览器内核（--check-only 模式，不下载）")
    else:
        print("  [缺失] 未检测到 Chromium 浏览器内核，开始下载（约 100-200MB，首次较慢）…")
        install_chromium()  # 非致命：失败不影响直接抓取

    print("\n" + "=" * 56)
    print("  环境就绪 ✓ 双击「启动产品信息抓取工具.bat」即可使用")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    sys.exit(main())