# -*- coding: utf-8 -*-
"""网页直接抓取模块：使用 requests + BeautifulSoup 从产品详情页提取结构化信息。

- 普通页面直接用 requests 抓取，速度快；
- 遇到反爬 JS 验证（如 EO-Bot / 加速乐）或纯动态渲染页面时，
  自动改用 Playwright 浏览器内核渲染后抓取，确保拿到的是页面真实内容；
- 分区提取支持“近义标题”匹配（如“规格参数”→“设备参数”、
  “应用场景”→“用途”、“功能特性”→“性能特点”），并识别产品分类。
"""

import os
import json
import re
import threading
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

FIELD_KEYS = ("产品名称", "用途", "设备介绍", "性能特点", "设备参数", "参考图片")

# 京东数据类别对应的字段（表头按数据类别切换）
JD_FIELD_KEYS = ("商品名称", "商品详情", "商品参数", "商品价格", "参考图")

# 图片 alt/title 说明中的通用噪音词（logo、图标、二维码等）
_IMG_ALT_SKIP = re.compile(r"(logo|图标|icon|二维码|购物车|搜索|京东|首页|加载中|返回|扫码|登录|注册)", re.I)

# 表头 → 网页常见近义标题（越靠前越优先、越具体）
_SECTION_KEYWORDS = {
    "用途": ["用途", "应用场景", "应用领域", "适用场景", "适用范围", "应用范围",
             "典型应用", "应用行业", "适用行业", "行业应用", "使用场景", "主要用途",
             "产品用途", "应用"],
    "设备介绍": ["产品介绍", "设备介绍", "产品简介", "产品概述", "产品说明", "产品详情",
               "设备概述", "产品描述", "设备描述", "产品展示", "介绍", "概述", "简介",
               "说明", "描述"],
    "性能特点": ["性能特点", "产品特点", "设备特点", "性能优势", "产品优势", "技术特点",
               "功能特点", "核心功能", "产品特性", "功能特性", "主要特性", "核心特性",
               "功能亮点", "特点优势", "亮点", "特点", "特性", "优势", "性能"],
    "设备参数": ["设备参数", "技术参数", "产品参数", "技术规格", "产品规格", "规格参数",
               "技术指标", "主要参数", "详细参数", "参数详情", "性能参数", "配置参数",
               "基本参数", "规格", "参数"],
}

_DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_DEFAULT_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
                      "Mobile/15E148 Safari/604.1")

# 反爬 JS 验证特征（EO-Bot / 加速乐 / 瑞数等）
_CHALLENGE_MARKS = ("solveChallenge", "EO-Bot", "__jsl", "acw_sc__v2",
                    "jsluid_s", "window.solveChallenge")

_SKIP_TAGS = ("script", "style", "noscript", "nav", "header", "footer",
              "aside", "form", "button", "select", "iframe")

# 浏览器渲染全局锁：同一时间只允许一个线程渲染（避免批量抓取时多开浏览器）
_JS_RENDER_LOCK = threading.Lock()


def normalize_url(url):
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def is_jd_url(url):
    """判断是否为京东地址（商品详情页 / 移动端 / 活动落地页）。

    包含 item.jd.com 桌面商品页、item.m.jd.com 移动商品页，以及用户常发的
    re.m.jd.com 活动/落地页等京东域名，便于统一使用移动 UA、登录 Cookie
    与页面内接口补充。
    """
    u = (url or "").lower()
    for host in ("item.jd.com", "item.m.jd.com", "item.jd.hk",
                 "re.m.jd.com", "m.jd.com", "pj.jd.com"):
        if host in u:
            return True
    return re.search(r"(^|\.|/)jd\.com/(?:[a-z0-9_-]+/)*\d+\.html", u) is not None


def is_jd_landing(url):
    """判断是否为京东活动页/落地页（如 re.m.jd.com/page/homelike）。

    这类页面本身没有商品详情，需要从中提取商品编号后转到真实商品页。
    """
    u = (url or "").lower()
    if "re.m.jd.com" in u:
        return True
    if ("m.jd.com" in u or "jd.com" in u) and "item." not in u:
        return re.search(r"/\d{6,}\.html", u) is None
    return False


def _jd_extract_sku(soup_or_html, url=""):
    """从京东地址或页面内嵌数据中提取商品编号（SKU），找不到返回空串。

    落地页（re.m.jd.com）的 HTML 里通常带有真实商品链接或 skuId 数据：
    - requests 直抓的落地页壳里链接是 JSON 转义的斜杠（item.jd.com\\/101...html）；
    - 浏览器渲染后的 DOM 里是未转义的真实链接。
    两种都兼容。
    """
    if url:
        m = re.search(r"/(?:product/)?(\d{6,})\.html", url or "")
        if m:
            return m.group(1)
        m = re.search(r"skuId=(\d{6,})", url, re.I)
        if m:
            return m.group(1)
    raw = str(soup_or_html) if not isinstance(soup_or_html, str) else soup_or_html
    # 转义斜杠 \\/ 与普通斜杠 / 都接受（\\ 在正则里匹配一个字面反斜杠）
    for pat in (r'"skuId"\s*:\s*"??(\d{6,})', r"'skuId'\s*:\s*'(\d{6,})'",
                r'"wareId"\s*:\s*"??(\d{6,})',
                r"(?:\\/|/)?product(?:\\/|/)(\d{6,})\.html",
                r"item\.jd\.com(?:\\/|/)(\d{6,})\.html",
                r"item\.m\.jd\.com(?:\\/|/)product(?:\\/|/)(\d{6,})",
                r'"(?:id|skuId|wareId)"\s*:\s*"(\d{10,})"'):
        m = re.search(pat, raw)
        if m:
            return m.group(1)
    return ""


def is_taobao_url(url):
    """判断是否为淘宝/天猫商品详情页地址。"""
    u = (url or "").lower()
    return any(x in u for x in ("item.taobao.com", "detail.tmall.com",
                                "detail.tmall.hk", "world.taobao.com",
                                "item.tmall.com"))


def is_pdd_url(url):
    """判断是否为拼多多商品详情页地址。"""
    u = (url or "").lower()
    return any(x in u for x in ("yangkeduo.com", "pinduoduo.com"))


def jd_cookies_file(cfg=None):
    """京东登录状态（Cookie）保存文件路径。"""
    if cfg:
        out = cfg["export"].get("output_dir") or "output"
        if not os.path.isabs(out):
            out = os.path.join(os.path.dirname(os.path.abspath(__file__)), out)
    else:
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    return os.path.join(out, "jd_cookies.json")


def load_jd_cookies(cfg=None):
    """读取已保存的京东登录 Cookie（供浏览器渲染时注入）。"""
    path = jd_cookies_file(cfg)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get("cookies") or []
        if isinstance(data, list):
            return [c for c in data if isinstance(c, dict)]
    except Exception:
        pass
    return []


def save_jd_cookies(cookies, cfg=None):
    """保存京东登录 Cookie 到本地文件，之后抓取会自动复用。"""
    path = jd_cookies_file(cfg)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "cookies": cookies}, f, ensure_ascii=False, indent=2)
        return path
    except Exception:
        return ""


def has_jd_session(cfg=None):
    """是否已保存可用的京东登录状态（忽略空值或已过期的 Cookie）。"""
    return jd_session_status(cfg)[0] == "valid"


def clear_jd_cookies(cfg=None):
    """清除已保存的京东登录状态，返回是否清除成功。"""
    path = jd_cookies_file(cfg)
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
    except Exception:
        pass
    return False

def jd_storage_file(cfg=None):
    """京东登录状态（完整浏览器存储 Cookie+localStorage）保存文件路径。"""
    return os.path.join(os.path.dirname(jd_cookies_file(cfg)), "jd_storage.json")


def load_jd_storage(cfg=None):
    """读取已保存的京东完整登录状态（Playwright storage_state）。"""
    path = jd_storage_file(cfg)
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        st = d.get("state") if isinstance(d, dict) else None
        if isinstance(st, dict) and isinstance(st.get("cookies"), list):
            return st
    except Exception:
        pass
    return {}


def save_jd_storage(state, cfg=None):
    """保存京东完整登录状态（storage_state）到本地，之后抓取自动复用。"""
    path = jd_storage_file(cfg)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "state": state}, f, ensure_ascii=False, indent=2)
        return path
    except Exception:
        return ""


def jd_session_status(cfg=None):
    """判断已保存的京东登录状态：none（无）/ valid（有效）/ expired（已过期）。

    返回 (状态, 有效 Cookie 数量)。
    """
    cks = load_jd_cookies(cfg)
    # 只有 pt_key/pt_pin/pt_token 才是真实登录凭证；
    # unick/pwdt_id/sso.jd.com 等只是基础 Cookie，不代表已登录，
    # 否则会误报“已登录”导致用户以为详情/价格能正常抓到
    auth = [c for c in cks if (c.get("name") or "").lower()
            in ("pt_key", "pt_pin", "pt_token")]
    if not auth:
        return "none", 0
    now = time.time()
    alive = expired = 0
    for c in auth:
        if not (c.get("value") or "").strip():
            continue
        exp = c.get("expires")
        try:
            if exp and float(exp) > 0 and float(exp) < now:
                expired += 1
            else:
                alive += 1
        except Exception:
            alive += 1
    if alive:
        return "valid", alive
    if expired:
        return "expired", expired
    return "none", 0




def login_jd(cfg, on_log=None, timeout=300):
    """打开可见浏览器让用户登录京东（扫码/账号），成功后把 Cookie 保存到本地。

    返回 True 表示登录成功；False 表示用户关闭窗口或超时。
    """
    def log(msg):
        if on_log:
            on_log(msg)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ValueError(
            "未安装 Playwright，无法打开京东登录窗口。\n"
            "请先安装：pip install playwright 后执行 playwright install chromium")

    timeout = max(60, int(timeout or 300))
    ua = cfg["scraper"].get("user_agent") or _DEFAULT_UA
    auth_names = ("pt_key", "pt_pin", "pt_token", "unick", "pwdt_id")
    with sync_playwright() as p:
        browser = None
        try:
            # 优先使用本机已安装的 Chrome，界面更接近日常使用
            browser = p.chromium.launch(
                headless=False, channel="chrome",
                args=["--disable-blink-features=AutomationControlled"])
        except Exception:
            browser = p.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"])
        try:
            ctx = browser.new_context(user_agent=ua, locale="zh-CN",
                                      ignore_https_errors=True,
                                      viewport={"width": 1280, "height": 860})
            page = ctx.new_page()
            log("已打开京东登录窗口，请在窗口中扫码或账号登录…")
            page.goto("https://passport.jd.com/new/login.aspx",
                      wait_until="domcontentloaded", timeout=60000)
            start = time.time()
            while time.time() - start < timeout:
                try:
                    if page.is_closed():
                        log("登录窗口已关闭，未完成登录。")
                        return False
                    cookies = ctx.cookies()
                    if any((c.get("name") or "").lower() in auth_names for c in cookies):
                        save_jd_cookies(cookies, cfg)
                        try:
                            save_jd_storage(ctx.storage_state(), cfg)
                        except Exception:
                            pass
                        log("京东登录成功，已保存完整登录状态，之后抓取会自动复用。")
                        return True
                except Exception:
                    try:
                        if page.is_closed():
                            log("登录窗口已关闭，未完成登录。")
                            return False
                    except Exception:
                        pass
                page.wait_for_timeout(1500)
            log("登录超时，未检测到登录成功（请确认扫码后停留片刻）。")
            return False
        finally:
            try:
                browser.close()
            except Exception:
                pass


def data_category(url, final_url=""):
    """返回数据类别：京东/淘宝/拼多多商品页返回对应类别，其余为“常规”。

    数据类别用于切换表格表头：常规/京东/淘宝/拼多多。
    """
    if is_jd_url(url) or is_jd_url(final_url):
        return "京东"
    if is_taobao_url(url) or is_taobao_url(final_url):
        return "淘宝"
    if is_pdd_url(url) or is_pdd_url(final_url):
        return "拼多多"
    return "常规"


def category_field_keys(category):
    """返回数据类别对应的内容字段（不含“序号”）。"""
    return JD_FIELD_KEYS if str(category or "") == "京东" else FIELD_KEYS


# 字段缺失判定时的同义词兜底（字段本身为空时看对应字段）
_MISSING_SYNONYMS = {
    "商品名称": ["产品名称"],
    "商品详情": ["设备介绍"],
    "商品参数": ["设备参数"],
    "参考图": ["参考图片"],
    "参考图片": ["参考图"],
}


def missing_fields(row, category):
    """返回当前数据类别下为空（含同义词兜底后仍为空）的字段列表。

    跳过参考图片/参考图（图片缺失不做 AI 补全判定）。
    """
    row = row or {}
    missing = []
    for f in category_field_keys(category):
        if f in ("参考图片", "参考图"):
            continue
        val = row.get(f) or ""
        if not str(val).strip():
            for alt in _MISSING_SYNONYMS.get(f, []):
                val = row.get(alt) or ""
                if str(val).strip():
                    break
        if not str(val).strip():
            missing.append(f)
    return missing


def _clean(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def _resolve(src, base_url):
    if not src:
        return ""
    return urljoin(base_url, src)


def _in_skip(el):
    node = el
    while node is not None:
        if node.name in ("nav", "header", "footer", "aside"):
            return True
        node = node.parent
    return False


def _looks_like_challenge(html):
    """判断是否为反爬 JS 验证页（只有一段混淆脚本、没有真实正文）。"""
    head = (html or "")[:8000]
    return any(m in head for m in _CHALLENGE_MARKS)


def _plain_len(html):
    """粗略统计页面纯文本长度（用于判断渲染结果是否有效）。"""
    try:
        return len(BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True))
    except Exception:
        return 0


_SITE_SUFFIX = re.compile(
    r"(海康|威视|hikvision|官网|官方网站|产品详情页|产品中心|首页|百科|有限公司|集团|股份|科技|技术|智能|服务支持|support|official)", re.I)


def _clean_title(text):
    """清理产品名称：去掉尾部“ - 站点名/官网”之类的后缀。"""
    t = _clean(text)
    while True:
        m = re.search(r"\s*[-|｜]\s*([^-|｜]{1,25})$", t)
        if not m:
            break
        if _SITE_SUFFIX.search(m.group(1)):
            t = t[:m.start()].rstrip()
        else:
            break
    return t


def _best_content_ul(soup):
    """找“最有内容”的列表（跳过纯链接菜单、参数表），用于性能特点兜底。"""
    best_el, best_n = None, 0
    for ul in soup.find_all("ul"):
        chain = " ".join(" ".join(el.get("class") or []) for el in ul.parents)
        own = " ".join(ul.get("class") or []) + " " + (ul.get("id") or "")
        if re.search(r"spec|param|accordion|tech-|menu|nav", chain + " " + own, re.I):
            continue
        lis = [li for li in ul.find_all("li", recursive=False)
               if not (li.find("a") and not li.find(["span", "p", "div", "ul", "ol", "img"]))]
        if not lis:
            continue
        rows = sum(1 for li in lis if li.find(class_="item-title"))
        if rows >= max(2, len(lis) // 2):
            continue  # 参数行列表，跳过
        if len(lis) > best_n:
            best_el, best_n = ul, len(lis)
    return best_el if best_n >= 3 else None

def _proxy_dict(cfg):
    """从配置读取代理，返回 requests 用的 proxies 字典；未配置返回 None。

    config.json 里 scraper.proxy 可填：http://127.0.0.1:7890 或 socks5://127.0.0.1:1080
    （换代理 IP 是当前绕过京东“PC频控”限流最实用的方式）。
    """
    try:
        p = (cfg.get("scraper") or {}).get("proxy") or ""
    except Exception:
        return None
    p = str(p or "").strip()
    if not p:
        return None
    if "://" not in p:
        p = "http://" + p
    return {"http": p, "https": p}




def _jd_warm_jd_session(cfg, jd_cookies=None):
    """京东请求前先访问一次 jd.com 首页，取得 __jdu 等基础 Cookie，
    可显著减少直接访问商品页被重定向到“首页壳/登录页”的概率。"""
    try:
        s = requests.Session()
        s.headers.update({
            "User-Agent": cfg["scraper"].get("user_agent") or _DEFAULT_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        if jd_cookies:
            s.cookies.update(jd_cookies or {})
        verify = bool(cfg["scraper"].get("verify_ssl", True))
        timeout = int(cfg["scraper"].get("timeout", 15))
        _px = _proxy_dict(cfg)
        s.get("https://www.jd.com/", timeout=timeout, verify=verify, allow_redirects=True,
              proxies=_px)
        return s
    except Exception:
        return None


def _fetch_html_http(url, cfg, on_log=None):
    """用 requests 抓取网页，返回 (html, final_url, encoding)。失败抛异常。"""
    # 京东移动版页面必须用手机 UA，否则容易被重定向到桌面/验证页
    if "item.m.jd.com" in (url or "").lower():
        _hd_ua = _DEFAULT_MOBILE_UA
    else:
        _hd_ua = cfg["scraper"].get("user_agent") or _DEFAULT_UA
    headers = {
        "User-Agent": _hd_ua,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    }
    verify = bool(cfg["scraper"].get("verify_ssl", True))
    timeout = int(cfg["scraper"].get("timeout", 15))
    max_retries = max(1, int(cfg["scraper"].get("max_retries", 2)) + 1)
    last_exc = None
    jd_cookies = None
    jd_session = None
    if is_jd_url(url):
        _jd_cks = load_jd_cookies(cfg)
        if _jd_cks:
            jd_cookies = {}
            for _c in _jd_cks:
                if _c.get("name") and _c.get("value"):
                    jd_cookies[_c["name"]] = _c["value"]
        jd_session = _jd_warm_jd_session(cfg, jd_cookies)
        if jd_session is not None and jd_cookies:
            jd_session.cookies.update(jd_cookies or {})
    for attempt in range(max_retries):
        try:
            req_headers = headers
            if jd_session is not None:
                req_headers = dict(headers)
                req_headers["Referer"] = "https://www.jd.com/"
                try:
                    resp = jd_session.get(url, headers=req_headers, timeout=timeout, verify=verify,
                                          proxies=_proxy_dict(cfg))
                except Exception:
                    resp = requests.get(url, headers=req_headers, timeout=timeout,
                                        verify=verify, cookies=jd_cookies,
                                        proxies=_proxy_dict(cfg))
            else:
                resp = requests.get(url, headers=req_headers, timeout=timeout,
                                    verify=verify, cookies=jd_cookies,
                                    proxies=_proxy_dict(cfg))
            resp.raise_for_status()
            if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "ascii"):
                resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text, resp.url, resp.encoding
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                time.sleep(1.0 + attempt)
    raise last_exc


def _fetch_html_js(url, cfg, on_log=None):
    """用 Playwright 浏览器内核渲染后抓取，返回 (html, final_url, 'utf-8')。

    专用于反爬验证页 / 动态渲染页。为线程安全与资源可控，
    每次调用独立启动、关闭浏览器，并用全局锁串行执行。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ValueError(
            "该页面需要浏览器渲染才能抓取，但当前环境未安装 Playwright。\n"
            "请安装：pip install playwright 后执行 playwright install chromium")

    wait_ms = max(500, int(cfg["scraper"].get("js_wait_ms", 5000)))
    timeout_ms = max(10000, int(cfg["scraper"].get("js_timeout", 60000)))
    ua = cfg["scraper"].get("user_agent") or _DEFAULT_UA
    jd_mobile = "item.m.jd.com" in url
    if jd_mobile:
        ua = _DEFAULT_MOBILE_UA  # 移动版页面用手机 UA，避免被重定向到桌面登录页
    viewport = {"width": 390, "height": 844} if jd_mobile else {"width": 1366, "height": 900}

    with _JS_RENDER_LOCK:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
            try:
                last_err = None
                _jd_mode = is_jd_url(url) or re.search(r"\.jd\.com", url or "") is not None
                # 未登录时京东必跳登录页/频控，5 次 attempt 只会反复空转（每次几十秒），
                # 压缩到 2 次；仅已登录（有 pt_key）时才按 5 次从容应对风控波动
                _jd_attempts = 5 if (_jd_mode and jd_session_status(cfg)[0] == "valid") else 2
                # 首次迭代时页面尚未导航，final_url 先以目标地址兜底，
                # 否则下方 is_jd_url(final_url) 会在赋值前引用 → NameError
                final_url = url
                for attempt in range(_jd_attempts if _jd_mode else 3):
                    ctx = None
                    try:
                        _px = _proxy_dict(cfg)
                        _pw_kwargs = dict(user_agent=ua, locale="zh-CN",
                                          ignore_https_errors=True, viewport=viewport)
                        if _px:
                            _pw_kwargs["proxy"] = {"server": _px.get("https") or _px.get("http")}
                        if is_jd_url(url):
                            # 优先加载完整登录状态（Cookie + localStorage），比单 Cookie 更持久
                            _st = load_jd_storage(cfg)
                            if _st and isinstance(_st.get("cookies"), list) and _st.get("cookies"):
                                try:
                                    _pw_kwargs["storage_state"] = _st
                                except Exception:
                                    pass
                        try:
                            ctx = browser.new_context(**_pw_kwargs)
                        except Exception:
                            # 存储状态损坏/不兼容时，退化为不带登录状态新建上下文
                            _pw_kwargs.pop("storage_state", None)
                            ctx = browser.new_context(**_pw_kwargs)
                        if is_jd_url(url):
                            _jd_cookies = load_jd_cookies(cfg)
                            if _jd_cookies:
                                try:
                                    ctx.add_cookies(_jd_cookies)
                                    if on_log and attempt == 0:
                                        on_log("已加载京东登录状态")
                                except Exception as exc:
                                    if on_log and attempt == 0:
                                        on_log("加载京东登录状态失败：%s" % exc)

                        page = ctx.new_page()
                        # 监听京东页面自己发起的接口请求（pc_detailpage_wareBusiness / getWareBusiness / 描述接口），
                        # 用 response 事件读取真实返回（不阻塞页面渲染：route.fetch 会挂起页面请求，
                        # 导致正文迟迟渲染不出来）。解决详情/参数/价格异步加载且需要签名才能调用的问题。
                        _jd_captured = []
                        if is_jd_url(url):
                            _jd_patterns = ("pc_detailpage_wareBusiness", "getWareBusiness",
                                            "description/channel", "api.m.jd.com",
                                            "item-soa.jd.com", "soa.jd.com", "getDetail")
                            def _jd_on_response(_resp):
                                try:
                                    _u = _resp.url
                                    if not any(_k in _u for _k in _jd_patterns):
                                        return
                                    _t = _resp.text()
                                    _j = None
                                    _s = (_t or "").strip()
                                    if _s.startswith(("{", "[")):
                                        try:
                                            _j = json.loads(_s)
                                        except Exception:
                                            _j = None
                                    elif "description" in _u or "cd.jd.com" in _u:
                                        # 描述接口可能返回 JSONP：jQuery...( {...} )
                                        _lo = _s.find("(")
                                        _hi = _s.rfind(")")
                                        if _lo >= 0 and _hi > _lo:
                                            try:
                                                _j = json.loads(_s[_lo + 1:_hi])
                                            except Exception:
                                                _j = None
                                    if isinstance(_j, (dict, list)):
                                        _jd_captured.append({"url": _u, "json": _j})
                                except Exception:
                                    pass
                            page.on("response", _jd_on_response)
                        resp = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                        if resp is not None and resp.status >= 400:
                            raise RuntimeError(f"HTTP {resp.status} {resp.status_text}")
                        try:
                            page.wait_for_load_state("networkidle", timeout=min(wait_ms, 10000))
                        except Exception:
                            pass
                        page.wait_for_timeout(min(wait_ms, 10000))
                        if is_jd_url(url) or is_jd_url(final_url):
                            # 京东移动版页面在模拟滚动后会被页面 JS 风控检测并强制跳转到登录页
                            # （导致正文变成登录页、Cookie 失效）。详情/参数走接口补充，
                            # 因此这里不做滚动，仅多等一会儿让首屏完整渲染。
                            page.wait_for_timeout(1500)
                        # 等待正文真正渲染出来再取（page.content() 在跳转期间会抛异常，
                        # 因此先等待内容就绪，取不到再退化为 outerHTML）
                        html = None
                        for _i in range(4):
                            try:
                                html = page.content()
                            except Exception:
                                try:
                                    page.wait_for_load_state("domcontentloaded", timeout=5000)
                                except Exception:
                                    pass
                                try:
                                    html = page.evaluate("document.documentElement.outerHTML")
                                except Exception:
                                    html = None
                            if html and _plain_len(html) >= 300 and not _looks_like_challenge(html):
                                break
                            page.wait_for_timeout(1500)
                        if not html:
                            raise RuntimeError("无法获取渲染后的页面内容")
                        final_url = page.url or url
                        if _jd_mode and _JD_LOGIN_URL.search(final_url or ""):
                            _st_now, _n_now = jd_session_status(cfg)
                            if _st_now != "valid":
                                # 未检测到有效登录：京东必把访问重定向到登录页，
                                # 再重试也只是反复空转（每次几十秒），直接返回当前页
                                # 交给上层 stub 判定；详情/参数走接口补充或提示登录。
                                if on_log:
                                    on_log("未检测到有效京东登录（访问被重定向到登录页），"
                                           "不再重试；如需详情/参数/价格请先登录")
                                return html, final_url, "utf-8"
                            if on_log:
                                on_log("京东将本次访问重定向到登录页（反爬波动），正在换上下文重试…")
                            time.sleep(0.8 + attempt * 0.6)
                            continue
                        if (not _looks_like_challenge(html)) and _plain_len(html) >= 300:
                            # 京东商品详情/参数/价格是页面 JS 异步加载的，HTML 里没有；
                            # 这里直接在浏览器上下文内调用京东公开接口补充并内嵌回 HTML
                            if is_jd_url(url) or is_jd_url(final_url):
                                _sku_m = re.search(r"/(\d{6,})\.html", final_url or url)
                                if _sku_m:
                                    _api_data = _jd_fetch_api_in_page(page, _sku_m.group(1), cfg, on_log)
                                    _cap_data = _jd_parse_captured(_jd_captured, _sku_m.group(1), on_log)
                                    _merged = dict(_cap_data or {})
                                    _merged.update(_api_data or {})
                                    if _merged:
                                        html = _jd_embed_api(html, _merged)
                                        if on_log:
                                            on_log("已内嵌京东真实接口数据（页面接口+补充接口）")
                            # 自动续期：抓到的是真实京东页且带登录，就把最新 Cookie/存储保存下来，
                            # 下次重启程序直接复用，无需重新扫码登录
                            if is_jd_url(url) or is_jd_url(final_url):
                                try:
                                    _cur = ctx.cookies()
                                    if any((c.get("name") or "").lower() in
                                           ("pt_key", "pt_pin") for c in _cur):
                                        save_jd_cookies(_cur, cfg)
                                        try:
                                            save_jd_storage(ctx.storage_state(), cfg)
                                        except Exception:
                                            pass
                                        if on_log:
                                            on_log("已自动刷新并保存京东登录状态（下次免登录）")
                                except Exception:
                                    pass
                            return html, final_url, "utf-8"
                        time.sleep(1.0)  # 等待验证 Cookie 生效后重新加载
                    except Exception as exc:
                        last_err = exc
                    finally:
                        try:
                            if ctx is not None:
                                ctx.close()
                        except Exception:
                            pass
                if last_err is None:
                    last_err = RuntimeError("页面渲染后仍无正文，可能被反爬拦截")
                raise last_err
            finally:
                try:
                    browser.close()
                except Exception:
                    pass


def fetch_html(url, cfg, on_log=None, use_js=False):
    """抓取网页，返回 (html, final_url, encoding)。

    - use_js=True 或配置 scraper.js_render=true 时直接用浏览器渲染；
    - 默认自动模式：requests 抓取后若发现反爬 JS 验证页，自动改用浏览器渲染；
    - 配置 scraper.js_render=false 时完全禁用浏览器渲染。
    """
    js_mode = str(cfg["scraper"].get("js_render", "auto")).lower()
    disabled = js_mode in ("0", "false", "no", "off", "never")
    forced = use_js or js_mode in ("1", "true", "yes", "on", "always")

    if forced and not disabled:
        return _fetch_html_js(url, cfg, on_log)

    html, final_url, enc = _fetch_html_http(url, cfg, on_log)
    if not disabled and _looks_like_challenge(html):
        if on_log:
            on_log("检测到页面反爬 JS 验证，正在用浏览器引擎渲染后抓取…")
        return _fetch_html_js(url, cfg, on_log)
    return html, final_url, enc


def fetch_page_html(url, cfg, on_log=None):
    """抓取网页 HTML（自动处理反爬 JS 验证），正文过短时再用浏览器渲染重试。

    返回 (html, final_url)。
    """
    html, final_url, _enc = fetch_html(url, cfg, on_log=on_log)
    # 京东页面若返回“首页壳/登录页/错误页”（不是真实商品页），也自动重试
    _soup0 = BeautifulSoup(html, "html.parser")
    _jd_stub0 = (is_jd_url(url) or is_jd_url(final_url)) and _jd_stub_detect(_soup0, final_url)
    if _plain_len(html) < 300 or _jd_stub0:
        try:
            html2, final_url2, _enc2 = fetch_html(url, cfg, on_log=on_log, use_js=True)
            _stub2 = (is_jd_url(url) or is_jd_url(final_url2)) and _jd_stub_detect(
                BeautifulSoup(html2, "html.parser"), final_url2)
            if _plain_len(html2) > _plain_len(html) and not _stub2:
                if on_log:
                    on_log("网页正文过短或为京东拦截页，已用浏览器引擎重新抓取")
                return html2, final_url2
        except Exception:
            pass
        # 京东桌面版仍被拦截时，改用移动版商品页
        if _jd_stub0:
            _m = re.search(r"/(\d{6,})\.html", url)
            if _m:
                try:
                    _mu = "https://item.m.jd.com/product/%s.html" % _m.group(1)
                    htmlm, finalm, _encm = fetch_html(_mu, cfg, on_log=on_log)
                    _stubm = _jd_stub_detect(BeautifulSoup(htmlm, "html.parser"), finalm)
                    if not _stubm and _plain_len(htmlm) > _plain_len(html):
                        if on_log:
                            on_log("京东桌面版被拦截，已改用移动版商品页")
                        return htmlm, finalm
                except Exception:
                    pass
    return html, final_url


def fetch_page_text(url, cfg, on_log=None):
    """抓取网页并转成纯文本（供 AI 提取）。返回 (text, final_url)。"""
    html, final_url = fetch_page_html(url, cfg, on_log=on_log)
    return html_to_text(html), final_url


def collect_page_image_urls(html, final_url):
    """从已抓取的 HTML 中收集全部图片 URL，返回列表。"""
    soup = BeautifulSoup(html or "", "html.parser")
    return collect_image_urls(soup, final_url)


def collect_page_product_images(html, final_url):
    """从已抓取的 HTML 中收集本产品相关图片 URL（最多 3 个）。

    京东页面走京东专用图片收集（主图轮播 #spec-list 等）。
    """
    soup = BeautifulSoup(html or "", "html.parser")
    if is_jd_url(final_url) or is_jd_url(html[:30]):
        return collect_jd_product_images(soup, final_url)
    return collect_product_images(soup, final_url)


def html_to_text(html, max_chars=20000):
    """把网页 HTML 转成纯文本（供 AI 提取使用），去掉脚本样式噪音。

    同时把页面图片的 alt/title 文字以“图片说明”块附加到末尾，
    让 AI 能“读”到图片里的关键文字（商品名称、卖点、参数等）。
    """
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    alt_lines, seen = [], set()
    for img in soup.find_all("img"):
        alt = _clean(img.get("alt") or img.get("title") or "")
        if len(alt) >= 4 and not _IMG_ALT_SKIP.search(alt) and alt not in seen:
            seen.add(alt)
            alt_lines.append(alt)
        if len(alt_lines) >= 30:
            break
    text = soup.get_text("\n", strip=True)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    if alt_lines:
        lines.append("【图片中的文字说明】\n" + "\n".join(alt_lines))
    text = "\n".join(lines)
    return text[:max_chars]


def _table_to_lines(table):
    lines = []
    for tr in table.find_all("tr"):
        cells = [_clean(td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if cells:
            lines.append("：".join(cells))
    return lines


def _match_field(heading):
    for field, keywords in _SECTION_KEYWORDS.items():
        for kw in keywords:
            if kw in heading:
                return field
    return None


def _row_lines(el):
    """识别“标题 | 值”式的参数行（如海康 tech-specs 结构）。

    只认直接子元素里成对的 item-title / item-desc，
    避免把整个大容器误判成一行。
    """
    def direct_find(cls):
        for child in el.find_all(recursive=False):
            if cls in (child.get("class") or []):
                return child
        return None

    if el.name not in ("div", "li"):
        return None
    title = direct_find("item-title")
    desc = direct_find("item-desc") if title is not None else None
    if title is not None and desc is not None:
        t = _clean(title.get_text(" ", strip=True))
        d = _clean(desc.get_text(" ", strip=True))
        if t and d:
            return f"{t}：{d}"
    header = direct_find("item-header")
    if header is not None and title is None:
        t = _clean(header.get_text(" ", strip=True))
        if t:
            return f"【{t}】"
    return None


def _walk_lines(el, add):
    """递归提取元素内的文本行，参数行输出为“名称 | 值”，列表输出为“· 条目”。"""
    if not getattr(el, "name", None):
        return
    if el.name in _SKIP_TAGS:
        return
    if el.name == "table":
        for ln in _table_to_lines(el):
            add(ln)
        return
    if el.name in ("ul", "ol"):
        lis = el.find_all("li", recursive=False)
        links = [li for li in lis
                 if li.find("a") and not li.find(["span", "p", "div", "ul", "ol", "img"])]
        if len(links) > 3 and len(links) == len(lis):
            return  # 纯链接的菜单 / 选项卡列表，跳过
        for li in lis:
            r = _row_lines(li)
            if r:
                add(r)
                continue
            if li.find(["ul", "ol"]):
                _walk_lines(li, add)
                continue
            t = _clean(li.get_text(" ", strip=True))
            if t:
                add("· " + t)
        return
    if el.name == "dl":
        for dt in el.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            t = _clean(dt.get_text(" ", strip=True))
            if dd:
                t += "：" + _clean(dd.get_text(" ", strip=True))
            if t:
                add(t)
        return
    if el.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        t = _clean(el.get_text(" ", strip=True))
        if t:
            add(t)
        return
    r = _row_lines(el)
    if r:
        add(r)
        return
    children = [c for c in el.children if getattr(c, "name", None)]
    blocky = [c for c in children if c.name in
              ("div", "p", "ul", "ol", "table", "dl", "section", "article",
               "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote")]
    if blocky:
        for c in children:
            _walk_lines(c, add)
        return
    t = _clean(el.get_text(" ", strip=True))
    if len(t) > 2:
        add(t)


def _extract_section_text(body):
    """把分区元素列表转成文本行（自动去重相邻重复）。"""
    out, seen = [], set()

    def add(line):
        line = _clean(line)
        if not line:
            return
        key = line if len(line) <= 60 else line[:60]
        if key in seen:
            return
        seen.add(key)
        out.append(line)

    for el in body:
        if el is None:
            continue
        if isinstance(el, str):
            add(el)
            continue
        _walk_lines(el, add)
    return "\n".join(out)


def _find_markers(soup):
    """找出页面中的“分区标题”元素（h1-h6 之外，也支持任意短文本标签），
    只取最内层匹配，避免祖先重复。返回 [(元素, 标题文本), ...]。
    """
    markers = []

    def walk(node):
        if not getattr(node, "name", None):
            return False
        if node.name in ("script", "style", "noscript", "nav", "header", "footer", "aside"):
            return True  # 整棵子树视为已处理（跳过）
        child_match = False
        for child in node.children:
            if walk(child):
                child_match = True
        text = _clean(node.get_text(" ", strip=True))
        if 1 <= len(text) <= 30 and _match_field(text) and not child_match:
            markers.append((node, text))
            return True
        return child_match

    walk(soup)
    return markers


def _collect_body(start_node):
    """从 start_node 开始收集兄弟内容，直到遇到下一个分区标题 / 页面级标签。"""
    body = []
    node = start_node
    while node is not None:
        if getattr(node, "name", None) and re.match(r"^h[1-6]$", node.name):
            break
        if getattr(node, "name", None) in ("script", "style", "nav", "header", "footer", "aside"):
            node = node.find_next_sibling()
            continue
        node_text = _clean(node.get_text(" ", strip=True)) if getattr(node, "name", None) else ""
        if node_text and len(node_text) <= 30 and _match_field(node_text):
            break  # 下一个分区标题
        body.append(node)
        node = node.find_next_sibling()
    return body


def _iter_sections(soup):
    """按标题把页面切成段落：yield (标题文字, 后续元素列表)。

    标题不限于 h1-h6：任何短文本（≤30 字）命中近义关键词的标签都视为分区标题；
    标题元素没有兄弟内容时，自动向上找最近带兄弟内容的父级（如海康页面
    “标题 div 内嵌在 header div，正文在 header 的兄弟 div”）。
    """
    for marker, heading in _find_markers(soup):
        body = _collect_body(marker.find_next_sibling())
        if not body:
            parent = marker.parent
            climbed = 0
            while (parent is not None and parent.name and parent.name not in ("html", "body")
                   and climbed < 3):
                nxt = parent.find_next_sibling()
                if nxt is not None:
                    body = _collect_body(nxt)
                    break
                parent = parent.parent
                climbed += 1
        if body:
            yield heading, body


def collect_image_urls(soup, base_url):
    """收集页面中**所有**图片的 URL（去重、转绝对地址、排除 base64 内嵌图）。"""
    seen, urls = set(), []

    def add(src):
        if not src:
            return
        src = src.strip()
        if src.lower().startswith("data:"):
            return
        abs_url = _resolve(src, base_url)
        if abs_url and abs_url not in seen:
            seen.add(abs_url)
            urls.append(abs_url)

    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-original", "data-lazy-src", "data-url"):
            add(img.get(attr))
        for srcset_attr in ("srcset", "data-srcset"):
            srcset = img.get(srcset_attr)
            if srcset:
                for part in srcset.split(","):
                    cand = part.strip().split(" ")[0]
                    add(cand)
    return urls


def _collect_url_from_style(el):
    """收集 CSS 背景图 / data-background 中的图片 URL。"""
    cands = []
    for attr in ("data-background", "style", "data-bg", "data-url"):
        val = el.get(attr) or ""
        if not val:
            continue
        if attr == "style" and "background" in val.lower():
            m = re.search(r"url\s*\(\s*['\"]?(.*?)['\"]?\s*\)", val, re.I)
            if m:
                cands.append(m.group(1))
        elif attr == "data-background":
            cands.append(val)
    return cands


def collect_product_images(soup, base_url):
    """提取本产品相关图片 URL（最多 3 个，换行分隔）。

    因各网站图片标签不一，采用多策略依次尝试，直到收集到足够的产品图：
    1. 产品图集/轮播容器（swiper / gallery / product-img / carousel）
    2. og:image / twitter:image / og:image:url
    3. 规格参数区域附近的图片（部分网站把产品图放在参数旁）
    4. 页面中较大的 <img>（宽度>=200 或 data-* 属性），过滤小图标
    5. 最后兜底：前 3 张图片（含 srcset）
    """
    seen, urls = set(), []

    def add(src):
        if not src:
            return
        src = str(src).strip()
        if src.lower().startswith("data:"):
            return
        # 去掉 OSS 图片处理参数（x-oss-process），保留原图地址；再过滤非目标格式
        src = re.sub(r"\?x-oss-process=.*$", "", src)
        _path = src.split("?", 1)[0].split("#", 1)[0].lower()
        if re.search(r"\.(svg|ico|gif|bmp|tif|tiff|avif)$", _path):
            return
        abs_url = _resolve(src, base_url)
        if abs_url and abs_url not in seen:
            seen.add(abs_url)
            urls.append(abs_url)

    def enough():
        return len(urls) >= 3

    def collect_from(el):
        """从一个元素里收集图片 url（img / style / data-* / srcset）。"""
        if el is None:
            return
        for img in (el.find_all("img") if hasattr(el, "find_all") else []):
            for attr in ("src", "data-src", "data-original", "data-lazy-src", "data-url", "data-img", "data-image"):
                add(img.get(attr))
            for srcset_attr in ("srcset", "data-srcset"):
                srcset = img.get(srcset_attr)
                if srcset:
                    for part in srcset.split(","):
                        cand = part.strip().split(" ")[0]
                        add(cand)
        for attr in ("data-background", "data-bg", "data-image", "data-img", "data-src", "data-original", "data-url"):
            add(el.get(attr))
        for u in _collect_url_from_style(el):
            add(u)

    # ---- 策略 1：产品图集 / 轮播容器 ----
    carousel_sel = (
        ".swiper-container, .swiper-wrapper, .product-carousel, .product-carousel-list, "
        ".product-gallery, .product-image-gallery, .gallery, "
        "[class*='product-gallery'], [class*='product-img'], [class*='product-image'], "
        "[class*='product-pic'], [class*='picture-list'], [class*='photo-list'], "
        "[class*='gallery-list'], [class*='carousel'], [class*='swiper-slide'], "
        "[id*='gallery'], [id*='swiper']"
    )
    for c in soup.select(carousel_sel):
        # 深入容器内的每一张幻灯片/条目收集图片
        for slide in c.select("[class*='slide'], [class*='carousel-item'], [class*='item'], li, div, a"):
            collect_from(slide)
        collect_from(c)
        if enough():
            return list(dict.fromkeys(urls))[:3]

    # ---- 策略 2：og:image 等社交分享图 ----
    for attrs in ({"property": "og:image"}, {"property": "og:image:url"},
                  {"name": "twitter:image"}, {"name": "twitter:image:src"}):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            add(meta["content"])
    if enough():
        return list(dict.fromkeys(urls))[:3]

    # ---- 策略 3：规格参数区域附近的图片 ----
    for kw in ("规格", "spec", "parameter", "参数"):
        for el in soup.find_all(attrs={"class": True}):
            cls = " ".join(el.get("class") or [])
            if kw.lower() in cls.lower():
                collect_from(el)
                if enough():
                    return list(dict.fromkeys(urls))[:3]

    # ---- 策略 4：页面中较大的 <img> 标签 ----
    for img in soup.find_all("img"):
        w = img.get("width")
        try:
            if w and int(w) < 200:
                continue
        except Exception:
            pass
        src = img.get("src") or img.get("data-src") or ""
        if re.search(r"\.(svg|ico)$", src, re.I):
            continue
        collect_from(img)
        if enough():
            return list(dict.fromkeys(urls))[:3]

    # ---- 策略 5：兜底取前 3 张图片 ----
    if not urls:
        for img in soup.find_all("img"):
            for attr in ("src", "data-src", "data-original", "data-lazy-src", "data-url"):
                add(img.get(attr))
            if enough():
                break
    return list(dict.fromkeys(urls))[:3]


def coerce_image_urls(urls, max_images=3):
    """清洗参考图片 URL：只保留 png/jpg/jpeg/webp，去重并限制最多 max_images 个。"""
    seen, out = [], []
    for u in urls or []:
        u = str(u or "").strip()
        if not u or u.lower().startswith("data:"):
            continue
        # 去掉 OSS / CDN 图片处理参数，保留原图地址
        u = re.sub(r"\?x-oss-process=.*$", "", u)
        # 京东缩略图升级为清晰大图：s48x48/s228x228 → s800x800（小图不清晰）
        _sz = re.search(r"/s(\d+)x(\d+)_jfs/", u)
        if _sz and int(_sz.group(1)) < 800:
            u = re.sub(r"/s\d+x\d+_jfs/", "/s800x800_jfs/", u)
        # .avif 只是外包装：xxx.jpg.avif → xxx.jpg（还原成浏览器可直接展示的格式）
        if u.lower().endswith(".avif"):
            u = u[:-5]
        path = u.split("?", 1)[0].split("#", 1)[0].lower()
        if re.search(r"\.(svg|ico|gif|bmp|tif|tiff|avif)$", path):
            continue
        if u not in seen:
            seen.append(u)
            out.append(u)
        if len(out) >= max_images:
            break
    return out


def format_reference_images(urls_or_text, max_images=3):
    """把 URL 文本/列表整理为一个换行分隔的参考图片字段（最多 3 个）。"""
    if isinstance(urls_or_text, (list, tuple)):
        lines = [str(x).strip() for x in urls_or_text if str(x or "").strip()]
    else:
        lines = [ln.strip() for ln in str(urls_or_text or "").splitlines() if ln.strip()]
    return "\n".join(coerce_image_urls(lines, max_images))


# 京东登录/欢迎/验证页标题特征
_JD_LOGIN_TITLE = re.compile(
    r"(京东-欢迎登录|京东登录注册|京东登录|登录注册|登录后查看|登录看更多|"
    r"请先登录|扫码登录|欢迎登录|验证码|滑块验证|人机验证|登录页面|账号登录|快捷登录)", re.I)
# 京东反爬/跳转占位页特征（首页壳、验证页、登录跳转、错误页）
_JD_STUB = re.compile(
    r"(正品低价|品质保障|配送及时|轻松购物|京东验证|passport\.jd\.com|risk_handler|login\.aspx|"
    r"您访问的页面不存在|页面不存在|error2?\.aspx|error\.html|访问出错|页面失效|"
    r"商品已下架|商品已失效|很抱歉.*找不到|404\s*Not\s*Found|page\s*not\s*found|"
    r"频控|pc-frequent-pro\.pf\.jd\.com|pf\.jd\.com|请求过于频繁|访问过于频繁|"
    r"正在检测您的访问|系统检测到您的访问异常)", re.I)

# 京东通用模板描述（“京东JD.COM是国内专业…”不是真正的商品介绍）
_JD_GENERIC_DESC = re.compile(
    r"(京东JD\.COM是国内|网上购物商城，为您提供|等相关信息|价格、图片、品牌、评论|"
    r"为您推荐|猜你喜欢)", re.I)

# 京东“商品详情/参数”区域的噪声：售后承诺、登录提示、公告、服务条款等都不是商品介绍/参数
_JD_DETAIL_NOISE = re.compile(
    r"(登录看更多商品信息|该商品超时未付款|京东国际公告|京东商城向您保证所售商品均为正品行货|"
    r"凭质保证书及京东商城发票|享受全国联保服务|奢侈品、钟表除外|运费政策|请您放心购买|"
    r"因厂家会在没有任何提前通知的情况下更改产品包装|本司不能确保客户收到的货物与商城图片|"
    r"商品提示|服务承诺|售后服务|购买咨询|温馨提示|发票说明|正品保障|品质保障|配送及时|"
    r"轻松购物|免责声明|以下仅供参考|7天无理由退货|7天价保|大件运费险|晚发赔|"
    r"可配送港澳台及海外|晒单|追评|大家说|宝贝评价|用户评价|累计评价|好评率|中评|差评|问大家|"
    r"免费上门退换|价保险|闪电退款|极速审核|晚到赔|破损包退换|买一试三|不满意包退换|"
    r"优鲜赔|上门取件|以旧换新|上门安装|原厂正货|厂家直供|保价|价保|运费险|"
    r"延保|以换代修|只换不修|先行赔付|假一赔十|货到付款|分期付款|开箱验货|七天无理由)", re.I)

# 京东“价格说明/划线价/折扣”模板：整段是平台价格规则说明，不是商品介绍
_JD_PRICE_TIP = re.compile(
    r"(京东价[：:]\s*京东价为商品的销售价|划线价[：:]\s*商品展示的划横线价格为参考价|"
    r"折扣[：:]\s*如无特殊说明|异常问题[：:]\s*商品促销信息以商品详|"
    r"京东价为商品的销售价|商品展示的划横线价格为参考价)", re.I)

# 评价/评论区起始标记：商品介绍正文在“评价/评论”标题之前，从该处截断
_JD_REVIEW_MARK = re.compile(
    r"^\s*(商品)?(评价|评论)|^\s*好评|^\s*好评率|^\s*中评|^\s*差评|"
    r"^\s*晒单|^\s*追评|^\s*大家说|^\s*宝贝评价|^\s*用户评价|^\s*累计评价|^\s*问大家", re.I | re.M)


def _filter_noise_lines(items):
    """过滤列表行里的导航 / 售后 / 服务保障类噪声（性能特点兜底用）。

    服务保障条目（7天无理由退货、免费上门退换、闪电退款…）是京东平台
    通用承诺，不是产品卖点，不能作为“性能特点”；页脚导航同理。
    """
    out = []
    for ln in items:
        nb = re.sub(r"^[^\u4e00-\u9fa5A-Za-z0-9]+", "", str(ln or ""))
        if _JD_NAV_NOISE.search(nb):
            continue
        if len(nb) <= 40 and _JD_DETAIL_NOISE.search(nb):
            continue
        if _JD_REVIEW_MARK.search(str(ln or "")):
            continue
        out.append(ln)
    return out


def _jd_strip_noise(text):
    """去掉商品详情/参数区域里的模板/售后噪声行，并截掉评价区内容。"""
    if not text:
        return ""
    # 整段是“京东价/划线价/折扣”平台价格规则说明时直接丢弃
    if _JD_PRICE_TIP.search(str(text)):
        return ""
    out = []
    for ln in str(text).splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if len(ln) <= 40 and _JD_DETAIL_NOISE.search(ln):
            continue
        out.append(ln)
    joined = "\n".join(out)
    cut = None
    for m in _JD_REVIEW_MARK.finditer(joined):
        cut = m.start()
        break
    if cut is not None:
        joined = joined[:cut].rstrip()
    return joined.strip()


def _jd_is_title_like(text, title):
    """判断某段文字是否只是产品名称/标题的重复（不是真正的介绍）。"""
    t = _clean(text)
    tt = _clean(title)
    if not t:
        return True
    if t == tt:
        return True
    if len(t) <= 40:
        return True
    # 正文与标题高度重叠（标题前 20 字几乎就是整段正文）
    if tt and len(tt) >= 12 and t.startswith(tt[:20]):
        return True
    return False


def _jd_param_section_quality(text):
    """统计“xx：xx”键值行数量，用于判断参数区域是否可用（要求至少 2 行）。"""
    n = 0
    for ln in str(text or "").splitlines():
        ln = ln.strip()
        if re.match(r"^[^：:]{1,30}[：:]\S", ln):
            n += 1
    return n

def _jd_stub_detect(soup, final_url=""):
    """判断是否为京东占位页/登录页/验证页（未拿到真实商品数据）。"""
    if _JD_STUB.search(final_url or "") or _JD_LOGIN_TITLE.search(final_url or ""):
        return True
    # PC 频控 / 限流页：pf.jd.com 或标题含“频控/访问异常”
    if re.search(r"pf\.jd\.com|frequent", final_url or "", re.I):
        return True
    _tt = _clean(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if _tt and re.search(r"频控|访问异常|访问过于频繁|请求过于频繁", _tt, re.I):
        return True
    # 真实商品页：商品名节点存在且有内容（桌面/移动版都含 #itemName）
    item = soup.select_one("#itemName")
    if item and len(_clean(item.get_text(" ", strip=True))) >= 6:
        return False
    if soup.select_one("#itemName, h1.sku-name, .sku-name, [class*='sku-name']"):
        return False
    title = _clean(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title and (_JD_STUB.search(title) or _JD_LOGIN_TITLE.search(title)):
        return True
    return False


def _jd_title(soup):
    """京东产品名称：优先 #itemName / h1，其次页面 JS 数据（pageConfig / skuName）。"""
    for sel in (".sku-title-name", "#itemName", "#name h1", "h1.sku-name", ".sku-name", "h1"):
        el = soup.select_one(sel)
        if el:
            t = _clean(el.get_text(" ", strip=True))
            if t and not _JD_STUB.search(t):
                return _clean_title(t)
    if soup.title:
        t = _clean(soup.title.get_text(" ", strip=True))
        if t and not _JD_STUB.search(t):
            return _clean_title(t)
    raw = str(soup)
    for pat in (r'"skuName"\s*:\s*"([^"]+)"', r'"pName"\s*:\s*"([^"]+)"'):
        m = re.search(pat, raw)
        if m:
            t = _clean(m.group(1))
            if t:
                return _clean_title(t)
    for m in re.finditer(r"window\.pageConfig\s*=\s*(\{.*?\})\s*;", raw, re.S):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        prod = data.get("product") or {}
        for key in ("skuName", "pName", "name"):
            t = prod.get(key)
            if t:
                return _clean_title(str(t))
    if soup.title:
        t = _clean(soup.title.get_text(" ", strip=True))
        if t and not _JD_STUB.search(t):
            return _clean_title(t)
    return ""


def _jd_img_src(img):
    """从京东 <img> 中取图片地址，兼容 data-lazy-img / data-url / data-src 等。"""
    for attr in ("data-lazy-img", "data-lazy-src", "data-url", "data-src",
                 "data-original", "data-img", "src"):
        v = img.get(attr)
        if v:
            return v
    return ""


_JD_BAD_IMG = re.compile(
    r"(unionfe|union/|imagetools|/rank/|/umm/|/wq/|/ling/|/jdphoto/|/attach/|/node/|"
    r"base/detail/images|transparent|joy\.png|favicon|/babel/|probe-web|m-risk|"
    r"webcontainer|jxfe|s(?:[0-9]{1,2}|1[0-5][0-9])x\d+_jfs|"
    r"/ppms/|draw-image|goods_promo)", re.I)


def _jd_bad_img(abs_url):
    """判断是否为京东促销/装饰/图标类非商品图。"""
    path = (abs_url or "").split("?", 1)[0]
    return bool(_JD_BAD_IMG.search(path))


def _jd_item_info(soup_or_html):
    """解析京东页面内嵌的 _itemInfo = ({...}) JS 对象，返回 dict；失败返回 {}。

    移动版 _itemInfo 是带尾逗号的 JS 字面量，不是严格 JSON：
    先尝试标准 json，再尝试去掉尾逗号后解析，最后用正则提取常用字段。
    """
    raw = str(soup_or_html) if not isinstance(soup_or_html, str) else soup_or_html
    m = re.search(r"_[a-zA-Z]*itemInfo\s*=\s*\(\s*\{", raw)
    if not m:
        return {}
    start = m.end() - 1  # '{' 下标
    depth = 0
    in_str = False
    esc = False
    i = start
    while i < len(raw):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
        i += 1
    seg = raw[start:i + 1]
    for fixed in (seg, re.sub(r",\s*([}\]])", r"\1", seg)):
        try:
            data = json.loads(fixed)
            if isinstance(data, dict):
                return data
        except Exception:
            continue

    # 正则兜底：_itemInfo 不是严格 JSON 时，只抽取常用字段（值均为字符串）
    def grab(key):
        mm = re.search(r'"%s"\s*:\s*"([^"]*)"' % re.escape(key), seg)
        return mm.group(1) if mm else ""

    data = {"product": {}}
    for k in ("skuName", "imageurl", "nameWithoutBrand", "color", "size", "spec"):
        v = grab(k)
        if v:
            data["product"][k] = v
    feats = {}
    nwb = grab("nameWithoutBrand")
    for k in ("shortTitle", "nameWithoutBrand"):
        v = grab(k)
        if v:
            feats[k] = v
    data["product"]["extend"] = {
        "features": feats,
        "productFeatures": {"nameWithoutBrand": nwb},
    }
    for k in ("brandName", "sellPoint", "cBrand", "model", "subtitle", "adWord"):
        v = grab(k)
        if v:
            data[k] = v
    mf = re.search(r'"priceFloor"\s*:\s*\{[^{}]*?"price"\s*:\s*"([^"]*)"', seg)
    if mf:
        data["priceFloor"] = {"price": mf.group(1)}
    return data


def _flatten_kv_lines(obj, out=None, depth=0):
    """把嵌套 dict/list 拍平成“键：值”行（用于京东业务接口的参数提取）。"""
    out = out if out is not None else []
    if depth > 5:
        return out
    _noise = ("id", "sku", "skuid", "wareid", "url", "image", "imageurl",
              "venderid", "erppid", "shopid", "cat1", "cat2", "cat3",
              "timestamp", "time", "date", "code", "msg", "retcode",
              "bizretcode", "expire", "uuid", "token")
    if isinstance(obj, dict):
        for k, v in obj.items():
            ks = str(k).strip()
            kl = ks.lower()
            if not ks or kl in _noise or re.search(r"(url|img|href|pic|logo)", kl):
                continue
            if isinstance(v, (str, int, float)):
                s = str(v).strip()
                if s and 1 <= len(s) <= 150 and not re.match(r"^\d{8,}$", s):
                    line = f"{ks}：{s}"
                    if line not in out:
                        out.append(line)
            else:
                _flatten_kv_lines(v, out, depth + 1)
    elif isinstance(obj, list):
        for it in obj:
            _flatten_kv_lines(it, out, depth + 1)
    return out


def _jd_price_from_biz(biz):
    """从京东业务接口数据提取商品价格（¥xx），兼容多种 price 结构。"""
    if not isinstance(biz, dict):
        return ""
    pv = biz.get("price")
    if isinstance(pv, dict):
        for k in ("finalPrice", "jdPrice", "price"):
            sub = pv.get(k)
            if isinstance(sub, dict):
                s = str(sub.get("price") or "").strip()
                if re.search(r"\d", s) and "?" not in s:
                    return "¥" + re.sub(r"\.?0+$", "", s)
            elif isinstance(sub, (str, int, float)):
                s = str(sub).strip()
                if re.search(r"\d", s) and "?" not in s:
                    return "¥" + re.sub(r"\.?0+$", "", s)
        for k in ("p", "m", "op"):
            s = str(pv.get(k) or "").strip()
            if re.search(r"\d", s) and "?" not in s:
                return "¥" + re.sub(r"\.?0+$", "", s)
    pi = biz.get("priceInfo")
    if isinstance(pi, dict):
        for k in ("price", "jdPrice"):
            s = str(pi.get(k) or "").strip()
            if re.search(r"\d", s) and "?" not in s:
                return "¥" + re.sub(r"\.?0+$", "", s)
    return ""


def _jd_attr_lines(biz, limit=200):
    """从京东业务接口提取“商品详情”属性表（productAttributeVO.attributes，含品牌/编号/型号…）。

    京东“商品详情”页签展示的就是这张属性表，属于商品详情内容，
    与 paramInfo（真正的规格参数）分开；只取 labelName/labelValue 用户可读行。
    """
    rows, seen = [], set()
    if not isinstance(biz, dict):
        return rows

    def add(k, v):
        k = _clean(k)
        v = _clean(v)
        if not k or not v or len(k) > 40 or len(v) > 300:
            return
        line = "{}：{}".format(k, v)
        if line not in seen:
            seen.add(line)
            rows.append(line)

    def push_attr(it):
        if not isinstance(it, dict):
            return
        add(it.get("labelName") or it.get("attrName") or it.get("name") or it.get("label")
            or it.get("key") or it.get("title"),
            it.get("labelValue") or it.get("attrValue") or it.get("value") or it.get("val")
            or it.get("text") or it.get("desc"))

    try:
        pa = biz.get("productAttributeVO") or {}
        if isinstance(pa, dict):
            for it in (pa.get("attributes") or []) if isinstance(pa.get("attributes"), list) else []:
                push_attr(it)
        elif isinstance(pa, list):
            for it in pa:
                push_attr(it)
    except Exception:
        pass
    # 兼容旧接口 productAttributeList / saleAttribute 等字段名
    try:
        for subk in ("productAttributeList", "saleAttributeList", "paramAttributes"):
            v = biz.get(subk)
            if isinstance(v, list):
                for it in v:
                    push_attr(it)
    except Exception:
        pass
    return rows[:limit]



def _jd_biz_param_lines(biz, limit=200):
    """从 getWareBusiness 响应中提取商品参数“键：值”行。

    优先解析 paramInfo.groups[].items[]（name/value 成对），
    其次 basicInfo 常用字段与 wareInfoReadMap 可读键值。
    注意：productAttributeVO / skuHeadVO 分别属于“商品详情 / 商品名称”，
    不再混进“商品参数”（用户反馈参数被抓成了属性表/JSON 结构）。
    """
    lines, seen = [], set()
    def add(k, v):
        k = _clean(k)
        v = _clean(v)
        if not k or not v or len(k) > 40 or len(v) > 200:
            return
        # 参数值是图片链接时：不作为“键：值”文本行（交给参数长图 OCR）
        if re.search(r"\.(png|jpe?g|webp|avif)(\?|$)", v.split("?", 1)[0].lower()):
            return
        line = "{}：{}".format(k, v)
        if line not in seen:
            seen.add(line)
            lines.append(line)
    if isinstance(biz, dict):
        try:
            for g in (biz.get("paramInfo") or {}).get("groups", []) or []:
                for it in (g.get("items") if isinstance(g, dict) else []) or []:
                    if isinstance(it, dict):
                        add(it.get("name"), it.get("value"))
        except Exception:
            pass
        try:
            for k, v in (biz.get("basicInfo") or {}).items():
                ks = str(k).strip()
                kl = ks.lower()
                if kl in ("skuname", "name", "wareid", "skuid", "productname",
                          "venderid", "shopid"):
                    continue
                # 英文内部字段名（如 brandName/saleDate）不是用户可读参数标签，跳过
                if re.search(r"[A-Za-z]", ks) and not re.search(r"[\u4e00-\u9fa5]", ks):
                    continue
                if isinstance(v, (str, int, float)):
                    add(ks, v)
        except Exception:
            pass
        # 新接口 pc_detailpage_wareBusiness：真正的“规格参数”只来自 paramInfo /
        # basicInfo / wareInfoReadMap 的可读键值；productAttributeVO（品牌/编号/
        # 型号…）属于“商品详情”属性表，skuHeadVO（标题/品牌/分类）属于商品名称，
        # 两者都不应混进商品参数（否则会把属性表/JSON 结构当参数输出）。
        try:
            wim = biz.get("wareInfoReadMap") or {}
            if isinstance(wim, dict):
                for k, v in wim.items():
                    if k in ("cn_brand", "brand", "brandName", "fare", "thwa",
                             "timeliness_id", "isPrescriptCat", "unLimit_cid",
                             "shop_id", "shop_name", "vender_name", "buyer_post",
                             "size", "img_dfs_url", "category_id", "template_ids"):
                        continue
                    if isinstance(v, (str, int, float)):
                        ks = str(k).strip()
                        if re.search(r"[A-Za-z]", ks) and not re.search(r"[\u4e00-\u9fa5]", ks):
                            continue
                        add(ks, v)
                    elif isinstance(v, dict):
                        for kk, vv in v.items():
                            if isinstance(vv, (str, int, float)):
                                ks = str(kk).strip()
                                if re.search(r"[A-Za-z]", ks) and not re.search(r"[\u4e00-\u9fa5]", ks):
                                    continue
                                add(ks, vv)
        except Exception:
            pass
        # 明确不再整树拍平（会把整个接口 JSON 结构当参数行输出，
        # 用户反馈“参数抓取的都是 json 数据结构”。只保留结构化解析结果。）
    return lines[:limit]


def _jd_biz_param_images(biz, limit=20):
    """从 wareBusiness 参数数据中收集“参数长图”URL（部分商品参数以图片展示）。

    参数图片通常在 paramInfo 各 group 的 item.value / pic / image 字段里，
    或在 paramInfo.images / productAttributeVO 的图片字段中。
    """
    urls, seen = [], set()

    def add(u):
        if not u:
            return
        u = str(u).strip()
        p = u.split("?", 1)[0].lower()
        if not re.search(r"\.(png|jpe?g|webp|avif)$", p):
            return
        if u not in seen:
            seen.add(u)
            urls.append(u)

    if not isinstance(biz, dict):
        return urls[:limit]
    try:
        pi = biz.get("paramInfo") or {}
        if isinstance(pi, dict):
            for g in pi.get("groups") or []:
                if not isinstance(g, dict):
                    continue
                for it in g.get("items") or []:
                    if not isinstance(it, dict):
                        continue
                    for k in ("value", "pic", "image", "imageUrl", "img", "url"):
                        add(it.get(k))
            for k in ("images", "imageList", "pics"):
                v = pi.get(k)
                if isinstance(v, list):
                    for it in v:
                        if isinstance(it, dict):
                            for kk in ("url", "imageUrl", "img", "src", "value"):
                                add(it.get(kk))
                        else:
                            add(it)
    except Exception:
        pass
    try:
        pa = biz.get("productAttributeVO")
        if isinstance(pa, list):
            for it in pa:
                if isinstance(it, dict):
                    for k in ("value", "pic", "image", "imageUrl", "url"):
                        add(it.get(k))
        elif isinstance(pa, dict):
            for v in pa.values():
                if isinstance(v, (str, int, float)):
                    add(v)
                elif isinstance(v, dict):
                    for vv in v.values():
                        add(vv)
    except Exception:
        pass
    return urls[:limit]




def _jd_normalize_ware_biz(data):
    """从不同京东接口响应中提取真正的 wareBusiness 字典。

    兼容两种常见结构：
    - 老接口 item-soa: {"wareBusiness": {...}}
    - 新接口 pc_detailpage_wareBusiness: {"code":0, "data": {"wareBusiness": {...}}}
    或 data 本身就是 wareBusiness。
    """
    if not isinstance(data, dict):
        return None
    for keys in (("wareBusiness",), ("data", "wareBusiness")):
        cur = data
        ok = True
        for k in keys:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                ok = False
                break
        if ok and isinstance(cur, dict):
            return cur
    _BIZ_KEYS = ("skuId", "basicInfo", "paramInfo", "priceInfo",
                 "skuHeadVO", "productAttributeVO", "colorSizeVO", "wareInfoReadMap")

    def _is_biz(d):
        return isinstance(d, dict) and any(d.get(k) for k in _BIZ_KEYS)

    d = data.get("data") if isinstance(data, dict) else None
    if _is_biz(d):
        return d
    if _is_biz(data):
        return data
    return None

def _jd_desc_from_biz(biz):
    """从 wareBusiness 中提取商品详情文字与详情图片（新接口 descriptionInfo 等字段）。

    返回 (text, imgs)：text 为介绍文字（可能为空），imgs 为详情图片 URL 列表。
    """
    text_parts, imgs, seen = [], [], set()
    if not isinstance(biz, dict):
        return "", []

    candidates = []

    def push(v):
        if isinstance(v, str) and v.strip():
            candidates.append(v.strip())

    def add_item(it):
        if isinstance(it, str):
            push(it)
        elif isinstance(it, dict):
            # 新接口描述块：{"type":1图片 / 2文字, "content":"..."}
            for k in ("content", "value", "text", "imageUrl", "img"):
                v = it.get(k)
                if v:
                    push(v)
                    break

    # descriptionInfo / descInfo 常见几种结构
    for dk in ("descriptionInfo", "descInfo", "desc"):
        di = biz.get(dk)
        if isinstance(di, dict):
            for k in ("descContent", "content", "desc", "introduction"):
                v = di.get(k)
                if isinstance(v, str) and v:
                    push(v)
            for k in ("contents", "contentList", "images", "imageList"):
                v = di.get(k)
                if isinstance(v, list):
                    for it in v:
                        add_item(it)
        elif isinstance(di, str) and di.strip():
            push(di)

    for k in ("description", "introduction", "detailContent", "detail"):
        v = biz.get(k)
        if isinstance(v, str) and v.strip():
            push(v)
        elif isinstance(v, dict):
            for sub in ("content", "descContent", "desc"):
                sv = v.get(sub)
                if isinstance(sv, str) and sv.strip():
                    push(sv)

    for t in candidates:
        t = str(t).strip()
        if not t:
            continue
        clear_path = t.split("?", 1)[0].lower()
        if clear_path.endswith((".png", ".jpg", ".jpeg", ".webp")) \
                or re.search(r"\.(png|jpe?g|webp)(\?|$)", clear_path):
            if t not in seen:
                seen.add(t)
                imgs.append(t)
            continue
        if len(t) >= 2 and not _JD_GENERIC_DESC.search(t) and not _JD_STUB.search(t):
            text_parts.append(_clean(t))
    return "\n".join(text_parts)[:4000], imgs[:10]




def _jd_mine_captured(data):
    """在任意京东接口返回里递归挖掘 详情/参数/价格/名称。

    京东不同端（桌面 getWareBusiness / 移动聚合 api.m.jd.com）返回结构不同，
    这里只按“含义字段名”抽取，避免把整个 JSON 当详情/参数输出。
    返回 dict：商品详情/商品参数/商品价格/商品名称 + _desc_imgs/_param_imgs。
    """
    out = {}
    _SEEN = set()

    def _norm_ware(b):
        # 兼容 paramInfo/basicInfo/priceInfo 这类 wareBusiness 子结构
        if not isinstance(b, dict):
            return None
        for k in ("paramInfo", "basicInfo", "priceInfo", "skuHeadVO",
                  "productAttributeVO", "colorSizeVO", "wareInfoReadMap"):
            if b.get(k):
                return b
        return None

    def _add_params(b):
        if not isinstance(b, dict):
            return
        if not out.get("商品参数"):
            lines = _jd_biz_param_lines(b)
            uniq = []
            for ln in lines:
                if ln not in uniq:
                    uniq.append(ln)
            if uniq:
                out["商品参数"] = "\n".join(uniq[:200])[:8000]
        if not out.get("_param_imgs"):
            pimgs = _jd_biz_param_images(b, limit=12)
            if pimgs:
                out["_param_imgs"] = pimgs

    def _add_desc(b):
        if not isinstance(b, dict):
            return
        if not out.get("商品详情"):
            dtext, dimgs = _jd_desc_from_biz(b)
            if dtext:
                out["商品详情"] = dtext
            else:
                # 京东“商品详情”页签展示的就是属性表（品牌/编号/型号…），
                # 接口无介绍文字时把它作为真实商品详情
                _alines = _jd_attr_lines(b)
                if _alines:
                    out["商品详情"] = "\n".join(_alines)[:4000]
            if dimgs:
                cur = out.setdefault("_desc_imgs", [])
                for u in dimgs:
                    if u not in cur:
                        cur.append(u)
                out["_desc_imgs"] = cur[:12]

    def _add_price(b):
        if not isinstance(b, dict) or out.get("商品价格"):
            return
        pv = _jd_price_from_biz(b)
        if pv:
            out["商品价格"] = pv

    def _add_name(b):
        if not isinstance(b, dict) or out.get("商品名称"):
            return
        sh = b.get("skuHeadVO") or {}
        if isinstance(sh, dict):
            n = sh.get("skuName") or sh.get("name") or sh.get("title")
            if isinstance(n, str) and len(n.strip()) >= 4:
                out["商品名称"] = n.strip()[:300]
                return
        for k in ("skuName", "wareTitle", "name"):
            if b.get(k) and isinstance(b[k], str) and len(b[k].strip()) >= 4:
                out["商品名称"] = b[k].strip()[:300]
                return

    def _walk(o):
        if isinstance(o, dict):
            _id = id(o)
            if _id in _SEEN:
                return
            _SEEN.add(_id)
            # 能整体作为 wareBusiness 子结构解析的节点
            if _norm_ware(o):
                _add_params(o)
                _add_desc(o)
                _add_price(o)
                _add_name(o)
            # 描述接口直接返回 {"content": "<html>..."}
            if isinstance(o.get("content"), str) and o["content"].strip() and not out.get("商品详情"):
                t = o["content"]
                tb = BeautifulSoup(t, "html.parser")
                text = _clean(tb.get_text(" ", strip=True))
                if len(text) >= 4 and not _JD_STUB.search(text) \
                        and not _JD_GENERIC_DESC.search(text) and not _JD_PRICE_TIP.search(text):
                    out["商品详情"] = text[:4000]
                imgs = []
                for u in collect_image_urls(tb, "https://item.jd.com/%s.html" % (o.get("skuId") or "")):
                    if re.search(r"\.(png|jpe?g|webp)(\?|$)", u.split("?", 1)[0], re.I):
                        imgs.append(u)
                if imgs:
                    cur = out.setdefault("_desc_imgs", [])
                    for u in imgs:
                        if u not in cur:
                            cur.append(u)
                    out["_desc_imgs"] = cur[:12]
            # skuHeadVO / paramInfo 等单独出现的键
            if "skuHeadVO" in o:
                _add_name(o)
            if "paramInfo" in o:
                _add_params(o)
            if "priceInfo" in o:
                _add_price(o)
            for v in o.values():
                _walk(v)
        elif isinstance(o, list):
            for it in o:
                _walk(it)

    _walk(data)
    return out


def _jd_parse_captured(captured, sku, on_log=None):
    """把浏览器里拦截到的京东接口响应（pc_detailpage_wareBusiness / getWareBusiness / 描述接口）
    解析成补充数据字典：商品参数 / 商品价格 / 商品详情。
    """
    out = {}
    if not captured:
        return out
    for item in captured:
        data = item.get("json") if isinstance(item, dict) else item
        if not isinstance(data, dict):
            continue

        # 0) 通用挖掘：移动聚合接口/任意结构里按含义字段抽详情、参数、价格
        mined = _jd_mine_captured(data)
        for k, v in mined.items():
            if v and not out.get(k):
                out[k] = v

        # 1) 描述接口：content 为介绍区 HTML/长图
        if isinstance(data.get("content"), str) and not out.get("商品详情"):
            t = data["content"]
            if t and not _JD_STUB.search(t) and not _JD_GENERIC_DESC.search(t) \
                    and not _JD_PRICE_TIP.search(t):
                tb = BeautifulSoup(t, "html.parser")
                text = _clean(tb.get_text(" ", strip=True))
                if len(text) >= 2:
                    out["商品详情"] = text[:4000]
                imgs = []
                for u in collect_image_urls(tb, "https://item.jd.com/%s.html" % sku):
                    if re.search(r"\.(png|jpe?g|webp)(\?|$)", u.split("?", 1)[0], re.I):
                        imgs.append(u)
                if imgs:
                    out["_desc_imgs"] = imgs[:10]

        # 2) 商品参数 / 价格：wareBusiness
        biz = _jd_normalize_ware_biz(data)
        if not biz:
            continue
        if not out.get("商品参数"):
            lines = _jd_biz_param_lines(biz)
            seen, uniq = set(), []
            for ln in lines:
                if ln and ln not in seen:
                    seen.add(ln)
                    uniq.append(ln)
            if uniq:
                out["商品参数"] = "\n".join(uniq[:200])[:8000]
        if not out.get("_param_imgs"):
            pimgs = _jd_biz_param_images(biz, limit=12)
            if pimgs:
                out["_param_imgs"] = pimgs
        if not out.get("商品价格"):
            _pv = _jd_price_from_biz(biz)
            if _pv:
                out["商品价格"] = _pv
            else:
                try:
                    pi = biz.get("priceInfo") if isinstance(biz, dict) else {}
                    if isinstance(pi, dict):
                        pv = str(pi.get("price") or pi.get("jdPrice") or "").strip()
                        if pv and "?" not in pv and re.search(r"\d", pv):
                            out["商品价格"] = "\u00a5" + re.sub(r"\.?0+$", "", pv)
                except Exception:
                    pass

        # 3) 商品详情：新接口 wareBusiness 内部可能自带介绍字段（文字+图片）
        if not out.get("商品详情"):
            dtext, dimgs = _jd_desc_from_biz(biz)
            if dtext:
                out["商品详情"] = dtext
            else:
                _alines = _jd_attr_lines(biz)
                if _alines:
                    out["商品详情"] = "\n".join(_alines)[:4000]
            if dimgs:
                cur = out.setdefault("_desc_imgs", [])
                for u in dimgs:
                    if u not in cur:
                        cur.append(u)
                out["_desc_imgs"] = cur[:10]

        # 4) 商品名称：新接口 skuHeadVO（标题域）
        if not out.get("商品名称") and isinstance(biz, dict):
            sh = biz.get("skuHeadVO") or {}
            if isinstance(sh, dict):
                _n = sh.get("skuName") or sh.get("name") or sh.get("title")
                if isinstance(_n, str) and len(_n.strip()) >= 4:
                    out["商品名称"] = _n.strip()[:300]

    if out and on_log:
        on_log("已从页面真实接口捕获%s" % ("、".join(k for k in ("商品详情", "商品参数", "商品价格") if out.get(k))))
    return out


def _jd_embed_api(html, api):
    """把浏览器上下文抓到的京东接口数据（JSON）内嵌回页面 HTML。"""
    if not api or not isinstance(html, str):
        return html
    try:
        blob = json.dumps(api, ensure_ascii=False)
    except Exception:
        return html
    tag = '<script id="jd_api_data" type="application/json">%s</script>' % blob
    if "</body>" in html:
        return html.replace("</body>", tag + "</body>", 1)
    return html + tag


def _jd_read_api_data(soup_or_html):
    """从页面 HTML/BeautifulSoup 中读取内嵌的 jd_api_data JSON。"""
    raw = str(soup_or_html) if not isinstance(soup_or_html, str) else soup_or_html
    m = (re.search(r'<script[^>]+id="jd_api_data"[^>]*type="application/json"[^>]*>(.*?)</script>',
                   raw, re.S)
         or re.search(r'<script[^>]+type="application/json"[^>]*id="jd_api_data"[^>]*>(.*?)</script>',
                      raw, re.S))
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _jd_fill_from_api(row, api):
    """把京东接口数据填入行字典（兼容 京东字段/通用字段 两套表头）。

    商品详情/设备介绍：接口描述优先；若当前值是“产品名称/品牌”拼凑的兜底文字
    （短、或含品牌行、或整体像名称串），也替换为接口返回的真实介绍。
    商品参数/设备参数：接口参数更完整时替换。
    """
    if not api:
        return
    for akey, keys in (("商品详情", ("商品详情", "设备介绍")),
                       ("商品参数", ("商品参数", "设备参数"))):
        v = (api.get(akey) or "").strip()
        if not v:
            continue
        if akey == "商品详情" and _JD_PRICE_TIP.search(v):
            # 京东“价格说明/划线价/折扣”模板是平台规则，不是商品介绍
            continue
        if akey == "商品详情" and len(v.splitlines()) <= 1 and len(v) < 30:
            # 单行短文本（如“品牌：Anciently”）不是真实商品介绍
            continue
        for k in keys:
            if k not in row:
                continue
            oldv = (row.get(k) or "").strip()
            if not oldv:
                row[k] = v[:8000]
                continue
            if akey == "商品详情":
                # 名称拼凑兜底：短、含品牌行、或正文与产品名称高度重叠
                weak = (len(oldv) < 60
                        or ("品牌：" in oldv and len(oldv) <= 160)
                        or (len(v) > len(oldv) and len(oldv) < 120))
                if weak:
                    row[k] = v[:8000]
            else:
                if len(v) > len(oldv):
                    row[k] = v[:8000]
    pv = (api.get("商品价格") or "").strip()
    if pv and "商品价格" in row and not (row.get("商品价格") or "").strip():
        row["商品价格"] = pv
    nv = (api.get("商品名称") or "").strip()
    if nv and len(nv) >= 4:
        for _k in ("商品名称", "产品名称"):
            if _k in row and not (row.get(_k) or "").strip():
                row[_k] = nv[:300]
    imgs = api.get("_desc_imgs") or []
    if imgs:
        more = format_reference_images(imgs)
        if more:
            for k in ("参考图", "参考图片"):
                if k in row and not (row.get(k) or "").strip():
                    row[k] = more


def _jd_fetch_api_in_page(page, sku, cfg, on_log=None):
    """在浏览器页面上下文内调用京东公开接口，返回补充数据字典；失败返回 {}。

    作用：京东商品详情/参数/价格是页面 JS 异步加载的（HTML 里没有），
    而且在浏览器上下文里请求可携带页面 Cookie、通过请求级反爬，
    比在 Python 侧用 requests 直接调接口成功率高得多。
    """
    out = {}
    if not re.match(r"^\d{6,}$", str(sku or "")):
        return out
    js = r"""
    async () => {
      const sku = "%s";
      const out = {};
      async function jget(url, isJson) {
        // 浏览器上下文内请求已携带 Cookie，成功率较高；每个接口只试 1 次，
        // 避免未登录/接口失效时多次重试白等（曾导致单次渲染阻塞 1 分钟+）
        for (let tryN = 0; tryN < 1; tryN++) {
          const ctl = new AbortController();
          const timer = setTimeout(function(){ ctl.abort(); }, 8000);
          try {
            const r = await fetch(url, {credentials: 'include', method: 'GET',
                signal: ctl.signal,
                headers: {'Accept': 'application/json, text/plain, */*',
                          'Referer': 'https://item.jd.com/'}});
            const t = await r.text();
            if (t && t.trim().length >= 2 && !/^\s*<html/i.test(t)) {
              if (isJson) { try { return JSON.parse(t); } catch (e) { return t; } }
              return t;
            }
          } catch (e) {}
          clearTimeout(timer);
          await new Promise(function(res){ setTimeout(res, 600 + tryN * 400); });
        }
        return null;
      }
      try {
        const p = await jget('https://p.3.cn/prices/mgets?skuIds=J_' + sku, true);
        if (Array.isArray(p) && p[0] && p[0].p) out.price = String(p[0].p);
      } catch (e) {}
      try {
        const wb = await jget('https://item-soa.jd.com/getWareBusiness?skuId=' + sku
            + '&mainSkuId=' + sku + '&charset=utf-8', true);
        if (wb) out.ware = wb;
      } catch (e) {}
      try {
        const d = await jget('https://cd.jd.com/description/channel?skuId=' + sku
            + '&mainSkuId=' + sku + '&charset=utf-8&callback=', false);
        if (d && typeof d === 'string') out.desc = d;
        else if (d) out.descObj = d;
      } catch (e) {}
      try {
        const body = JSON.stringify({skuId: sku, area: '1_2809_51226_0', num: '1',
            clientSource: 'PC', sfTime: '1,0,0'});
        const api = 'https://api.m.jd.com/?functionId=pc_detailpage_wareBusiness'
            + '&appid=pc-item-soa&client=pc&clientVersion=1.0.0&t=' + Date.now()
            + '&body=' + encodeURIComponent(body);
        const nw = await jget(api, true);
        if (nw && typeof nw === 'object') out.newWare = nw;
      } catch (e) {}
      return out;
    }
    """ % str(sku)
    try:
        data = page.evaluate(js)
    except Exception as exc:
        if on_log:
            on_log("浏览器内京东接口调用失败：%s" % exc)
        return {}
    if not isinstance(data, dict):
        return {}

    def log(msg):
        if on_log:
            on_log(msg)

    # 1) 商品价格：p.3.cn
    price_v = str(data.get("price") or "").strip()
    if price_v and "?" not in price_v and re.search(r"\d", price_v):
        out["商品价格"] = "¥" + re.sub(r"\.?0+$", "", price_v)

    # 2) 商品参数/价格：item-soa getWareBusiness
    wb = data.get("ware")
    biz = _jd_normalize_ware_biz(wb) or wb
    if isinstance(biz, dict):
        lines = _jd_biz_param_lines(biz)
        uniq = []
        for ln in lines:
            if ln not in uniq:
                uniq.append(ln)
        if uniq:
            out["商品参数"] = "\n".join(uniq[:200])[:8000]
        if not out.get("_param_imgs"):
            pimgs = _jd_biz_param_images(biz, limit=12)
            if pimgs:
                out["_param_imgs"] = pimgs
        try:
            _pv = _jd_price_from_biz(biz)
            if _pv and "商品价格" not in out:
                out["商品价格"] = _pv
        except Exception:
            pass

    # 2.5) 新接口 pc_detailpage_wareBusiness（页面 JS 同款，浏览器上下文携带 Cookie）
    nw = data.get("newWare")
    nbiz = _jd_normalize_ware_biz(nw)
    if isinstance(nbiz, dict) and nbiz is not biz:
        if not out.get("商品参数"):
            nlines = _jd_biz_param_lines(nbiz)
            nuniq = []
            for ln in nlines:
                if ln not in nuniq:
                    nuniq.append(ln)
            if nuniq:
                out["商品参数"] = "\n".join(nuniq[:200])[:8000]
        if not out.get("商品价格"):
            try:
                _npv = _jd_price_from_biz(nbiz)
                if _npv:
                    out["商品价格"] = _npv
            except Exception:
                pass
        if not out.get("商品详情"):
            dtext, dimgs = _jd_desc_from_biz(nbiz)
            if dtext:
                out["商品详情"] = dtext
            else:
                _alines = _jd_attr_lines(nbiz)
                if _alines:
                    out["商品详情"] = "\n".join(_alines)[:4000]
            if dimgs:
                cur = out.setdefault("_desc_imgs", [])
                for u in dimgs:
                    if u not in cur:
                        cur.append(u)
                out["_desc_imgs"] = cur[:10]
        if not out.get("_param_imgs"):
            pimgs = _jd_biz_param_images(nbiz, limit=12)
            if pimgs:
                out["_param_imgs"] = pimgs

    # 3) 商品详情：cd.jd.com 描述接口（可能返回 HTML / JSON / JSONP）
    t = data.get("desc") or ""
    if isinstance(data.get("descObj"), dict):
        t = str(data["descObj"].get("content") or "") or t
    # 页面 JS fetch 受 CORS 限制经常拿不到，改用 Playwright page.request
    # 在浏览器上下文内直连（带页面 Cookie、不走 CORS），成功率更高
    if not t and not out.get("商品详情"):
        try:
            _du = ("https://cd.jd.com/description/channel?skuId=%s&mainSkuId=%s"
                   "&charset=utf-8&callback=" % (sku, sku))
            _dr = page.request.get(_du, timeout=15000)
            if _dr.ok:
                _rt = (_dr.text() or "").strip()
                if _rt.startswith("{"):
                    try:
                        _j = json.loads(_rt)
                        _rt = str(_j.get("content") or _rt)
                    except Exception:
                        pass
                else:
                    _lo = _rt.find("(")
                    _hi = _rt.rfind(")")
                    if _lo >= 0 and _hi > _lo:
                        try:
                            _j = json.loads(_rt[_lo + 1:_hi])
                            _rt = str(_j.get("content") or _rt)
                        except Exception:
                            pass
                if _rt and not _JD_STUB.search(_rt):
                    t = _rt
        except Exception:
            pass
    if t and t.strip().startswith("{"):
        try:
            _jdq = json.loads(t)
            if isinstance(_jdq, dict) and _jdq.get("content"):
                t = str(_jdq["content"])
        except Exception:
            pass
    if t and not _JD_STUB.search(t):
        tb = BeautifulSoup(t, "html.parser")
        text = _clean(tb.get_text(" ", strip=True))
        if len(text) >= 4:
            out["商品详情"] = text[:4000]
        imgs = []
        for u in collect_image_urls(tb, "https://item.jd.com/%s.html" % sku):
            if re.search(r"\.(png|jpe?g|webp)(\?|$)", u.split("?", 1)[0], re.I):
                imgs.append(u)
        if imgs:
            out["_desc_imgs"] = imgs[:10]

    # 4) 商品详情兜底：从业务接口 wareBusiness 内部字段取介绍（描述接口返回纯图片时）
    if not out.get("商品详情"):
        dtext, dimgs = _jd_desc_from_biz(biz)
        if dtext:
            out["商品详情"] = dtext
        else:
            _alines = _jd_attr_lines(biz)
            if _alines:
                out["商品详情"] = "\n".join(_alines)[:4000]
        if dimgs:
            cur = out.setdefault("_desc_imgs", [])
            for u in dimgs:
                if u not in cur:
                    cur.append(u)
            out["_desc_imgs"] = cur[:10]

    # 5) 模拟点击画廊“规格参数”缩略图，收集弹层里的参数长图 URL（部分商品参数以图片展示）
    try:
        _js_pimg = r"""
        async () => {
          const before = new Set();
          Array.prototype.slice.call(document.images).forEach(function(i){
            before.add(i.currentSrc || i.src || '');
          });
          let target = null;
          const cands = Array.prototype.slice.call(
              document.querySelectorAll('[class*="parameter"], [id*="parameter"]'));
          for (const el of cands) {
            const t = (el.textContent || '').trim();
            if (t.indexOf('规格参数') >= 0 || t.indexOf('参数') >= 0) { target = el; break; }
          }
          if (!target) target = cands[0] || null;
          if (target) { try { target.click(); } catch (e) {} }
          await new Promise(function(res){ setTimeout(res, 2600); });
          const out = [];
          const add = function(u){
            u = (u || '').trim();
            if (!u || u.indexOf('data:') === 0) return;
            if (out.indexOf(u) < 0) out.push(u);
          };
          Array.prototype.slice.call(document.images).forEach(function(i){
            const u = i.currentSrc || i.src || i.getAttribute('data-src')
                || i.getAttribute('data-lazy-img') || '';
            if (u && !before.has(u)) add(u);
          });
          // 参数/规格/大图弹层容器里的图片（含 background-image）
          Array.prototype.slice.call(document.querySelectorAll(
              '[class*="parameter"],[id*="parameter"],[class*="lightbox"],[id*="lightbox"],' +
              '[class*="bigimg"],[class*="BigImg"],[class*="zoomImg"],' +
              '[class*="preview"],[id*="preview"],[class*="spec"]')).forEach(function(el){
            Array.prototype.slice.call(el.querySelectorAll('img')).forEach(function(i){
              add(i.currentSrc || i.src || i.getAttribute('data-src') || '');
            });
            const bg = getComputedStyle(el).backgroundImage || '';
            const m = bg.match(/url\(["']?([^"')]+)["']?\)/);
            if (m) add(m[1]);
          });
          return out;
        }
        """
        _pimg_urls = page.evaluate(_js_pimg)
        if isinstance(_pimg_urls, list):
            _cur_t = out.setdefault("_param_imgs", [])
            for _u in _pimg_urls:
                _u = str(_u or "").strip()
                if _u and _u not in _cur_t:
                    _cur_t.append(_u)
    except Exception:
        pass

    got = [k for k in ("商品价格", "商品参数", "商品详情") if out.get(k)]
    if got:
        log("浏览器上下文京东接口补充成功：%s" % "、".join(got))
    return out


def _jd_api_supplement(sku, cfg, on_log=None, browser_data=None):
    """京东公开接口补充抓取：价格(p.3.cn)、商品介绍(cd.jd.com)、商品参数(item-soa)。

    当京东商品页被反爬/需登录而拿不到 商品详情/商品参数/商品价格 时，
    依次尝试这些接口，能取到多少补多少；全部失败时静默返回空 dict。
    """
    out = {}

    def log(msg):
        if on_log:
            on_log(msg)

    # 已提供浏览器上下文抓到的接口数据时，直接使用，不再发请求
    if browser_data:
        _bd = _jd_read_api_data(browser_data) if isinstance(browser_data, str) else dict(browser_data or {})
        if _bd:
            return _bd

    if not re.match(r"^\d{6,}$", str(sku or "")):
        return out
    verify = bool(cfg["scraper"].get("verify_ssl", True))
    timeout = int(cfg["scraper"].get("timeout", 15))
    _cks = load_jd_cookies(cfg)
    cookies = None
    if _cks:
        cookies = {c.get("name"): c.get("value") for c in _cks
                   if c.get("name") and c.get("value")}

    def make_session(ua):
        s = requests.Session()
        s.headers.update({
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*;q=0.9",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        if cookies:
            s.cookies.update(cookies or {})
        _px = _proxy_dict(cfg)
        if _px:
            s.proxies.update(_px)
        return s

    def ok_json(r):
        """接口返回必须是 JSON，且未被重定向到京东错误页/登录页。"""
        if r is None:
            return False
        if _JD_STUB.search(r.url or ""):
            return False
        ct = (r.headers.get("content-type") or "").lower()
        if ct and "json" not in ct and "text/plain" not in ct:
            return False
        try:
            r.json()
            return True
        except Exception:
            return False

    def try_ua(uas):
        """用一组 UA 依次尝试三个接口；先拿到哪个字段就用哪个。"""
        for ua in uas:
            sess = make_session(ua)
            if len(sess.cookies) == 0:
                # 预热一次首页，拿到 __jdu 等基础 Cookie
                try:
                    sess.get("https://www.jd.com/", timeout=timeout, verify=verify,
                             allow_redirects=True)
                except Exception:
                    pass

            # 1) 商品价格：p.3.cn 老价格接口（未登录/网络抖动时易超时，用短超时避免拖慢整体）
            try:
                r = sess.get("https://p.3.cn/prices/mgets?skuIds=J_%s" % sku,
                             timeout=5, verify=verify,
                             headers={"Referer": "https://item.jd.com/%s.html" % sku})
                if ok_json(r):
                    data = r.json()
                    if isinstance(data, list) and data and isinstance(data[0], dict):
                        v = str(data[0].get("p") or "").strip()
                        if "?" not in v and re.search(r"\d", v):
                            fv = re.sub(r"\.?0+$", "", v)
                            if "商品价格" not in out:
                                out["商品价格"] = "¥" + fv
                                log("京东价格接口取到价格：%s" % out["商品价格"])
            except Exception as exc:
                log("京东价格接口失败：%s" % exc)

            # 2) 商品参数/价格：item-soa 业务数据接口
            try:
                r = sess.get("https://item-soa.jd.com/getWareBusiness?skuId=%s&mainSkuId=%s&charset=utf-8"
                             % (sku, sku), timeout=timeout, verify=verify,
                             headers={"Referer": "https://item.jd.com/%s.html" % sku})
                if ok_json(r):
                    data = r.json()
                    biz = _jd_normalize_ware_biz(data)
                    if isinstance(biz, dict) and biz.get("skuId") is None \
                            and not biz.get("paramInfo") and not biz.get("basicInfo"):
                        biz = None  # 空壳响应
                    lines, price_v = [], ""
                    if isinstance(biz, dict):
                        lines = _jd_biz_param_lines(biz)
                        pi = biz.get("priceInfo") if isinstance(biz, dict) else {}
                        if isinstance(pi, dict):
                            pv = pi.get("price") or pi.get("jdPrice") or ""
                            if isinstance(pv, (str, int, float)):
                                pvs = str(pv).strip()
                                if "?" not in pvs and re.search(r"\d", pvs):
                                    price_v = "¥" + re.sub(r"\.?0+$", "", pvs)
                    seen2, uniq = set(), []
                    for ln in lines:
                        if ln and ln not in seen2:
                            seen2.add(ln)
                            uniq.append(ln)
                    if uniq and "商品参数" not in out:
                        out["商品参数"] = "\n".join(uniq[:200])[:8000]
                        log("京东业务接口取到商品参数 %d 条" % len(uniq))
                    if price_v and "商品价格" not in out:
                        out["商品价格"] = price_v
                        log("京东业务接口取到价格：%s" % price_v)
            except Exception as exc:
                log("京东业务接口失败：%s" % exc)

            # 3) 商品介绍：cd.jd.com 描述接口（返回介绍区 HTML/长图）
            try:
                r = sess.get("https://cd.jd.com/description/channel?skuId=%s&mainSkuId=%s&charset=utf-8&callback="
                             % (sku, sku), timeout=timeout, verify=verify,
                             headers={"Referer": "https://item.jd.com/%s.html" % sku})
                t = ""
                if r is not None and not _JD_STUB.search(r.url or ""):
                    ct = r.text or ""
                    if ct.strip().startswith("{"):
                        try:
                            j = json.loads(ct)
                            t = str(j.get("content") or "")
                        except Exception:
                            t = ct
                    else:
                        t = ct
                if t and not _JD_STUB.search(t) and not _JD_GENERIC_DESC.search(t) \
                        and "商品详情" not in out:
                    tb = BeautifulSoup(t, "html.parser")
                    text = _clean(tb.get_text(" ", strip=True))
                    if len(text) >= 2:
                        out["商品详情"] = text[:4000]
                        log("京东描述接口取到商品介绍 %d 字" % len(text))
                    imgs = collect_image_urls(tb, "https://item.jd.com/%s.html" % sku)
                    if imgs:
                        out["_desc_imgs"] = imgs
            except Exception as exc:
                log("京东描述接口失败：%s" % exc)

            # 三个关键字段都齐了就提前结束
            if out.get("商品价格") and out.get("商品参数") and out.get("商品详情"):
                break

    try_ua([cfg["scraper"].get("user_agent") or _DEFAULT_UA, _DEFAULT_MOBILE_UA])
    return out

def collect_jd_product_images(soup, base_url):
    """京东商品图：优先主图轮播区，其次 og:image，然后明显带 jfs/360buyimg 的大图。
    按 jfs 图床路径去重（同一张图的 n4/n1/不同尺寸/不同格式只保留最高清的一个），
    最多 3 张；过滤促销/装饰/小图标。"""
    _best, _order = {}, []

    # 京东图片主机域名（含 jfs 图床）
    _JD_IMG_HOST = re.compile(r"(360buyimg|jd\.com|jfs)", re.I)
    # 商品大图特征：mobilecms 主图、s7xx 大图、n1x.360buyimg.com 高清图、
    # storage.jd.com/ware-man* 商品素材图、imgzone 参数/详情图
    _JD_GOOD_IMG = re.compile(
        r"(mobilecms|jfs/|ware-man|waremark|n1\d\.360buyimg\.com|"
        r"img\d+\.360buyimg\.com/(?:img|imgzone)/jfs|s\d{3,}x\d+|!q\d{1,2}\.dpg|"
        r"m\.360buyimg\.com/(?:mobilecms|n\d))", re.I)

    def add(src):
        if not src:
            return
        src = str(src).strip()
        if src.lower().startswith("data:"):
            return
        path = src.split("?", 1)[0].split("#", 1)[0].lower()
        if re.search(r"\.(svg|ico|gif|bmp|tif|tiff)$", path):
            return
        abs_url = _resolve(src, base_url)
        if re.search(r"jcm\.jd\.com\S*pre", abs_url, re.I):
            return  # 京东埋点占位图
        # 过滤明显非商品图：js、促销、装饰、小图标
        if _jd_bad_img(abs_url):
            return
        if not abs_url:
            return
        key = _jd_jfs_key(abs_url)
        if key not in _best or _jd_img_better(abs_url, _best[key]):
            if key not in _best:
                _order.append(key)
            _best[key] = abs_url

    def enough():
        return len(_best) >= 3

    def _img_in_ad(img):
        node = img
        while node is not None and getattr(node, "parent", None) is not None:
            node = node.parent
            name = getattr(node, "name", None)
            if name in (None, "html", "body"):
                return False
            _id = (node.get("id") or "") if hasattr(node, "get") else ""
            _cls = " ".join(node.get("class") or []) if hasattr(node, "get") else ""
            if re.search(r"(advert|promo|adPosition|report)", _id + " " + _cls, re.I):
                return True
        return False

    def collect_from(el):
        if el is None:
            return
        for img in (el.find_all("img") if hasattr(el, "find_all") else []):
            if _img_in_ad(img):
                continue
            add(_jd_img_src(img))
            for a in ("srcset", "data-srcset"):
                s = img.get(a)
                if s:
                    for part in s.split(","):
                        add(part.strip().split(" ")[0])
        # 移动版主图常用 background 样式引用真实图
        for a in ("data-url", "data-src", "data-original", "data-lazy-img",
                  "data-image", "data-img", "data-bg", "background-image"):
            add(el.get(a))
        for tag in ("style",):
            st = el.get(tag) or ""
            # 移动版主图常以 background: url(&quot;...&quot;) 引用真实图片
            for m in re.finditer(r'url\(&quot;([^&]+)&quot;\)', st):
                add(m.group(1).strip())

    # 桌面版主图轮播 + 移动版轮播/主图
    for sel in (".image-carousel-content", "#spec-list", "#spec-img", ".spec-items",
                ".swiper-wrapper", ".swiper-slide", "#J_galleryList", ".gallery",
                "#J-detail-content", "#detail", ".product-intro", "#J-product-detail",
                "#popupMain"):
        for c in soup.select(sel):
            collect_from(c)
        if enough():
            return [ _best[k] for k in _order ][:3]

    for attrs in ({"property": "og:image"}, {"property": "og:image:url"},
                  {"name": "twitter:image"}):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            add(meta["content"])
    if enough():
        return [ _best[k] for k in _order ][:3]

    # 移动版 _itemInfo.product.imageurl 主图（相对路径 jfs/... → img14.360buyimg.com/n1/...）
    info0 = _jd_item_info(str(soup))
    _p = ((info0.get("product") or {}).get("imageurl") or "").strip()
    if _p and re.search(r"\.(png|jpe?g|webp)$", _p.split("?", 1)[0], re.I):
        if re.match(r"^https?://", _p, re.I):
            add(_p)
        else:
            add("https://img14.360buyimg.com/n1/" + _p.lstrip("/"))
    if enough():
        return [ _best[k] for k in _order ][:3]

    # 宽泛扫描：跳过促销/装饰/店铺元素（按 class/id/alt），只收明显商品大图
    _JD_NOISE_CLS = re.compile(
        r"(rank|floor|icon|logo|shop|banner|arrow|star|brand|qr|code|head|foot|"
        r"bg|advert|promo|mark|badge|service|search|cart|nav|title|sprite)", re.I)
    _JD_PIC_CLS = re.compile(
        r"(spec|pic|gallery|carousel|sku|avt|main|detail|img|photo|intro|preview|thumb)", re.I)
    for img in soup.find_all("img"):
        src = _jd_img_src(img)
        if not src or not _JD_IMG_HOST.search(src):
            continue
        w = img.get("width") or img.get("data-w") or ""
        try:
            if w and int(w) < 100:
                continue
        except Exception:
            pass
        abs_url = _resolve(src, base_url)
        if not _JD_GOOD_IMG.search(abs_url):
            continue
        cls = " ".join(img.get("class") or []) + " " + (img.get("id") or "") + " " + (img.get("alt") or "")
        # 促销/装饰/店铺 logo 等噪声直接跳过
        if _JD_NOISE_CLS.search(cls):
            continue
        # 纯商品图特征（mobilecms 主图等）或带商品图样式 class 的才保留
        if re.search(r"(mobilecms|s7\d{2,}x|!q\d{1,2}\.dpg|ware-man)", abs_url) or _JD_PIC_CLS.search(cls):
            add(src)
        if enough():
            break

    if not _best:
        # 仍然没有：退回通用图片收集（也带基础过滤）
        return [u for u in collect_product_images(soup, base_url)
                if not _jd_bad_img(u)][:3]
    return [ _best[k] for k in _order ][:3]




def _jd_attrs_lines(soup, limit=300):
    """京东桌面版 .attrs 容器：把 label/value 两列输出为“键：值”行。

    例：<li class="item"><span class="label">品牌</span><span class="value">爱玛（AIMA）</span></li>
        输出：品牌：爱玛（AIMA）
    """
    out, seen = [], set()

    def add_line(k, v):
        k = _clean(k)
        v = _clean(v)
        if not k or not v or len(k) > 30:
            return
        line = "{}：{}".format(k, v)
        if line not in seen:
            seen.add(line)
            out.append(line)

    for item in soup.select(".attrs .item"):
        lab = item.select_one(".label")
        val = item.select_one(".value")
        if lab and val:
            add_line(lab.get_text(" ", strip=True), val.get_text(" ", strip=True))
            continue
        dt = item.find(["dt", "b", "strong"])
        dd = item.find(["dd", "em", "p"]) if dt else None
        if dt and dd:
            add_line(dt.get_text(" ", strip=True), dd.get_text(" ", strip=True))
            continue
        spans = [s for s in item.find_all("span", recursive=False)
                 if _clean(s.get_text(" ", strip=True))]
        if len(spans) >= 2:
            add_line(spans[0].get_text(" ", strip=True),
                     spans[1].get_text(" ", strip=True))
        if len(out) >= limit:
            break
    return out


def _jd_specs(soup):
    """京东“规格与包装/商品参数”：输出统一的“键：值”行。"""
    out, seen = [], set()

    def add_line(k, v):
        k = _clean(k)
        v = _clean(v)
        if not k or not v or len(k) > 30:
            return
        line = f"{k}：{v}"
        if line not in seen:
            seen.add(line)
            out.append(line)

    # 结构 0-a：HTML 表格行（移动端参数常渲染成 <table>，每行 键/值 两列）
    for tr in soup.select("table tr, [class*='Ptable'] tr, #detParam tr, "
                          "[class*='paramTable'] tr, [class*='specTable'] tr, "
                          "[class*='parameter'] table tr"):
        cells = []
        for td in tr.find_all(["td", "th"]):
            c = _clean(td.get_text(" ", strip=True))
            if c:
                cells.append(c)
        if len(cells) >= 2 and len(cells[0]) <= 30 \
                and not re.search(r"https?://", cells[0], re.I):
            add_line(cells[0], cells[1])

    # 结构 0.5：京东移动版 #detParam 商品参数 / #package 包装清单（两列布局）
    for li in soup.select("#detParam li, #detParam .parameter2 li, "
                          "#package li, #package .mod_row li, "
                          "#detail2 li, #detail2 .parameter2 li, "
                          "#detParam dl, [class*='Ptable'] li, "
                          "[class*='paramInfo'] li, [class*='param-item'] li, "
                          "[class*='attr-item'] li, [class*='spec-item'] li, "
                          "[class*='specification'] li, [class*='parameter-list'] li, "
                          ".m-parameter li, ul.parameter2 li, "
                          "li[class*=\"parameter\"], li[class*=\"spec-item\"]"):
        dt = li.find(["dt", "b", "strong"])
        dd = li.find(["dd", "em", "p"]) if dt else None
        if dt and dd:
            add_line(dt.get_text(" ", strip=True), dd.get_text(" ", strip=True))
            continue
        spans = [s for s in li.find_all("span", recursive=False)
                 if _clean(s.get_text(" ", strip=True))]
        if len(spans) >= 2:
            add_line(spans[0].get_text(" ", strip=True),
                     spans[1].get_text(" ", strip=True))
            continue
        spans = [s for s in li.find_all("span") if _clean(s.get_text(" ", strip=True))]
        if len(spans) >= 2:
            add_line(spans[0].get_text(" ", strip=True),
                     spans[1].get_text(" ", strip=True))

    # 结构 1：li > dl > dt/dd，或 li 内 span 对（京东桌面端常见）
    for li in soup.select(".parameter2 li, .p-parameter li, #detail .p-parameter li, "
                          ".parameter li, ul.parameter2 li, .spec-param li, "
                          "[class*='spec-item'] li"):
        dt = li.find(["dt", "b", "strong"])
        dd = li.find(["dd", "em", "p"]) if dt else None
        if dt and dd:
            add_line(dt.get_text(" ", strip=True), dd.get_text(" ", strip=True))
            continue
        spans = [s for s in li.find_all("span", recursive=False)
                 if _clean(s.get_text(" ", strip=True))]
        if len(spans) >= 2:
            add_line(spans[0].get_text(" ", strip=True),
                     spans[1].get_text(" ", strip=True))
            continue
        spans = [s for s in li.find_all("span") if _clean(s.get_text(" ", strip=True))]
        if len(spans) >= 2:
            add_line(spans[0].get_text(" ", strip=True),
                     spans[1].get_text(" ", strip=True))

    # 结构 2：dt/dd 成对（不限容器）
    for dt in soup.find_all("dt"):
        ddt = _clean(dt.get_text(" ", strip=True))
        dd = dt.find_next("dd")
        if dd and dd.parent is dt.parent:
            add_line(ddt, dd.get_text(" ", strip=True))

    # 结构 3：“键：值”文本行（京东部分参数直接排版成一行）
    if not out:
        for el in soup.select(".attrs li, li, .p-parameter div, .parameter2 div"):
            t = _clean(el.get_text(" ", strip=True))
            if "：" in t or re.match(r"^[\w\u4e00-\u9fa5]{1,20}[:：]", t):
                parts = re.split(r"[:：]", t, maxsplit=1)
                if len(parts) == 2:
                    add_line(parts[0], parts[1])
    return out

def _jd_description(soup):
    """京东商品介绍文字：优先 meta 描述，其次商品介绍/详情区域文本。"""
    for attrs in ({"name": "description"}, {"property": "og:description"},
                  {"name": "twitter:description"}):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            t = _clean(meta["content"])
            if t:
                return t[:2000]
    best = ""
    for sel in ("[class*='productIntro']", "[class*='detail-info']",
                "[class*='product-intro']", "#J-detail-content",
                "[class*='goods-detail']", "[class*='introduce']",
                "[class*='detail-content']"):
        for el in soup.select(sel):
            t = _clean(el.get_text(" ", strip=True))
            if len(t) > len(best):
                best = t
    return best[:4000]


def _jd_feature_fallback(soup):
    """京东无明确“性能特点”文本时，用最有内容的列表兜底。"""
    best_ul = _best_content_ul(soup)
    if best_ul is not None and not _in_skip(best_ul):
        out = []
        _walk_lines(best_ul, out.append)
        items = _filter_noise_lines(ln for ln in out if ln)
        if items:
            return "\n".join(items)[:4000]
    return ""


def _jd_img_alts(soup, title="", limit=20):
    """收集商品图片的 alt/title 文字（把图片里的关键文字识别出来）。

    过滤掉与产品名称重复、过短或属于通用噪音的文字，
    尽量只保留真正描述卖点 / 参数 / 使用场景的图片文字。
    """
    title_prefix = _clean(title)[:12]
    alts, seen = [], set()
    for img in soup.find_all("img"):
        if _in_skip(img):
            continue
        # 规格/可选配置区（颜色、型号、电池容量等 SKU 选项）的图片文字不是性能特点
        _chain = " ".join(" ".join(el.get("class") or []) for el in img.parents)
        _own = " ".join(img.get("class") or [])
        if re.search(r"(specification|spec-|choose|sku-?item|sale-?attr|saleAttr|"
                     r"parameter|param-|option-|color-?size|size-?choose)", _chain + " " + _own, re.I):
            continue
        alt = _clean(img.get("alt") or img.get("title") or "")
        if len(alt) < 6 or _IMG_ALT_SKIP.search(alt) or alt in seen:
            continue
        # 导航/售后/评价类图片文字不是性能特点
        if _JD_NAV_NOISE.search(alt) or (len(alt) <= 40 and _JD_DETAIL_NOISE.search(alt)):
            continue
        if alt == _clean(title) or (title_prefix and alt.startswith(title_prefix)):
            continue
        # 去掉只重复型号 / SKU 的文字（纯 ASCII 字母数字短串）
        if len(re.sub(r"[A-Za-z0-9\-\s]", "", alt)) == 0 and len(alt) <= 30:
            continue
        # 类似“轻巧ONE 灰色 48V12A锂电”这类 SKU 组合（型号+颜色+容量）不是性能特点
        if re.search(r"(灰|黑|白|蓝|红|银|金|粉|紫|绿|棕)色", alt) \
                and re.search(r"(锂电|铅酸|电池|\d+\s*V)", alt):
            continue
        seen.add(alt)
        alts.append(alt)
        if len(alts) >= limit:
            break
    return "\n".join(alts)[:2000]


def _jd_new_description(soup):
    """京东商品介绍：优先 _scoped_1fbfn_1 容器文本，其次移动版介绍区 / attrs 键值行，再通用解析。

    - _scoped_1fbfn_1：用户提供的商品详情容器；
    - #commDesc / #J-detail-content：商品介绍区（多为长图，取图片说明文字）；
    - .attrs：桌面版 label/value 两列（如 品牌：爱玛（AIMA））；
    - 最后退回 meta 描述等通用解析。
    """
    for el in soup.select("._scoped_1fbfn_1"):
        t = _clean(el.get_text(" ", strip=True))
        if len(t) >= 10:
            return t[:4000]
    for sel in ("#commDesc", "#J-detail-content", "[class*='detail-content']",
                "[class*='goods-detail']", "[class*='descContent']",
                "[class*='intro']"):
        for el in soup.select(sel):
            t = _clean(el.get_text(" ", strip=True))
            if len(t) >= 20:
                return t[:4000]
            for img in el.find_all("img"):
                alt = _clean(img.get("alt") or img.get("title") or "")
                if len(alt) >= 6 and not _IMG_ALT_SKIP.search(alt):
                    return alt[:4000]
    attrs = _jd_attrs_lines(soup)
    if attrs:
        return "\n".join(attrs)[:4000]
    return _jd_description(soup)

def _jd_section_by_keyword(soup, keywords, base_url="", max_text=8000, max_imgs=12):
    """按关键字标题定位京东/普通页面区域（如“商品详情”“规格参数”“参数”）。

    思路：找出短文本里含关键字的“标题”元素（如 商品详情 / 规格参数），
    向上找到包含它的内容容器，返回 (区域文本, 区域内图片URL列表)。
    采用“最小足量容器”：从标题向上取最近且正文够小的祖先，
    避免误把整页/整个大区块（含售后、评价等）当成该标题区域。
    找不到返回 ("", [])。
    """
    K = [re.compile(re.escape(kw)) for kw in keywords]
    cands = []
    for el in soup.find_all(True):
        try:
            own = _clean("".join(el.find_all(string=True, recursive=False)))
        except Exception:
            continue
        if not own or len(own) > 40:
            continue
        if not any(k.search(own) for k in K):
            continue
        # 向上找内容容器：选“最小但正文够读”的祖先（从标题往上层级越远正文越长）
        node, best_node, best_len = el, None, None
        for _ in range(9):
            p = node.parent
            if p is None or p.name in ("html", "body") or p is soup:
                break
            node = p
            t = _clean(node.get_text(" ", strip=True))
            if len(t) < 6:
                continue
            if best_node is None or len(t) < best_len:
                best_node, best_len = node, len(t)
            if len(t) <= 600:
                break  # 已经足够小，直接采用，不再往上扩大
        if best_node is None:
            continue  # 找不到合适的正文容器，跳过该候选
        t = _clean(best_node.get_text(" ", strip=True))
        if len(t) < 2:
            continue
        cands.append((len(own), len(t), best_node))
    if not cands:
        return "", []
    # 标题最短、容器最小的优先
    cands.sort(key=lambda x: (x[0], x[1]))
    node = cands[0][2]
    # 用换行取文本，尽量保持“一行一条”的键值/段落结构
    full = node.get_text("\n", strip=True) or ""
    for h in node.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b"]):
        ht = _clean(h.get_text(" ", strip=True))
        if ht and ht in full:
            full = full.replace(ht, "", 1)
    full = re.sub(r"[ \t\r\f\v]+", " ", full)
    full = re.sub(r"\n\s*\n+", "\n", full).strip()
    # 去掉售后/登录/公告等模板噪声行（这些不是商品介绍/参数）
    full = _jd_strip_noise(full)
    imgs = []
    for img in node.find_all("img"):
        src = _jd_img_src(img)
        if not src:
            continue
        u = _resolve(src, base_url)
        if u and re.search(r"\.(png|jpe?g|webp)(\?|$)", u.split("?", 1)[0], re.I):
            if not _jd_bad_img(u):
                if u not in imgs:
                    imgs.append(u)
        if len(imgs) >= max_imgs:
            break
    return full[:max_text], imgs[:max_imgs]
_JD_LOGIN_URL = re.compile(
    r"plogin\.m\.jd\.com|passport\.jd\.com/(new/)?login|/login/login\?|sso\.jd\.com|"
    r"qr\.m\.jd\.com|verify\.jd\.com", re.I)

_JD_NAV_NOISE = re.compile(
    r"(^|\s)(购物车|我的订单|我的京东|企业采购|网站导航|手机京东|网站无障碍|"
    r"首页|全部商品分类|京东超市|京东家电|京东国际|领券中心|值得买|服务热线|"
    r"京ICP备|京公网安备|营业执照|增值电信业务|联系我们)(\s|$)", re.I)

_JD_REVIEW_TEXT = re.compile(
    r"(此用户未填写评价内容|好评率\s*[：:]?\s*\d|追评\s*[：:]|晒单返现|"
    r"来自\s*京东\s*(App|APP)?\s*的评价|^[0-9一二三四五六七八九十百千]+\s*条?评价|"
    r"^用户评价|^宝贝评价|^累计评价|满意度\s*[：:]?\s*\d|"
    r"尺码大小[：:]\s*推荐|舒适度[：:]\s*走|做工[：:]\s*(精细|一般)|"
    r"^[a-zA-Z0-9]\*{2,4}[a-zA-Z0-9](\s|$)|^j\*+[a-z0-9]\s)", re.I | re.M)

# 营销/促销话术：主图、SKU 图、营销横幅上常见，参数配置图里几乎不出现。
# OCR 出的“参数”若一半以上行是这类话术，说明扫到的是产品图而不是参数表。
_JD_MARKET_TEXT = re.compile(
    r"(厂家直销|破损补寄|破损包赔|断货补发|A级正品|假一赔十|正品保障|质保[一二三四五六七八九十百千0-9]+\s*年?|"
    r"超足功率|足功率|官方正品|官方授权|全国联保|售后无忧|极速发货|顺丰包邮|包邮|"
    r"限时抢购|限时立减|下单立减|优惠券|买一送一|免费安装|破损包赔|七天无理由|"
    r"终身质保|质保终身|一年换新|三年换新|五年质保)")


def _jd_market_ratio(text):
    """文本里“营销/促销话术”行占比；>=0.5 视为产品主图/营销图文案，不是参数表。"""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    if not lines:
        return 0.0
    hits = sum(1 for ln in lines if _JD_MARKET_TEXT.search(ln))
    return hits / len(lines)


def _jd_is_param_noise(u):
    """参数图噪声：主图缩略（/s228x228_jfs 等）、晒单/占位/营销图标（imagetools）。"""
    _pl = (str(u or "").split("?", 1)[0] or "").lower()
    if re.search(r"/s\d+x\d+[_-]", _pl):
        return True
    if re.search(r"\b(shaidan|default\.image|imagetools)\b", _pl):
        return True
    return False


def _jd_jsonish(text):
    """判断文本是否像整段 JSON 数据结构（避免把接口 JSON 当详情/参数输出）。"""
    s = str(text or "").strip()
    if not s:
        return True
    if s.startswith(("{", "[")):
        return True
    head = s[:300]
    return bool(re.search(r'"[^"]{1,40}"\s*:', head))


def _jd_clean_param_text(text):
    """参数文本只保留可读行（键：值 / 正常文字行），剔除 JSON、图片链接、噪声行。"""
    if not text:
        return ""
    out, seen = [], set()
    for ln in str(text).splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if re.search(r'"[A-Za-z_]\w*"\s*:', ln):
            continue                       # {"key": 值  结构行
        if ln.startswith(("{", "[")) or ln.startswith(("}", "]")):
            continue
        if re.search(r"https?://\S", ln, re.I):
            continue
        if _JD_DETAIL_NOISE.search(ln) and len(ln) <= 40:
            continue
        # 评价内容混入（“j***z 尺码大小：推荐…”是匿名用户评价，不是商品参数）
        if _JD_REVIEW_TEXT.search(ln):
            continue
        if ln in seen:
            continue
        seen.add(ln)
        out.append(ln)
    joined = "\n".join(out)[:8000]
    # 清洗后整体仍像一段 JSON 数据结构（键全带引号、成对出现）时直接放弃，
    # 避免把接口返回的 JSON 结构当参数输出（用户反馈“参数抓取的都是 json 数据结构”）
    if joined and _jd_jsonish(joined):
        return ""
    # 参数区文本若是“京东价/划线价/折扣”平台价格说明，也不是商品参数
    if joined and _JD_PRICE_TIP.search(joined):
        return ""
    return joined


def _jd_jfs_key(url):
    """京东图片去重键：以 jfs/ 图床路径为准，忽略尺寸/缩略图/格式差异。"""
    u = str(url or "")
    path = u.split("?", 1)[0].split("#", 1)[0]
    m = re.search(r"(jfs/[A-Za-z0-9_\-./]+)", path, re.I)
    if m:
        key = re.sub(r"\.[a-z0-9]+$", "", m.group(1), flags=re.I)
        return key.lower()
    m = re.search(r"(ware-man/[A-Za-z0-9_\-./]+)", path, re.I)
    if m:
        return m.group(1).lower()
    return path.lower()


def _jd_img_better(new, old):
    """判断 new 是否比 old 更适合作为参考图（高清原图 > 缩略图 > webp/avif）。"""
    np_, op_ = (new or "").lower(), (old or "").lower()

    def rank(u):
        r = 0
        if u.endswith((".jpg", ".jpeg", ".png")):
            r += 2
        elif u.endswith(".webp"):
            r += 1
        if re.search(r"/n1/", u) or re.search(r"!q\d+\.dpg", u):
            r += 2
        if "mobilecms" in u:
            r -= 1
        return r

    return rank(np_) > rank(op_)


def _jd_ocr_url(u):
    """把京东缩略图/带质量后缀的链接升级成高清原图，供 OCR 识别图中文字。

    例：//img14.360buyimg.com/n4/jfs/...jpg → //img14.360buyimg.com/n1/jfs/...jpg
        //m.360buyimg.com/.../s750x750_jfs/...jpg!q80.dpg.webp → ...jpg
        //img10.360buyimg.com/imgzone/jfs/...jpg.avif → ...jpg
    """
    u = str(u or "").strip()
    if u.lower().startswith("data:"):
        return u
    u = re.sub(r"![A-Za-z0-9_.]+", "", u)          # 去掉 !q80.dpg.webp / !q70.dpg.webp 等
    u = u.split("?")[0].split("#")[0]
    u = re.sub(r"/n\d+/", "/n1/", u)               # n4/n5 缩略图 → 高清 n1
    low = u.lower()
    if low.endswith(".webp"):
        u = u[:-5]
    elif low.endswith(".avif"):
        u = u[:-5]                                 # .jpg.avif → .jpg
    return u




def _jd_detail_images(soup, base_url, limit=12):
    """收集商品介绍区里的真实详情图（长图），跳过促销/广告/埋点图。

    京东移动版商品介绍（#detail1 / #commDesc）通常只有图片没有文字，
    这些长图需要交给本地 OCR 识别出商品介绍文字。取 12 张覆盖整段正文，
    参数表图可能在中部/尾部（不只看前几张）。
    """
    urls, seen = [], set()

    def in_ad(img):
        node = img
        while node is not None and getattr(node, "parent", None) is not None:
            node = node.parent
            name = getattr(node, "name", None)
            if name in (None, "html", "body"):
                return False
            _id = (node.get("id") or "") if hasattr(node, "get") else ""
            _cls = " ".join(node.get("class") or []) if hasattr(node, "get") else ""
            if re.search(r"(advert|promo|adPosition|banner|report)", _id + " " + _cls, re.I):
                return True
        return False

    for sel in ("#detail1 img", "#detail img", "#commDesc img", "#J-detail-content img",
                "[class*='detail-content'] img", "[class*='goods-detail'] img",
                "[class*='descContent'] img", ".p_desc img", "[class*='intro'] img"):
        for img in soup.select(sel):
            if in_ad(img):
                continue
            src = _jd_img_src(img)
            if not src:
                continue
            u = _resolve(src, base_url)
            if not u or u in seen:
                continue
            p = u.split("?", 1)[0].lower()
            if not re.search(r"\.(png|jpe?g|webp)$", p):
                continue
            if _jd_bad_img(u):
                continue
            seen.add(u)
            urls.append(u)
            if len(urls) >= limit:
                break
        if len(urls) >= limit:
            break
    return urls


def _jd_param_image_urls(soup, base_url, limit=8):
    """收集商品参数容器内的图片 URL（桌面/移动端通用），并兜底扫描页面内嵌 JSON。

    京东部分商品参数以长图形式呈现，需要把图片链接交给 AI 视觉/本地 OCR
    来识别图中的文字。移动版参数容器 #detParam 常为空，参数图往往在
    页面内嵌 JSON（_itemInfo / jd_api_data）里“参数/规格”附近，这里一并收集。
    """
    urls, seen = [], set()

    def add(u):
        if not u:
            return
        abs_url = _resolve(u, base_url)
        if not abs_url or abs_url in seen:
            return
        p = abs_url.split("?", 1)[0].lower()
        if not re.search(r"\.(png|jpe?g|webp|avif)$", p):
            return
        if _jd_bad_img(abs_url):
            return
        seen.add(abs_url)
        urls.append(abs_url)

    for el in soup.select("._scoped_1nhp8_1, #detParam, #detail2, .p-parameter, "
                          "[class*='parameter'], [class*='Ptable'], "
                          "[class*='specification'], [class*='spec-param']"):
        for img in el.find_all("img"):
            add(_jd_img_src(img))
        for a in ("data-url", "data-src", "data-original", "data-lazy-img"):
            add(el.get(a))

    # 兜底：扫描页面内嵌 JSON 里“参数/规格”附近的图片链接（参数区域经常整张是一张长图）
    if not urls:
        raw = str(soup)
        for kw in ("商品参数", "规格参数", "规格与包装", "基本参数", "参数配置", "paramInfo"):
            for m in re.finditer(re.escape(kw), raw):
                lo = max(0, m.start() - 200)
                hi = min(len(raw), m.end() + 500)
                for um in re.finditer(r'https?://[^"\'\s<>\\]+360buyimg\.com[^"\'\s<>\\]*',
                                      raw[lo:hi]):
                    u = um.group(0)
                    if u.lower().endswith((".js", ".css", ".svg")):
                        continue
                    add(u)
                if urls:
                    break
            if urls:
                break
    return urls[:limit]




def _jd_param_alts(soup):
    """参数容器（_scoped_1nhp8_1）内图片的 alt 文字（无 OCR/AI 时的兜底）。

    只收“键：值”形式的参数行（如“材质：PVC”）；SKU 选项图/营销图 alt
    （“AOKANG”“219”“京合发费极速送达/售后天忧”等碎片）一律不收，
    避免把非参数文字当商品参数输出。
    """
    lines, seen = [], set()
    for el in soup.select("._scoped_1nhp8_1"):
        for img in el.find_all("img"):
            alt = _clean(img.get("alt") or img.get("title") or "")
            if not alt or alt in seen or _IMG_ALT_SKIP.search(alt):
                continue
            if not re.search(r"[：:]", alt):
                continue  # 只要键值行；碎片 alt 不是参数
            seen.add(alt)
            lines.append(alt)
    return "\n".join(lines)[:4000]

# 本地 OCR 引擎（RapidOCR）只初始化一次；多线程下加锁，避免重复初始化/并发冲突
_OCR_LOCK = threading.Lock()
_ocr_engine = None


# 京东参数表图常带的标题字眼（用户指路：参数在长图最后几张，大多带这些字眼）
_JD_PARAM_TITLE = re.compile(
    r"(参数配置|基本参数|规格参数|技术参数|主要参数|产品参数|详细参数|参数表|参数信息|"
    r"规格与包装|配置参数|整车参数|核心参数)")

# 属性表/商品详情图带的关键词：DOM 属性表文字或详情图，不是“参数配置”
_JD_ATTR_MARK = re.compile(r"(商品编号|店铺[：:]|上架时间|制造商名称|售后保障|包装清单)")

# 属性表常见键（品牌/编号/货号/制动方式/电池类型…）——带这些键的表是“商品详情”，
# 不是“参数配置”。参数配置表键多是技术量词（电池/电机/功率/容量/电压/尺寸…）。
_JD_ATTR_KEYS = ("品牌", "商品编号", "货号", "上架时间", "店铺",
                 "证书编号", "制造商", "包装清单", "保修", "产地", "授权",
                 "制动方式", "电池类型", "适用人群", "适用场景", "操控方式", "主体")


def _jd_attr_table_ratio(text):
    """文本里“属性表键”占键值行的比例；高则说明这是商品属性表而非参数配置表。

    京东“商品详情”= 属性表（品牌：xx / 货号：xx / 上架时间：xx…），
    京东“商品参数”= 参数配置（电池：72V / 电机：1000W…），两者都是键值行，
    用属性表键占比区分（参数配置表几乎不含这些键）。
    """
    keys = [re.split(r"[：:]", ln, 1)[0].strip() for ln in str(text or "").splitlines()
            if re.search(r"[：:]", ln)]
    if not keys:
        return 0.0
    n_hit = sum(1 for k in keys if any(ak in k for ak in _JD_ATTR_KEYS))
    return n_hit / len(keys)


def _jd_ocr_param_longimages(detail_imgs, cfg, scan_back=3):
    """从正文详情长图找“参数配置/基本参数”参数表图。

    京东“商品参数”大多在详情长图的最后几张（带“参数配置/基本参数”
    字眼，如“电池：72V / 电机：1000W”）；但也有商品把参数表图放在
    长图【中部】（前面还有卖点图/背面图）。因此：
      阶段1 先扫尾部 scan_back+1 张，快速命中常见情况即停；
      阶段2 未命中时把长图剩余（中部/前部）逐张向前补扫，只接受
      参数字眼或键值行>=3 的强参数表，避免误收营销广告图。
    """
    imgs = [u for u in (detail_imgs or []) if u]
    if not imgs:
        return ""
    tail = imgs[-(scan_back + 1):]
    head = imgs[:max(0, len(imgs) - (scan_back + 1))]
    for u in reversed(tail):
        t = _jd_ocr_images([u], cfg)
        if not t:
            continue
        if _JD_ATTR_MARK.search(t):
            continue  # 商品详情属性表图（商品编号/店铺/上架时间…），不是参数配置
        if _jd_attr_table_ratio(t) >= 0.5:
            continue  # 键行一半以上是“品牌/货号/上架时间”等属性键 → 属性表
        if _JD_PARAM_TITLE.search(t) or _jd_param_section_quality(t) >= 2:
            return t
    # 阶段2：参数表图在长图中部时，向前补扫剩余长图
    for u in reversed(head):
        t = _jd_ocr_images([u], cfg)
        if not t:
            continue
        if _JD_ATTR_MARK.search(t) or _jd_attr_table_ratio(t) >= 0.5:
            continue
        if _JD_PARAM_TITLE.search(t) or _jd_param_section_quality(t) >= 3:
            return t
    return ""


def _jd_param_ocr_urls(detail_imgs, param_imgs):
    """参数 OCR 候选图 URL：优先正文详情长图的【最后 3 张】，其次参数图。

    京东“商品参数”常常不是参数图，而是正文详情长图（#commDesc / 描述接口
    content）里的整张参数表图（如“电池：72V / 电机：1000W”），这类参数表
    几乎总在详情长图的最后几张（带“参数配置/基本参数”字眼）。
    """
    cands = []
    for u in reversed(list(detail_imgs or [])[-3:]):
        if u and u not in cands:
            cands.append(u)
    for u in (param_imgs or [])[:3]:
        if u and u not in cands:
            cands.append(u)
    return cands


def _jd_ocr_images(urls, cfg):
    """对图片列表做本地 OCR（RapidOCR），把识别出的文字按行返回。

    未安装 rapidocr_onnxruntime、配置关闭 OCR 或识别失败时返回空字符串，
    不影响抓取主流程。图片先下载再识别，识别在全局锁内串行执行。
    """
    if not urls:
        return ""
    cfg = cfg or {}
    scraper_cfg = cfg.get("scraper") or {}
    try:
        if not bool(scraper_cfg.get("ocr", True)):
            return ""
    except Exception:
        return ""
    try:
        from rapidocr import RapidOCR  # 新版（Python 3.13/3.14 也支持）
    except Exception:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except Exception:
            return ""

    # 1) 先下载图片（不占用 OCR 锁）
    payloads = []
    headers = {"User-Agent": scraper_cfg.get("user_agent") or _DEFAULT_UA}
    verify = bool(scraper_cfg.get("verify_ssl", True))
    _ck = {}
    try:
        for _c in load_jd_cookies(cfg):
            if _c.get("name") and _c.get("value"):
                _ck[_c["name"]] = _c["value"]
    except Exception:
        pass
    for u in urls[:6]:
        try:
            # 用高清原图识别（缩略图/WebP 上文字太小识别不清）
            resp = requests.get(_jd_ocr_url(u), headers=headers, timeout=20, verify=verify,
                                proxies=_proxy_dict(cfg), cookies=_ck or None)
            resp.raise_for_status()
            # 跳过空白/占位图：京东参数图列表里常见几百字节的透明小图
            # （如 jdphoto 空规格图），OCR 识别结果为空还白白耗时数秒
            if len(resp.content) < 2000:
                continue
            payloads.append(resp.content)
        except Exception:
            continue
    if not payloads:
        return ""

    # 2) OCR 识别（全局串行）
    global _ocr_engine
    with _OCR_LOCK:
        try:
            if _ocr_engine is None:
                _ocr_engine = RapidOCR()
            rows = []
            import io as _io
            from PIL import Image as PILImage
            for data in payloads:
                try:
                    img = PILImage.open(_io.BytesIO(data)).convert("RGB")
                    # 商品详情长图往往高达几千像素，OCR 极慢；等比缩到高 2600 内再识别，
                    # 速度提升明显且中文识别率基本不受影响
                    _w, _h = img.size
                    if _h > 2600:
                        _r = 2600.0 / _h
                        img = img.resize((max(1, int(_w * _r)), 2600), PILImage.LANCZOS)
                    out = _ocr_engine(img)
                    if hasattr(out, "txts"):
                        # rapidocr 新版：输出对象带 txts/scores 字段
                        rows.extend(str(t or "").strip() for t in (out.txts or []))
                    elif isinstance(out, (tuple, list)) and len(out) >= 1:
                        # 旧版 rapidocr_onnxruntime：返回 (result, elapse)
                        result = out[0]
                        entries = result if isinstance(result, list) else (result or {}).get("res") or []
                        for entry in entries:
                            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                                rows.append(str(entry[1] or "").strip())
                            elif isinstance(entry, dict):
                                rows.append(str(entry.get("text") or entry.get("txt") or "").strip())
                except Exception:
                    continue
        except Exception:
            return ""
    seen, out = set(), []
    for ln in rows:
        ln = _clean(ln)
        if not ln or ln in seen:
            continue
        seen.add(ln)
        out.append(ln)
    return "\n".join(out)[:8000]




def _jd_price(soup, raw_html=""):
    """京东商品价格：优先 .product-price--main 中 unit+value，其次 JSON price/priceFloor。

    桌面版结构：product-price-panel → product-price--main
      → product-price--unit（¥） + product-price--value（2599） → 输出 ¥2599。
    京东移动端会把价格打码（如 3??9），带 ? 的价格视为无效，返回空串。
    """
    # 桌面版：unit + value 分开展示
    for main_el in soup.select(".product-price--main"):
        val_el = main_el.select_one(".product-price--value")
        if val_el is not None:
            v = _clean(val_el.get_text(" ", strip=True))
            if v and "?" not in v and re.search(r"\d", v):
                unit_el = main_el.select_one(".product-price--unit")
                unit = _clean(unit_el.get_text(" ", strip=True)) if unit_el is not None else ""
                unit = unit.replace(" ", "")
                return f"{unit}{v}" if unit else v

    for sel in (".product-price--main", ".summary-price .p-price", ".p-price",
                ".price"):
        for el in soup.select(sel):
            t = _clean(el.get_text(" ", strip=True))
            if not t or "?" in t:
                continue
            sym = "¥" if ("￥" in t or "¥" in t) else ""
            m = re.search(r"(\d+(?:,\d{3})*(?:\.\d+)?)",
                          t.replace("￥", "").replace("¥", ""))
            if m:
                return f"{sym}{m.group(1)}"
    if not raw_html:
        raw_html = str(soup)
    for pat in (r'"priceFloor"\s*:\s*\{[^{}]*?"price"\s*:\s*"([^"]+)"',
                r'"jdPrice"\s*:\s*"([^"]+)"',
                r'"pPrice"\s*:\s*"([^"]+)"',
                r'"aPrice"\s*:\s*"([^"]+)"',
                r'"realPrice"\s*:\s*"([^"]+)"',
                r'"currentPrice"\s*:\s*"([^"]+)"',
                r'"price"\s*:\s*"([^"]+)"'):
        m = re.search(pat, raw_html)
        if m:
            v = m.group(1)
            if v and "?" not in v and re.search(r"\d", v):
                sym = "¥" if ("¥" in v or "￥" in v) else ""
                return f"{sym}{v}"
    return ""


def _jd_dom_attr_lines(soup):
    """从渲染后的 DOM 提取“商品详情/基本属性”表行列表（品牌：xx / 型号：xx…）。

    京东桌面版：.detail_item .attrs .list .item（label + value 两列）；
    移动版：._scoped_1fbfn_1 内 .item。这套属性表就是“商品基本参数”，
    既要作为商品详情（京东“商品详情”页签展示的就是它），也要作为商品参数。
    """
    lines, seen = [], set()
    sel = (".attribute .list .item, .attribute .item, "
           "._scoped_1fbfn_1 .attrs .item, ._scoped_1fbfn_1 .item, "
           "._scoped_1fbfn_1 li[class*=item]")
    for it in soup.select(sel):
        lab = it.select_one(".label, .name, dt, b, strong")
        val = it.select_one(".value, dd, em, p")
        if not (lab and val):
            spans = [s for s in it.find_all("span", recursive=False)
                     if _clean(s.get_text(" ", strip=True))]
            if len(spans) >= 2:
                lab, val = spans[0], spans[1]
        if lab and val:
            ln = "{}：{}".format(_clean(lab.get_text(" ", strip=True)),
                                 _clean(val.get_text(" ", strip=True)))
            if len(ln) > 3 and ln not in seen:
                seen.add(ln)
                lines.append(ln)
    return lines


def _jd_attr_block(text, title=""):
    """判断一段文字是否为“商品详情”属性表（连续“键：值”行），是则返回清洗后文本。

    京东“商品详情”页签展示的就是属性表（品牌：xx / 商品编号：xx / 型号：xx…），
    属于商品详情内容，不应被 _real_desc_candidate 当作“参数”过滤掉。
    """
    t = (text or "").strip()
    if not t or _jd_garbage_text(t) or _jd_jsonish(t) or _JD_REVIEW_TEXT.search(t):
        return ""
    if _JD_STUB.search(t) or _JD_PRICE_TIP.search(t):
        # 京东“价格说明/划线价/折扣”模板是平台规则，不是商品属性表
        return ""
    ln_list = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if len(ln_list) < 2:
        return ""
    kv = 0
    for ln in ln_list:
        if re.match(r"^[^：:]{1,40}[：:]\S{1,300}$", ln):
            kv += 1
    if kv < max(2, int(len(ln_list) * 0.6)):
        return ""
    if title and _jd_is_title_like(t, title):
        return ""
    return _jd_strip_noise(t)


def _extract_jd(soup, final_url, url, category=None, image_urls=None, raw_html="", cfg=None):
    """京东商品详情页专用解析（按京东固定信息模块），返回 (字段字典, 附加信息字典)。

    结果字典同时包含通用字段（产品名称/设备介绍/…）和京东字段
    （商品名称/商品详情/商品参数/商品价格/参考图），供不同表头类别使用。
    """
    result = {k: "" for k in FIELD_KEYS}
    jd = {k: "" for k in JD_FIELD_KEYS}
    if _jd_stub_detect(soup, final_url):
        # 京东占位页/登录页/验证页：不把页面内容当商品处理；
        # 但浏览器上下文内嵌的京东接口数据（价格/参数/详情）是真实的，仍要保留
        api_stub = _jd_read_api_data(soup)
        if api_stub:
            _jd_fill_from_api(jd, api_stub)
            _jd_fill_from_api(result, api_stub)
            if image_urls is None:
                image_urls = []
            result.update(jd)
            return result, {"url": url, "final_url": final_url,
                            "category": category or "", "image_url": "",
                            "image_urls": image_urls, "param_image_urls": []}
        return result | jd, {"url": url, "final_url": final_url,
                             "category": category or "", "image_url": "",
                             "image_urls": [], "param_image_urls": []}
    if is_jd_landing(final_url or url):
        # 京东活动/落地页不是商品页：页面内容（“京东热卖”标题、页脚导航等）
        # 不能当作商品数据。真实商品数据需从落地页内的 landUrl 跳转后抓取
        # （scrape 已处理）；跳转失败时保持空结果，避免垃圾内容污染字段。
        return result | jd, {"url": url, "final_url": final_url,
                             "category": category or "", "image_url": "",
                             "image_urls": [], "param_image_urls": [],
                             "detail_image_urls": []}
    if image_urls is None:
        image_urls = collect_jd_product_images(soup, final_url)
    if not category:
        category = _extract_category(soup)

    title = _jd_title(soup)[:200]
    result["产品名称"] = title
    jd["商品名称"] = title

    # 页面内嵌 _itemInfo：品牌 / 短标题 / 卖点 / 去品牌名称，可作介绍与用途兜底
    info = _jd_item_info(str(soup))
    info_product = info.get("product") or {}
    info_ext = info_product.get("extend") or {}
    info_feats = info_ext.get("features") or info_ext.get("productFeatures") or {}
    brand = _clean(info.get("brandName") or info_product.get("brandName") or "")
    short_title = _clean(info_feats.get("shortTitle") or "")
    sell_point = _clean(info.get("sellPoint") or info_product.get("sellPoint") or "")
    name_without_brand = _clean(info_product.get("nameWithoutBrand")
                                or (info_ext.get("productFeatures") or {}).get("nameWithoutBrand")
                                or "")

    # 浏览器内嵌的京东接口数据 = 真实 商品详情/参数/价格（优先于名称类兜底）
    api_data = _jd_read_api_data(soup)
    api_desc = (api_data.get("商品详情") or "").strip()

    desc = _jd_new_description(soup)

    # 真实介绍判定：过滤模板/噪声/评价/JSON/参数行/标题重复
    def _real_desc_candidate(t):
        t = (t or "").strip()
        if not t or len(t) < 10 or _JD_GENERIC_DESC.search(t) or _JD_STUB.search(t):
            return ""
        if _JD_PRICE_TIP.search(t):
            return ""
        if _jd_garbage_text(t):
            return ""
        if _jd_jsonish(t) or _JD_REVIEW_TEXT.search(t):
            return ""
        # 整段几乎都是“键：值”参数行时，它是参数，不是商品介绍
        _lns = [ln.strip() for ln in t.splitlines() if ln.strip()]
        _kv = _jd_param_section_quality(t)
        if _lns and _kv >= max(2, len(_lns) // 2):
            return ""
        if _jd_is_title_like(t, title):
            return ""
        return _jd_strip_noise(t)

    if desc:
        desc = _real_desc_candidate(desc)

    # 浏览器内嵌接口的真实介绍优先（标题/卖点兜底只在没有真实介绍时使用）；
    # 整段“键：值”属性表也属于商品详情（京东商品详情页签展示的就是属性表）
    if api_desc:
        _c = _real_desc_candidate(api_desc) or _jd_attr_block(api_desc, title)
        if _c:
            desc = _c
    if not desc:
        _c = _real_desc_candidate(_jd_new_description(soup)) or _jd_attr_block(_jd_new_description(soup), title)
        if _c:
            desc = _c

    # 找不到介绍时，按“商品详情”等字眼定位其下方内容区域（不同网站容器名不一样）
    detail_section_imgs = []
    if not desc:
        _dt, _dimg = _jd_section_by_keyword(soup, ("商品详情", "商品介绍", "详情介绍", "商品描述"), final_url)
        _c = _real_desc_candidate(_dt)
        if _c:
            desc = _c
        if _dimg:
            detail_section_imgs = _dimg

    # 真正的商品介绍常是长图（#commDesc / .detail_pc / #detail1），本地 OCR 识别出文字
    if not desc:
        # 合并浏览器接口数据里的详情长图（商品介绍常是图片，接口拿到的图最真实）
        if api_data and api_data.get("_desc_imgs"):
            for _di in api_data["_desc_imgs"]:
                if _di not in detail_section_imgs:
                    detail_section_imgs.append(_di)
        _dlong = _jd_detail_images(soup, final_url, limit=12)
        for _di in _dlong:
            if _di not in detail_section_imgs:
                detail_section_imgs.append(_di)
        if detail_section_imgs:
            # 详情长图 OCR 较慢，只识别前 3 张（首图通常含核心介绍文字）
            _ocr_d = _jd_ocr_images(detail_section_imgs[:3], cfg or {})
            _c = _real_desc_candidate(_ocr_d)
            if _c:
                desc = _c
    # 京东“商品详情”页签展示的内容就是属性表（品牌/编号/型号…）；
    # 接口没拿到时，从渲染后的 DOM 属性表提取作为商品详情
    if not desc:
        _alines2 = _jd_dom_attr_lines(soup)
        if _alines2:
            _c2 = _jd_attr_block("\n".join(_alines2), title)
            if _c2:
                desc = _c2
    if desc:
        result["设备介绍"] = desc[:4000]
        jd["商品详情"] = result["设备介绍"]

    # 商品参数：优先 _scoped_1nhp8_1 容器文本，其次通用参数解析；
    # 参数为图片时记录图片链接交给 AI/OCR 识别，并先用 alt 文字兜底。
    # 参数文本只保留可读行（键：值），绝不输出接口 JSON 结构。
    param_img_urls = _jd_param_image_urls(soup, final_url)
    _api_pimgs = []
    if api_data:
        for _piu in _jd_biz_param_images(api_data, limit=8):
            if _piu not in _api_pimgs:
                _api_pimgs.append(_piu)
        for _piu in (api_data.get("_param_imgs") or []):
            if _piu not in _api_pimgs:
                _api_pimgs.append(_piu)
    # 过滤掉画廊缩略/晒单/占位图等噪声（s228/s800/shaidan/default.image/imagetools
    # 都是主图缩略/营销图标，不是参数图）；接口参数图（_api_pimgs）同样过滤——
    # 否则未登录拿不到正文长图时，OCR 兜底会去扫主图，把“厂家直销/破损补寄”当参数。
    _api_pimgs_f = [u for u in _api_pimgs if not _jd_is_param_noise(u)]
    _filtered_pi = [u for u in param_img_urls
                    if not _jd_is_param_noise(u) and u not in _api_pimgs_f]
    param_img_urls = _api_pimgs_f + _filtered_pi
    param_alts = _jd_param_alts(soup)
    specs = _jd_specs(soup)
    # 参数候选链（【商品参数 = 参数配置】，属性表属于商品详情，绝不混入）：
    # 结构化 kv 表 → “参数配置/基本参数”字眼 DOM 区 → 参数容器文本；
    # 每个候选都独立清洗，清洗为空（噪声/JSON/评价）才试下一个。
    _param_cands = []
    if specs:
        _param_cands.append("\n".join(specs))
    _kt, _kimgs = _jd_section_by_keyword(soup,
                                         ("参数配置", "基本参数", "规格参数",
                                          "商品参数", "技术参数", "规格与包装", "参数"),
                                         final_url)
    if _kt and _jd_param_section_quality(_kt) >= 2 and not _jd_garbage_text(_kt) \
            and not _JD_ATTR_MARK.search(_kt) and _jd_attr_table_ratio(_kt) < 0.5:
        _param_cands.append(_kt)
    if not param_img_urls and _kimgs:
        param_img_urls = _kimgs
    for _el in soup.select("._scoped_1nhp8_1, #detParam, .p-parameter, "
                           "[class*='paramTable'], [class*='Ptable'], "
                           "[class*='parameter-list']"):
        _t = _clean(_el.get_text("\n", strip=True))
        if len(_t) >= 8:
            _param_cands.append(_t)
        break
    param_text = ""
    for _pc in _param_cands:
        if _jd_attr_table_ratio(_pc) >= 0.5:
            continue  # 键行一半以上是“品牌/货号/上架时间”等属性键 → 属性表，不是参数配置
        _ct = _jd_clean_param_text(_pc)[:8000]
        if _ct:
            param_text = _ct
            break
    param_weak = False
    if param_text:
        result["设备参数"] = param_text
    elif param_alts:
        result["设备参数"] = _jd_clean_param_text(param_alts)[:8000]
        param_weak = True
    # 参数是图片时：本地 OCR 识别图片里的“参数配置”。
    # 京东“商品参数”在【正文详情长图】里（带“参数配置/基本参数”字眼的参数表图，
    # 如“电池：72V / 电机：1000W”），大多在尾部几张、也有在中部；未命中长图再 OCR
    # 参数图兜底。
    if not detail_section_imgs:
        if api_data and api_data.get("_desc_imgs"):
            detail_section_imgs = list(api_data["_desc_imgs"])
        for _di in _jd_detail_images(soup, final_url, limit=12):
            if _di not in detail_section_imgs:
                detail_section_imgs.append(_di)
    # 拿到正文长图（通常需登录）→ 参数配置以长图 OCR 为准（用户明确要求抓长图里的参数）；
    # 未登录拿不到长图时才退回页面文本候选（上面 param_text/param_alts 已填的保持不变）
    if not result["设备参数"] or param_weak or detail_section_imgs:
        _ocr_t = _jd_ocr_param_longimages(detail_section_imgs, cfg or {})
        if not _ocr_t and not result["设备参数"]:
            _ocr_cands = _jd_param_ocr_urls(detail_section_imgs, param_img_urls)
            if _ocr_cands:
                _ocr_t = _jd_ocr_images(_ocr_cands, cfg or {})
        if _ocr_t and not _JD_REVIEW_TEXT.search(_ocr_t) and not _JD_STUB.search(_ocr_t):
            _c_t = _jd_clean_param_text(_ocr_t)
            # 参数图文字：不与商品标题高度重复、清洗后仍有内容即可采用；
            # 不强求“键：值”行数——部分参数图以段落/列表形式呈现文字。
            # 营销话术占一半以上 → 扫到的是产品主图/营销图，不是参数表，拒绝。
            if _c_t and len(_c_t) >= 10 and not _jd_is_title_like(_c_t, title) \
                    and not _JD_ATTR_MARK.search(_c_t) and _jd_attr_table_ratio(_c_t) < 0.5 \
                    and _jd_market_ratio(_c_t) < 0.5:
                result["设备参数"] = _c_t[:8000]
    jd["商品参数"] = result["设备参数"]

    alts = _jd_img_alts(soup, title=title)
    # 注意：真实商品详情拿不到时不再用标题填充“商品详情/设备介绍”——
    # 用户反馈“商品详情抓取的都是标题”，标题属于商品名称字段，
    # 详情留空可触发自动抓取模式的 AI 补全，也不会把标题冒充详情导出。

    # 图片里的文字识别结果，仅在没有明确性能特点文字时作为补充
    feats = _jd_feature_fallback(soup)
    if feats:
        result["性能特点"] = feats
    elif alts:
        result["性能特点"] = alts[:4000]

    # 用途：京东无专门“应用场景”模块，用 卖点/短标题/关键词 兜底
    usage = sell_point if sell_point and not _JD_STUB.search(sell_point) else ""
    if not usage:
        usage = short_title
    if not usage:
        kw = soup.find("meta", attrs={"name": "keywords"})
        if kw and kw.get("content"):
            usage = _clean(kw["content"])
            # meta keywords 常以“, 京东”类平台后缀结尾，不是商品用途
            usage = re.sub(r"[,，]\s*京东\s*$", "", usage).strip()
    if not usage and title:
        usage = title
    if usage:
        result["用途"] = usage[:1000]

    img_text = format_reference_images(image_urls)
    result["参考图片"] = img_text
    jd["参考图"] = img_text

    # 浏览器内嵌的京东接口数据（详情/参数/价格）优先补全空字段
    _api_data = api_data if api_data else _jd_read_api_data(soup)
    if not _api_data:
        _api_data = _jd_read_api_data(soup)
    if _api_data:
        _jd_fill_from_api(jd, _api_data)
        _jd_fill_from_api(result, _api_data)

    result.update(jd)
    meta = {"url": url, "final_url": final_url, "category": category,
            "image_url": image_urls[0] if image_urls else "",
            "image_urls": image_urls, "param_image_urls": param_img_urls,
            "detail_image_urls": detail_section_imgs}
    return result, meta





def _extract_category(soup):
    """识别产品分类：优先面包屑，其次 meta category / keywords。"""
    crumb = (soup.select_one("[class*='breadcrumb']")
             or soup.select_one("[class*='crumb']")
             or soup.select_one("[class*='location']")
             or soup.select_one("[class*='position']"))
    if crumb:
        t = _clean(crumb.get_text(" > ", strip=True)).strip(">").strip(" >")
        t = re.sub(r"\s*>\s*$", "", t)
        segments = [s.strip() for s in t.split(">") if s.strip()]
        segments = [s for s in segments if s not in ("首页", "产品中心", "全部产品")]
        if len(segments) > 1:
            segments = segments[:-1]  # 去掉最后一段（通常是产品名称本身）
        if segments:
            return " / ".join(segments)[:200]
    for attrs in ({"name": "category"}, {"property": "article:section"}):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            t = _clean(meta["content"])
            if t:
                return t[:200]
    kw = soup.find("meta", attrs={"name": "keywords"})
    if kw and kw.get("content"):
        t = _clean(kw["content"])
        if t:
            return t[:200]
    return ""


def _parse_page(soup, final_url, url, category=None, image_urls=None):
    """从解析好的 soup 中提取字段，返回 (字段字典, 附加信息字典)。"""
    result = {k: "" for k in FIELD_KEYS}
    if image_urls is None:
        image_urls = collect_product_images(soup, final_url)
    if not category:
        category = _extract_category(soup)

    # ---- 产品名称：优先 h1（通常最干净），再 og:title，最后 <title> ----
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = _clean(h1.get_text(" ", strip=True))
    if not title:
        og_title = (soup.find("meta", attrs={"property": "og:title"})
                    or soup.find("meta", attrs={"name": "twitter:title"}))
        if og_title and og_title.get("content"):
            title = _clean(og_title["content"])
    if not title and soup.title:
        title = _clean(soup.title.get_text(" ", strip=True))
    title = _clean_title(title)
    result["产品名称"] = title[:200]

    # ---- 网页描述（备用用途字段）----
    desc = ""
    for attrs in ({"property": "og:description"}, {"name": "description"},
                  {"name": "twitter:description"}):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            desc = _clean(meta["content"])
            break

    # ---- 按分区提取：同字段取内容最长（最可能是正文）的分区 ----
    best = {f: "" for f in _SECTION_KEYWORDS}
    for heading, body in _iter_sections(soup):
        field = _match_field(heading)
        if not field:
            continue
        text = _extract_section_text(body)
        if len(text) > len(best[field]):
            best[field] = text
    for field, text in best.items():
        if text:
            result[field] = text[:8000]

    # ---- 各字段兜底 ----
    if not result["用途"] and desc:
        result["用途"] = desc[:2000]

    if not result["设备介绍"]:
        paras, seen = [], set()
        for el in soup.find_all(["p", "div"]):
            cls = " ".join(el.get("class") or [])
            if el.name == "div" and not any(k in cls for k in
                    ("description", "intro", "summary", "about", "detail", "overview")):
                continue
            t = _clean(el.get_text(" ", strip=True))
            for ul in el.find_all("ul"):
                t = t.replace(_clean(ul.get_text(" ", strip=True)), "")
            for junk in ("扫码查看更多", "加入配单", "查看更多", "收起", "展开全部",
                         "本页面图片及内容仅供参考，产品外观、功能、参数等以实物及官方说明书为准。"):
                t = t.replace(junk, "")
            t = re.sub(r"\s{2,}", " ", t).strip()
            if len(t) >= 20 and t not in seen:
                seen.add(t)
                paras.append(t)
            if len(paras) >= 2:
                break
        if paras:
            result["设备介绍"] = "\n".join(paras)[:4000]
        elif category:
            result["设备介绍"] = result["产品名称"]

    if not result["设备参数"]:
        table = soup.find("table")
        if table:
            result["设备参数"] = "\n".join(_table_to_lines(table))[:8000]
        else:
            # 通用兜底：找含“规格/spec”的容器，或参数行密集的容器
            spec_el = None
            for el in soup.find_all(True):
                cls = " ".join(el.get("class") or [])
                if "spec" in cls.lower() and len(_clean(el.get_text(" ", strip=True))) > 50:
                    spec_el = el
                    break
            if spec_el is None:
                best_el, best_n = None, 0
                for el in soup.find_all(["div", "li"]):
                    n = len(el.find_all(class_="item-title"))
                    if n > best_n:
                        best_el, best_n = el, n
                spec_el = best_el if best_n >= 3 else None
            if spec_el is not None:
                out = []
                _walk_lines(spec_el, out.append)
                result["设备参数"] = "\n".join(out)[:8000]

    if not result["性能特点"]:
        best_ul = _best_content_ul(soup)
        if best_ul is not None:
            out = []
            _walk_lines(best_ul, out.append)
            items = _filter_noise_lines(ln for ln in out if ln)
            if items:
                result["性能特点"] = "\n".join(items)[:4000]

    # ---- 参考图片：最多 3 个产品图片 URL（每行一个）----
    result["参考图片"] = format_reference_images(image_urls)

    meta = {"url": url, "final_url": final_url,
            "category": category, "image_url": image_urls[0] if image_urls else "",
            "image_urls": image_urls}
    return result, meta


def _shrink_image(path, cfg):
    """把下载的大图压缩到合理尺寸（最长边默认 800px），减小体积并加快 Excel 导出。"""
    try:
        from PIL import Image as PILImage
        max_px = int(cfg["export"].get("image_max_px", 800))
        with PILImage.open(path) as im:
            if im.width <= max_px and im.height <= max_px:
                return
            im.thumbnail((max_px, max_px), PILImage.LANCZOS)
            ext = os.path.splitext(path)[1].lower()
            if ext == ".png":
                im.save(path, "PNG", optimize=True)
            else:
                im.convert("RGB").save(path, "JPEG", quality=88, optimize=True)
    except Exception:
        pass


def download_image(url, save_dir, index, name, cfg):
    """下载图片到本地，返回本地路径；失败返回空字符串。"""
    if not url:
        return ""
    try:
        headers = {"User-Agent": cfg["scraper"].get("user_agent") or _DEFAULT_UA}
        verify = bool(cfg["scraper"].get("verify_ssl", True))
        resp = requests.get(url, headers=headers, timeout=15, verify=verify,
                            proxies=_proxy_dict(cfg))
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "").lower()
        if "png" in ctype:
            ext = ".png"
        elif "webp" in ctype:
            ext = ".webp"
        elif "gif" in ctype:
            ext = ".gif"
        elif "jpeg" in ctype or "jpg" in ctype:
            ext = ".jpg"
        else:
            ext = ".jpg"
        safe = re.sub(r'[\\/:*?"<>|\s]+', "_", name or "")[:40].strip("_") or f"产品{index}"
        fname = f"{index:02d}_{safe}{ext}"
        os.makedirs(save_dir, exist_ok=True)
        path = os.path.join(save_dir, fname)
        with open(path, "wb") as f:
            f.write(resp.content)
        _shrink_image(path, cfg)   # 压缩大图，加快后续导出
        return path
    except Exception:
        return ""


def _parse_site_page(soup, final_url, url, category=None, image_urls=None, cfg=None):
    """按站点选择解析方式：京东走固定信息模块，其余走通用分区解析。

    京东专用解析缺字段时，用通用解析补齐；非京东页面保持原逻辑。
    """
    if is_jd_url(final_url) or is_jd_url(url):
        result, meta = _extract_jd(soup, final_url, url, category, image_urls,
                                   raw_html=str(soup), cfg=cfg)
        missing = [k for k in FIELD_KEYS if not (result.get(k) or "").strip()]
        # 落地页/占位页不是商品页：不做通用解析兜底，
        # 否则“购物指南：购物流程 / 配送方式：上门自提”这类页脚导航会污染商品参数
        if missing and not _jd_stub_detect(soup, final_url) \
                and not is_jd_landing(final_url or url):
            soup_c = BeautifulSoup(str(soup), "html.parser")
            for tag in soup_c(["script", "style", "noscript", "nav", "header",
                               "footer", "aside"]):
                tag.decompose()
            gen_result, gen_meta = _parse_page(soup_c, final_url, url, category, image_urls)
            for k in missing:
                if not gen_result.get(k):
                    continue
                v = gen_result[k]
                # 通用解析可能命中京东页面里的“价格说明/服务承诺/评价区”等内容，
                # 京东字段必须再经过京东噪声清洗，避免平台模板冒充商品数据
                if k in ("设备介绍", "产品名称", "性能特点"):
                    v = _jd_strip_noise(v)
                    # 单行短文本（如“品牌：Anciently”）不是真实商品介绍
                    if k == "设备介绍" and len(v.splitlines()) <= 1 and len(v) < 30:
                        continue
                else:
                    v = _jd_clean_param_text(v)
                if v and not _JD_PRICE_TIP.search(str(v)) \
                        and not _jd_garbage_text(str(v)) and not _JD_GENERIC_DESC.search(str(v)):
                    result[k] = v
        # 通用兜底补齐后，同步到京东字段，保证两类表头都能取到值
        for jk, gk in (("商品名称", "产品名称"), ("商品详情", "设备介绍"),
                       ("商品参数", "设备参数"), ("参考图", "参考图片")):
            if not (result.get(jk) or "").strip() and (result.get(gk) or "").strip():
                result[jk] = result[gk]
        return result, meta
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside"]):
        tag.decompose()
    return _parse_page(soup, final_url, url, category, image_urls)



def _jd_garbage_text(text):
    """检测字段值是否夹杂京东拦截/错误页/通用模板内容；返回应为空的脏文字，否则空串。"""
    if not text:
        return ""
    strong = (r"您访问的页面不存在|error2?\.aspx|京东JD\.COM是国内|网上购物商城，为您提供|"
              r"正品低价、品质保障、配送及时、轻松购物|京东首页|"
              r"你好，请登录|亲爱的用户，欢迎您|欢迎您来到京东|只为品质生活|"
              r"频控|访问过于频繁|请求过于频繁|访问异常")
    if re.search(strong, text, re.I):
        # 只清整字段，不清含有正常内容的字段（脏文字占比高才清）
        return text if len(text) <= 60 or re.search(strong, text[:120]) else ""
    return ""


def _jd_clean_result(result):
    """清理结果中混入的京东拦截/错误页/通用模板内容（避免垃圾数据占住字段）。"""
    for k, v in list((result or {}).items()):
        if not isinstance(v, str) or not v:
            continue
        g = _jd_garbage_text(v)
        if g:
            result[k] = ""
        elif _JD_LOGIN_TITLE.search(v):
            # 登录页标题等较长却以登录文案为主的字段
            if len(v) <= 80 and _JD_LOGIN_TITLE.search(v):
                result[k] = ""
    return result


def scrape(url, cfg, on_log=None):

    """直接抓取一个产品详情页，返回 (字段字典, 附加信息字典)。"""
    def log(msg):
        if on_log:
            on_log(msg)

    url = normalize_url(url)
    js_mode = str(cfg["scraper"].get("js_render", "auto")).lower()
    js_disabled = js_mode in ("0", "false", "no", "off", "never")

    log(f"请求网页：{url}")
    _js_done = {}  # 本次已用浏览器渲染过的 URL -> html（避免重复启动浏览器）
    html, final_url, _enc = fetch_html(url, cfg, on_log=log)
    soup = BeautifulSoup(html, "html.parser")

    # re.m.jd.com 等京东活动/落地页本身没有商品详情：
    # 若页面里能找到商品编号（SKU），自动转到真实商品详情页再抓取
    if (is_jd_url(url) or is_jd_url(final_url)) and is_jd_landing(final_url or url):
        _sku0 = _jd_extract_sku(soup, final_url or url)
        if not _sku0 and not js_disabled:
            # requests 直抓的落地页壳里商品数据是 JS 动态加载的，
            # 浏览器渲染后 DOM 里才有未转义的真实商品链接
            log("落地页静态 HTML 未解析出商品编号，正在用浏览器引擎解析落地页…")
            try:
                _lh, _lf, _lenc = fetch_html(url, cfg, on_log=log, use_js=True)
                _sku0 = _jd_extract_sku(_lh, _lf)
                if _sku0:
                    _js_done[_lf or url] = _lh
            except Exception as _exc:
                log("落地页浏览器解析失败：%s" % _exc)
        if _sku0 and not re.search(r"/(?:product/)?\d{6,}\.html", final_url or url):
            # 桌面商品页在浏览器上下文里能稳定捕获 pc_detailpage_wareBusiness 等
            # 真实接口（价格/参数/详情），比移动页更可靠，优先使用桌面页
            _item_u = "https://item.jd.com/%s.html" % _sku0
            log("检测到京东活动/落地页，已定位商品编号 %s，转到商品详情页…" % _sku0)
            html, final_url, _enc = fetch_html(_item_u, cfg, on_log=log)
            # 更新 url 为真实商品页地址（后续按 item.jd.com 提取 SKU）
            url = _item_u
            soup = BeautifulSoup(html, "html.parser")
        elif not _sku0:
            log("落地页中未找到商品编号（该活动页可能不含单商品链接），将按页面内容解析…")

    is_jd = is_jd_url(url) or is_jd_url(final_url)
    saw_jd_stub = False
    category = _extract_category(soup)  # 面包屑可能在 nav/header 内，先提取
    all_imgs = (collect_jd_product_images(soup, final_url) if is_jd
                else collect_product_images(soup, final_url))
    result, meta = _parse_site_page(soup, final_url, url, category, all_imgs, cfg)

    # 内容过少（纯动态页面）或京东返回占位页时，自动用浏览器引擎重试
    sparse = (not result["产品名称"]) and not any(
        result.get(k) for k in ("用途", "设备介绍", "性能特点", "设备参数"))
    jd_stub = is_jd and _jd_stub_detect(soup, final_url)
    if (sparse or jd_stub) and not js_disabled:
        reason = "页面内容过少，可能是动态加载页面" if sparse else "京东页面未返回商品数据（疑似反爬/跳转）"
        log(f"{reason}，正在用浏览器引擎重试…")
        try:
            html2, final_url2, _enc2 = fetch_html(url, cfg, on_log=log, use_js=True)
            _js_done[url] = html2
            soup2 = BeautifulSoup(html2, "html.parser")
            is_jd2 = is_jd_url(url) or is_jd_url(final_url2)
            category2 = _extract_category(soup2)
            all_imgs2 = (collect_jd_product_images(soup2, final_url2) if is_jd2
                         else collect_product_images(soup2, final_url2))
            result2, meta2 = _parse_site_page(soup2, final_url2, url, category2, all_imgs2, cfg)
            stub2 = is_jd2 and _jd_stub_detect(soup2, final_url2)
            has_content = result2["产品名称"] or any(result2.get(k) for k in
                    ("用途", "设备介绍", "性能特点", "设备参数"))
            if stub2:
                saw_jd_stub = True
                older_had = result["产品名称"] or any(result.get(k) for k in
                        ("用途", "设备介绍", "性能特点", "设备参数"))
                if older_had:
                    log("浏览器渲染仍被京东拦截（登录/验证页），已保留首次抓取结果")
                else:
                    log("浏览器渲染仍被京东拦截（登录/验证页），未能获取商品数据；"
                        "继续尝试移动版与京东接口补充…")
                # 拦截页/错误页数据一律不采用（避免“页面不存在/登录页”占住真实字段）
                result2 = dict.fromkeys(result2, "")
            elif has_content:
                result, meta = result2, meta2
        except Exception as exc:
            log(f"浏览器重试失败：{exc}（保留首次结果）")

    # 京东桌面版常被反爬拦截到登录页：自动改用移动版商品页再试一次
    # 先走 requests 直抓（速度快、常能拿到真实移动版页面），失败再上浏览器渲染
    if is_jd and (not result["产品名称"]) and not js_disabled:
        m = re.search(r"/(\d{6,})\.html", url)
        if m:
            mobile_url = "https://item.m.jd.com/product/%s.html" % m.group(1)
            log("京东桌面版被拦截，正在尝试移动版商品页…")
            for _use_js in (False, True):
                try:
                    html3, final_url3, _enc3 = fetch_html(mobile_url, cfg, on_log=log, use_js=_use_js)
                    if _use_js:
                        _js_done[mobile_url] = html3
                    soup3 = BeautifulSoup(html3, "html.parser")
                    if _jd_stub_detect(soup3, final_url3):
                        saw_jd_stub = True
                        log("移动版返回登录/验证页%s" % ("，改用浏览器渲染重试…" if not _use_js else "，未能获取商品数据。"))
                        continue
                    category3 = _extract_category(soup3)
                    all_imgs3 = collect_jd_product_images(soup3, final_url3)
                    result3, meta3 = _parse_site_page(soup3, final_url3, url, category3, all_imgs3, cfg)
                    if result3["产品名称"]:
                        result, meta = result3, meta3
                        break
                    log("移动版商品页未解析出商品名%s" % ("，改用浏览器渲染重试…" if not _use_js else "。"))
                except Exception as exc:
                    log(f"移动版商品页重试失败：{exc}（保留现有结果）")
                    break
            if not result["产品名称"]:
                log("移动版商品页也未获取到商品数据（可能需登录京东）。")

    # 京东仍为占位页时给出明确提示
    if is_jd and (not result["产品名称"]) and (final_url and _JD_STUB.search(final_url)):
        log("京东商品页被反爬拦截或需登录，未能获取商品数据；"
            "建议在浏览器中登录京东后复制商品链接，或稍后重试。")

    # 京东商品详情/参数/价格常需登录/接口异步加载，页面拿不到时用京东公开接口补充
    if is_jd:
        m = re.search(r"/(\d{6,})\.html", url)
        if m:
            sku = m.group(1)

            def _jd_need_fields():
                return [k for k in ("商品详情", "商品参数", "商品价格")
                        if not (result.get(k) or "").strip()
                        or bool(_jd_garbage_text(result.get(k) or ""))]

            need = _jd_need_fields()
            if need:
                log("京东页面缺 %s，正在尝试京东公开接口补充…" % "、".join(need))
                # 优先复用本次已渲染页面内嵌的接口数据（不再重复启动浏览器）
                _embedded = None
                for _h in list(_js_done.values()):
                    _d = _jd_read_api_data(_h)
                    if _d:
                        _embedded = _d
                        break
                api = _jd_api_supplement(sku, cfg, on_log=log, browser_data=_embedded)
                _jd_fill_from_api(result, api)

            # 接口直连仍缺/被拦截时，用浏览器上下文抓接口数据（携带 Cookie、可绕过请求级反爬）
            still = _jd_need_fields()
            if still and not js_disabled:
                _login_ok = jd_session_status(cfg)[0] == "valid"
                if not _login_ok and not (result.get("产品名称") or "").strip():
                    # 页面完全没拿到（被频控/跳登录）+ 未登录：再渲染也只是重复失败，
                    # 直接跳过，避免又等 1 分钟（用户反馈“一直提示已加载登录/卡死”）
                    log("未获取到京东页面且未检测到有效登录，"
                        "跳过浏览器接口补充（请先登录京东后重试）")
                else:
                    log("尝试在浏览器上下文内补充京东接口数据…")
                    _browser_tried = False
                    _mobile_u = "https://item.m.jd.com/product/%s.html" % sku
                    # 未登录时桌面版频控/跳登录概率高，只试成功概率更高的移动版
                    for _bu in ((_mobile_u,) if not _login_ok else (_mobile_u, url)):
                        if _bu in _js_done:
                            _bapi = _jd_read_api_data(_js_done[_bu])
                            if _bapi:
                                _jd_fill_from_api(result, _bapi)
                                _browser_tried = True
                            still = _jd_need_fields()
                            if not still:
                                break
                            continue
                        try:
                            _bhtml, _bfinal, _ = fetch_html(_bu, cfg, on_log=log, use_js=True)
                            _js_done[_bu] = _bhtml
                            _bapi = _jd_read_api_data(_bhtml)
                            if _bapi:
                                _jd_fill_from_api(result, _bapi)
                                _browser_tried = True
                            still = _jd_need_fields()
                            if not still:
                                break
                        except Exception as exc:
                            log("浏览器补充京东接口失败：%s" % exc)
                            break
                    if _browser_tried:
                        log("已用浏览器上下文补充京东商品数据（含可能需登录的价格/参数/详情）")
    # 清理：绝不让拦截页/错误页/登录页内容占住字段
    _jd_clean_result(result)

    if meta.get("category"):
        log(f"识别产品分类：{meta['category']}")
    log(f"抓取完成：{result['产品名称'] or url}，发现 {len(meta['image_urls'])} 张图片")
    return result, meta

