# -*- coding: utf-8 -*-
"""产品信息自动抓取工具 —— 程序入口。

用法：
    python main.py                              # 启动图形界面
    python main.py --cli --url <网址> [--url <网址> ...] \
        [--mode direct|ai|auto] [--export 输出.xlsx] [--config config.json]
"""

import argparse
import os
import sys
from datetime import datetime
import time




def run_cli(args):
    import ai_extractor
    import config as config_mod
    import exporter
    import scraper
    from concurrent.futures import ThreadPoolExecutor

    cfg = config_mod.load_config(args.config)
    fixed, name = config_mod.auto_apply_saved_provider(cfg)
    if fixed:
        print(f"⚠ 检测到当前 AI 配置缺失，已自动应用已保存配置「{name}」")
    dapplied, dname = config_mod.apply_default_provider(cfg)
    if dapplied:
        print(f"已应用默认配置「{dname}」")
    urls = [u.strip() for u in args.url if u.strip()]
    if not urls:
        print("错误：--url 不能为空")
        return 1

    def task(item):
        """抓取单个网址，返回 (序号, 行, 异常)。"""
        i, url = item
        import log_manager
        _t0 = time.time()
        _log_buf = []

        def _tee(msg):
            _log_buf.append(str(msg))
            print(msg)

        print(f"[{i}/{len(urls)}] 抓取 {url}")
        try:
            if args.mode == "direct":
                row, meta = scraper.scrape(url, cfg, on_log=_tee)
            elif args.mode == "ai":
                html, final_url, _ = scraper.fetch_html(url, cfg)
                cat = scraper.data_category(url, final_url)
                row = ai_extractor.ai_extract(url, scraper.html_to_text(html), cfg,
                                              on_log=_tee, category=cat)
                meta = {"url": url, "final_url": final_url,
                        "image_url": row.get("参考图片") or row.get("参考图") or ""}
                if not ((row.get("参考图片") or row.get("参考图") or "").strip()):
                    img_urls = scraper.collect_page_product_images(html, final_url)
                    if img_urls:
                        row["参考图片"] = "\n".join(img_urls)
                        row["参考图"] = row["参考图片"]
                        meta["image_urls"] = img_urls
                        meta["image_url"] = img_urls[0]
            else:  # auto
                row, meta = scraper.scrape(url, cfg, on_log=_tee)
                cat = scraper.data_category(url, meta.get("final_url") or "")
                missing = scraper.missing_fields(row, cat)
                if missing and (cfg["ai"].get("api_key") or "").strip():
                    print("AI 补全缺失字段：" + "、".join(missing))
                    try:
                        html, final_url, _ = scraper.fetch_html(url, cfg)
                        cat = scraper.data_category(url, final_url)
                        ai_row = ai_extractor.ai_extract(
                            url, scraper.html_to_text(html), cfg, on_log=_tee,
                            existing={k: row.get(k) for k in missing},
                            category=cat)
                        for k in missing:
                            if ai_row.get(k):
                                row[k] = ai_row[k]
                        # 京东字段与通用字段互相同步
                        for gk, jk in (("产品名称", "商品名称"), ("设备介绍", "商品详情"),
                                       ("设备参数", "商品参数"), ("参考图片", "参考图")):
                            if not (row.get(gk) or "").strip() and (row.get(jk) or "").strip():
                                row[gk] = row[jk]
                            elif not (row.get(jk) or "").strip() and (row.get(gk) or "").strip():
                                row[jk] = row[gk]
                    except Exception as exc:
                        print(f"AI 补全失败（已保留网页解析结果）：{exc}")
                elif missing:
                    print("提示：未配置 AI，缺失字段为空。")

            cat = scraper.data_category(url, meta.get("final_url") or "")
            row["_data_category"] = cat
            name = (row.get("产品名称") or row.get("商品名称") or "").strip()
            if not name:
                row["产品名称"] = url
                row["商品名称"] = url
            img = (row.get("参考图片") or row.get("参考图") or "").strip() or (meta.get("image_url") or "").strip()
            row["_url"] = url
            row["_image_path"] = ""
            # 默认不下载图片，直接保存最多 3 个产品图片链接；如需嵌入可开启 download_images
            if img and img.startswith("http") and cfg["export"].get("download_images", False):
                first = img.splitlines()[0].strip()
                path = scraper.download_image(
                    first, os.path.join(config_mod.get_output_dir(cfg), "images"), i,
                    row.get("产品名称") or "", cfg)
                if path:
                    row["_image_path"] = path
                    row["参考图片"] = path
                    row["参考图"] = path
            else:
                img_text = scraper.format_reference_images(img)
                row["参考图片"] = img_text
                row["参考图"] = img_text
            print("✔ 完成：" + str(row.get("产品名称") or row.get("商品名称") or url))
            log_manager.log_scrape(url, cat, args.mode, ok=True,
                                   duration_ms=(time.time() - _t0) * 1000,
                                   row=row, logs=_log_buf, cfg=cfg)
            return i, row, None
        except Exception as exc:
            print(f"✘ {url} 失败：{exc}")
            cat = scraper.data_category(url)
            row = {k: "" for k in scraper.FIELD_KEYS}
            if cat == "京东":
                row.update({k: "" for k in scraper.JD_FIELD_KEYS})
            row["_data_category"] = cat
            row["_url"] = url
            row["_image_path"] = ""
            log_manager.log_scrape(url, cat, args.mode, ok=False, error=str(exc),
                                   duration_ms=(time.time() - _t0) * 1000,
                                   row=row, logs=_log_buf, cfg=cfg)
            return i, row, exc

    workers = max(1, int(cfg["scraper"].get("max_workers", 4)))
    print(f"使用 {workers} 个线程并行抓取 {len(urls)} 个网址…")
    rows = [None] * len(urls)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, row, _err in pool.map(task, enumerate(urls, 1)):
            rows[i - 1] = row

    if not args.export:
        out = os.path.join(config_mod.get_output_dir(cfg),
                           f"产品信息表_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
    else:
        out = args.export
    exporter.export_excel(rows, out, cfg, on_log=print)  # 类别由行内 _data_category 自动推断
    print("已导出：" + out)
    return 0


def main():
    # Windows 控制台默认 GBK，改为 UTF-8 输出，避免中文乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="产品信息自动抓取工具")
    parser.add_argument("--cli", action="store_true", help="命令行模式（不启动图形界面）")
    parser.add_argument("--url", nargs="+", default=[], help="产品详情页地址")
    parser.add_argument("--mode", choices=["direct", "ai", "auto"], default="auto",
                        help="抓取模式：direct=直接抓取，ai=AI智能抓取，auto=自动抓取（默认）")
    parser.add_argument("--export", default="", help="导出的 Excel 文件路径")
    parser.add_argument("--config", default="", help="配置文件路径（默认 config.json）")
    args = parser.parse_args()

    if args.cli:
        sys.exit(run_cli(args))

    import customtkinter as ctk
    from app_gui import ProductApp

    root = ctk.CTk()
    # 先隐藏窗口并把透明度设为 0：整个构建期间绝不闪现任何窗口
    root.withdraw()
    root.attributes('-alpha', 0.0)
    app = ProductApp(root)
    # 等界面全部构建、布局完成后再一次性显示完整窗口：
    # 先以完全透明状态完成首次绘制，再恢复不透明度，
    # 避免“小窗→慢慢展开”的首帧闪烁
    root.update_idletasks()
    root.deiconify()
    root.update()
    root.attributes('-alpha', 1.0)
    root.update()
    root.mainloop()


if __name__ == "__main__":
    main()
