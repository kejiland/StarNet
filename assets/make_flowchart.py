# -*- coding: utf-8 -*-
"""产品信息自动抓取工具 —— 项目流程图生成脚本。

用 PIL 绘制“项目整体架构 + 数据流”流程图，输出 PNG。
运行：python assets/make_flowchart.py
"""
import os
from PIL import Image, ImageDraw, ImageFont

# ---------- 主题 ----------
BLUE = (59, 142, 208)          # 主蓝 #3B8ED0
BLUE_D = (31, 106, 165)
BLUE_BG = (235, 244, 252)      # 节点浅蓝底
GREEN = (46, 125, 50)
GREEN_BG = (236, 246, 236)
DEEP = (20, 30, 48)
GRAY = (90, 100, 115)
LANE_BG = (247, 249, 252)
LANE_LINE = (214, 222, 232)
WHITE = (255, 255, 255)
ARROW = (110, 120, 135)

FONT_PATH = r"C:\Windows\Fonts\msyh.ttc"
F = None


def font(size, bold=False):
    global F
    try:
        return ImageFont.truetype(FONT_PATH, size, index=0 if not bold else 0)
    except Exception:
        return ImageFont.load_default()


def text_w(d, s, f):
    b = d.textbbox((0, 0), s, font=f)
    return b[2] - b[0]


def draw_text(d, cx, cy, s, f, fill, anchor="mm"):
    d.text((cx, cy), s, font=f, fill=fill, anchor=anchor)


def draw_node(d, x, y, w, h, title, lines, fill=BLUE_BG, border=BLUE, title_fill=DEEP):
    """圆角矩形节点：标题(粗体) + 若干正文行(居中)。"""
    d.rounded_rectangle([x, y, x + w, y + h], radius=12, fill=fill,
                        outline=border, width=2)
    tf = font(16, True)
    bf = font(13)
    cy = y + 24
    draw_text(d, x + w / 2, cy, title, tf, title_fill)
    if lines:
        cy += 6
        for ln in lines:
            cy += 20
            draw_text(d, x + w / 2, cy, ln, bf, GRAY)


def draw_arrow_v(d, x, y0, y1, label=""):
    """竖直箭头（带三角头）。"""
    d.line([x, y0, x, y1 - 8], fill=ARROW, width=2)
    d.polygon([(x, y1), (x - 6, y1 - 9), (x + 6, y1 - 9)], fill=ARROW)
    if label:
        draw_text(d, x + 14, (y0 + y1) / 2, label, font(12), GRAY, anchor="lm")


def draw_arrow_h(d, x0, x1, y, label=""):
    d.line([x0 + 8, y, x1 - 8, y], fill=ARROW, width=2)
    d.polygon([(x1, y), (x1 - 9, y - 6), (x1 - 9, y + 6)], fill=ARROW)
    if label:
        draw_text(d, (x0 + x1) / 2, y - 10, label, font(12), GRAY)


def lane(d, y0, y1, label):
    d.rectangle([0, y0, W, y1], fill=LANE_BG)
    d.line([0, y0, W, y0], fill=LANE_LINE, width=1)
    d.line([0, y1, W, y1], fill=LANE_LINE, width=1)
    draw_text(d, 40, (y0 + y1) / 2, label, font(18, True), BLUE_D, anchor="lm")


W, H = 1560, 1780
img = Image.new("RGB", (W, H), WHITE)
d = ImageDraw.Draw(img)

# 标题
draw_text(d, W / 2, 40, "产品信息自动抓取工具 —— 项目流程图", font(26, True), DEEP)

# ============ 泳道 1：启动入口 ============
lane(d, 78, 235, "入口层")
draw_node(d, 480, 108, 600, 100, "启动 main.py", [
    "命令行模式 --cli：批量抓取 + 导出 Excel",
    "图形界面模式：启动 app_gui.ProductApp",
])
# ============ 泳道 2：GUI ============
lane(d, 235, 600, "GUI 操作层\napp_gui.py")
draw_node(d, 430, 268, 700, 78, "输入地址列表（每行一个）+ 选择模式", ["直接抓取 / AI 智能抓取 / 自动抓取(推荐)"])
draw_node(d, 430, 370, 700, 78, "点击抓取 → 任务队列（可暂停）", ["ThreadPool 并行 N 个，逐个 _capture_one，日志+进度"])
draw_node(d, 430, 472, 700, 78, "单条抓取 _capture_one(url, mode, i)", ["自动模式：缺字段 → AI 补全（ai_extractor）"])
draw_arrow_v(d, 780, 388, 442)
draw_arrow_v(d, 780, 490, 544)
# ============ 泳道 3：抓取引擎 ============
lane(d, 600, 1080, "抓取引擎\nscraper.py")
draw_node(d, 430, 630, 700, 82, "1. 识别站点类别", ["京东（is_jd_url） / 普通站点"])
draw_node(d, 430, 736, 700, 92, "2. 请求网页 fetch_html", ["requests 直抓 → 内容过少/反爬验证页？→ 浏览器渲染重试"])
draw_node(d, 430, 852, 700, 82, "3. 浏览器渲染（Playwright）", ["串行锁 + 京东登录 Cookie 注入 + 页面接口捕获内嵌"])
draw_node(d, 430, 958, 700, 82, "4. 解析 HTML / 接口数据 → 字段提取", ["属性表=商品详情 · 参数配置=商品参数 · 价格 · 图片"])
draw_arrow_v(d, 780, 712, 802)
draw_arrow_v(d, 780, 828, 914)
draw_arrow_v(d, 780, 934, 1020)
# ============ 泳道 4：京东分支 & 辅助能力 ============
lane(d, 1080, 1510, "京东分支 &\n辅助能力")
# 左列：京东特殊处理
draw_node(d, 60, 1112, 700, 78, "落地页/活动页 → 提取商品 SKU", ["re.m.jd.com 跳转 item.jd.com"])
draw_node(d, 60, 1214, 700, 86, "商品详情：属性表(.attrs) / 接口描述", ["品牌·编号·型号·制动方式…（DOM + 描述接口）"])
draw_node(d, 60, 1324, 700, 92, "商品参数：参数配置（长图OCR优先）", ["DOM文本 → 长图尾部→中部扫描 → 参数图 → 兜底"])
draw_node(d, 60, 1440, 700, 50, "接口补全：价格 / 详情 / 参数", ["p.3.cn · getWareBusiness · description"])
# 右列：辅助能力
draw_node(d, 800, 1112, 700, 78, "本地 OCR（RapidOCR）", ["详情长图前 3 张 · 参数长图扫描（识别图中文字）"])
draw_node(d, 800, 1214, 700, 86, "AI 补全（ai_extractor.py）", ["DeepSeek 等模型：缺字段自动补全（自动模式）"])
draw_node(d, 800, 1324, 700, 92, "日志 / 图片下载", ["log_manager 每次抓取留档 · 参考图下载"])
draw_node(d, 800, 1440, 700, 50, "配置 config.json", ["AI 模型 · 并发数 · OCR 开关 · 输出目录"])
# ============ 泳道 5：输出 ============
lane(d, 1510, 1760, "输出层")
draw_node(d, 60, 1548, 440, 150, "结果表格", ["双击单元格编辑\n右键更多操作\nCtrl/Shift 多选行"])
draw_node(d, 560, 1548, 440, 150, "导出 Excel（exporter.py）", ["按数据类别分表\nopenpyxl 生成 .xlsx"])
draw_node(d, 1060, 1548, 440, 150, "自动保存 / 参考图", ["未导出数据.json 断点保存\n参考图片/本地图片管理"])
# 泳道 4 → 5 汇聚箭头
draw_arrow_v(d, 410, 1490, 1548)
draw_arrow_v(d, 1150, 1490, 1548)

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs")
os.makedirs(out, exist_ok=True)
p = os.path.join(out, "项目流程图.png")
img.save(p, "PNG")
print("已生成:", os.path.abspath(p))
print("尺寸: %dx%d" % (W, H))