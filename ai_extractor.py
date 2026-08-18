# -*- coding: utf-8 -*-
"""AI 智能提取模块：调用 OpenAI 兼容接口（可自定义地址 / Key / 模型），
从网页文本中提取结构化产品信息。

兼容性说明（参考 CC-Switch 等中转站使用经验）：
- 中转站 / 代理站通常要求请求路径带 /v1（例如 https://api.xxx.com/v1/chat/completions），
  也有一部分只接受不带 /v1 的路径；本模块会自动依次尝试多个路径。
- 中转站的模型名通常是“别名”，必须使用其 /v1/models 返回的模型 ID；
  可用 fetch_models() 一键获取，再在「AI 设置」中选择。
- 部分免费中转站偶发 404 / 超时，模块会自动重试。
"""

import json
import re
import time

import requests

FIELD_KEYS = ("产品名称", "用途", "设备介绍", "性能特点", "设备参数", "参考图片")

# 京东数据类别对应的字段（与表头一致）
JD_FIELD_KEYS = ("商品名称", "商品详情", "商品参数", "商品价格", "参考图")

# 获取模型列表失败时的兜底选项（可手动输入任意模型名）
COMMON_MODELS = [
    "deepseek-chat", "deepseek-reasoner", "deepseek-v4-flash",
    "gpt-4o-mini", "gpt-4o", "gpt-5.5",
    "claude-haiku-4-5-20251001", "claude-sonnet-5", "claude-opus-5",
    "qwen-plus", "qwen-max", "glm-4-flash", "kimi-k2.5", "moonshot-v1-8k",
    "gemini-2.5-flash", "llama3.1",
]

SYSTEM_PROMPT = """你是一名资深产品经理助理，负责从产品网页内容中提取结构化产品信息。
要求：
1. 严格输出一个 JSON 对象，不要输出任何其他文字、注释或 Markdown 代码块标记。
2. JSON 字段固定为：产品名称、用途、设备介绍、性能特点、设备参数、参考图片。
3. 内容必须忠于原文，不得编造；网页中的导航、广告、推荐内容属于噪音，请忽略。
4. 性能特点请分条整理，每条一行；设备参数请按“参数名：值”或“参数名 | 值”逐行整理。
5. 参考图片填写网页中产品主图的完整 URL；无法确定时填空字符串。
6. 网页常用近义说法请归入对应字段：如“应用场景/适用领域/应用范围”归“用途”，“产品简介/产品概述”归“设备介绍”，“功能特性/产品亮点/主要特性”归“性能特点”，“技术规格/规格参数/技术指标”归“设备参数”。
7. 设备介绍应填写对产品的整体介绍文字（产品简介/产品概述/产品详情），不要填写网页分类路径、面包屑或产品分类信息。
8. 若页面确实没有某字段内容，该字段输出空字符串即可，不要写“未明确说明”“无”之类的占位文字。
9. 网页文本末尾可能带有“【图片中的文字说明】”段落，其中是网页图片中识别出的文字，可用于设备介绍/性能特点，但仅作参考；若与正文矛盾，以正文为准。
10. 京东等电商网页的设备参数常以“键：值”或“键 | 值”形式出现，请一律按“参数名：值”逐行整理。"""

JD_SYSTEM_PROMPT = """你是一名资深电商运营助理，负责从京东商品网页内容中提取结构化商品信息。
要求：
1. 严格输出一个 JSON 对象，不要输出任何其他文字、注释或 Markdown 代码块标记。
2. JSON 字段固定为：商品名称、商品详情、商品参数、商品价格、参考图。
3. 内容必须忠于原文，不得编造；网页中的导航、推荐、广告属于噪音，请忽略。
4. 商品名称填写页面标题中的完整商品名称（含品牌、型号）；商品详情填写对商品的整体介绍文字，不要填写分类路径或面包屑。
5. 商品参数请按“参数名：值”逐行整理；网页文本末尾的“【图片中的文字说明】”可能是参数图片中识别出的文字，可用于商品参数。
6. 商品价格填写页面中显示的真实价格数字（如 5299）；若价格被打码（含 ?）或页面未显示价格，输出空字符串。
7. 参考图填写商品主图的完整 URL，最多 3 个，用换行分隔；无法确定时填空字符串。
8. 若页面确实没有某字段内容，该字段输出空字符串即可，不要写“无”“未明确说明”之类的占位文字。"""

_FIELD_ALIASES = {
    "产品名称": ["产品名称", "产品名", "名称", "name", "product_name", "product name"],
    "用途": ["用途", "应用场景", "应用领域", "适用场景", "use", "usage", "application"],
    "设备介绍": ["设备介绍", "产品介绍", "产品简介", "产品概述", "介绍", "简介", "intro",
               "introduction", "description", "product_intro"],
    "性能特点": ["性能特点", "产品特点", "设备特点", "特点", "性能", "优势", "特性",
               "features", "feature", "highlights"],
    "设备参数": ["设备参数", "技术参数", "产品参数", "规格参数", "参数", "规格",
               "specs", "specifications", "parameters", "params"],
    "参考图片": ["参考图片", "图片", "图片地址", "图片URL", "图片url", "image", "image_url",
               "imageUrl", "img", "图片链接"],
}

JD_FIELD_ALIASES = {
    "商品名称": ["商品名称", "商品名", "名称", "name", "product_name", "skuName", "sku_name"],
    "商品详情": ["商品详情", "商品介绍", "产品介绍", "产品简介", "详情", "介绍",
               "description", "detail", "product_intro"],
    "商品参数": ["商品参数", "产品参数", "规格参数", "技术参数", "参数", "规格",
               "specs", "specifications", "parameters", "params"],
    "商品价格": ["商品价格", "价格", "售价", "现价", "price", "salePrice", "jdPrice"],
    "参考图": ["参考图", "参考图片", "图片", "图片地址", "图片URL", "image", "image_url",
             "imageUrl", "img", "图片链接"],
}

_MAX_ATTEMPTS = 2   # 每个候选地址的尝试轮数（应对中转站偶发故障）


def endpoint_candidates(base_url):
    """返回聊天补全接口的候选地址（按推荐顺序）。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("未配置 AI 请求地址")
    if base.endswith("/chat/completions"):
        return [base]
    if base.endswith("/v1"):
        return [base + "/chat/completions"]
    return [base + "/v1/chat/completions", base + "/chat/completions"]


def models_endpoint_candidates(base_url):
    """返回模型列表接口的候选地址（按推荐顺序）。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("未配置 AI 请求地址")
    if base.endswith("/models"):
        return [base]
    if base.endswith("/v1"):
        return [base + "/models"]
    return [base + "/v1/models", base + "/models"]


def normalize_base_url(base_url):
    """兼容旧调用：返回推荐端点地址。"""
    return endpoint_candidates(base_url)[0]


def _api_error_text(resp):
    """尽量从响应体中提取可读的错误信息。"""
    try:
        data = resp.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])
            if isinstance(err, str) and err:
                return err
    except Exception:
        pass
    return f"HTTP {resp.status_code} {resp.reason}"


def _raise_api_error(resp, context=""):
    """把 API 错误转成带解决建议的中文提示。"""
    msg = _api_error_text(resp)
    prefix = f"{context}：" if context else ""
    if resp.status_code == 403 and ("模型" in msg or "别名" in msg or "alias" in msg.lower()):
        raise ValueError(
            f"{prefix}{msg}\n"
            "解决办法：该 API 只允许调用其模型列表中的模型，请点击「AI 设置 → 获取模型列表」，"
            "选择列表中的模型名称。")
    if resp.status_code == 404:
        raise ValueError(
            f"{prefix}接口地址不存在（{msg}）\n"
            "解决办法：检查 API 请求地址是否正确，中转站一般需要带 /v1，"
            "例如 https://api.xxx.com/v1。")
    if resp.status_code in (401, 403):
        raise ValueError(f"{prefix}{msg}\n请检查 API Key 是否正确。")
    raise ValueError(f"{prefix}{msg}")


def _build_user_prompt(page_text, existing=None, context=None, category=None):
    head = ""
    if context:
        parts = []
        for k in ("来源网址", "产品名称"):
            v = context.get(k)
            if v:
                parts.append(f"{k}：{v}")
        if parts:
            head = "【页面信息】" + "；".join(parts) + "\n\n"
    if existing:
        extra = ("以下字段已初步抓取（可能为空），请核实并补全为更完整、准确的内容，"
                 "保留已有正确信息：\n" + json.dumps(existing, ensure_ascii=False, indent=2))
        return f"{head}网页正文内容如下：\n\n{page_text}\n\n{extra}"
    return f"{head}网页正文内容如下：\n\n{page_text}"


def _build_user_content(page_text, existing=None, context=None, image_urls=None, category=None):
    """构造 user 消息内容。

    - 未启用视觉 / 无图片时返回纯文本；
    - 启用视觉且有图片时返回多模态数组（OpenAI 兼容 image_url 格式），
      让支持视觉的模型直接“看”产品图片（主图/详情图）。
    """
    text = _build_user_prompt(page_text, existing, context, category)
    imgs = [str(u).strip() for u in (image_urls or []) if str(u or "").strip()]
    if not imgs:
        return text
    content = [{"type": "text", "text": text}]
    for u in imgs[:3]:
        content.append({"type": "image_url", "image_url": {"url": u}})
    return content


def _extract_json(text):
    """从模型回复中稳健地解析 JSON。"""
    if not text:
        return {}
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("AI 返回内容中未找到 JSON")
    return json.loads(text[start:end + 1])


def _normalize_result(data, fields=None, aliases=None):
    fields = fields or FIELD_KEYS
    aliases = aliases or _FIELD_ALIASES
    out = {k: "" for k in fields}
    if not isinstance(data, dict):
        return out
    for field, alias_list in aliases.items():
        for key in alias_list:
            if key in data and data[key] is not None:
                val = data[key]
                if isinstance(val, (list, tuple)):
                    val = "\n".join(str(v) for v in val if str(v).strip())
                val = str(val).strip()
                if val:
                    out[field] = val
                    break
    return out


def _is_definitive_error(resp):
    """401 / 模型未配置类的 403 / Key 错误 属于明确的配置问题，无需重试。"""
    if resp.status_code == 401:
        return True
    if resp.status_code == 403:
        msg = _api_error_text(resp)
        return ("模型" in msg or "别名" in msg or "alias" in msg.lower()
                or "key" in msg.lower() or "auth" in msg.lower() or "token" in msg.lower())
    return False


def _post_retry(candidates, payload, headers, timeout, log=None):
    """依次尝试候选地址并整体重试，返回 JSON；全部失败抛可读异常。

    设计要点（针对不稳定中转站）：
    - 某个地址超时/404 不再立即放弃，而是记录后继续尝试其它地址与下一轮重试；
    - 明确的配置错误（401、模型未配置 403 等）立即报错，避免无谓等待；
    - 若最终全部 404，说明地址填写有误，给出带 /v1 的解决建议。
    """
    errors = []
    all_404 = True
    for attempt in range(_MAX_ATTEMPTS):
        for endpoint in candidates:
            try:
                resp = requests.post(endpoint, json=payload, headers=headers, timeout=timeout)
                if resp.status_code < 400:
                    return resp.json()
                err_text = _api_error_text(resp)
                errors.append(f"{endpoint} → HTTP {resp.status_code} {err_text}")
                if resp.status_code == 404:
                    if log:
                        log(f"{endpoint} 返回 404，继续尝试其它路径…")
                    continue
                all_404 = False
                if _is_definitive_error(resp):
                    _raise_api_error(resp)
                continue
            except requests.RequestException as exc:
                all_404 = False
                errors.append(f"{endpoint} → {exc}")
                if log:
                    log(f"网络错误：{exc}")
                continue
    if all_404:
        raise ValueError("接口地址不存在（所有候选路径都返回 404）。\n"
                         "解决办法：检查「AI 设置 → API 请求地址」是否正确；中转站一般填写根地址即可，"
                         "例如 https://api.baipiao.eu.org，程序会自动补全 /v1。")
    raise ValueError("暂时无法连接 AI 服务（网络或服务商不稳定），已尝试：\n" + "\n".join(errors))


def _get_retry(candidates, headers, timeout, log=None):
    """获取类接口（模型列表）的重试封装，返回 JSON。"""
    errors = []
    all_404 = True
    for attempt in range(_MAX_ATTEMPTS):
        for endpoint in candidates:
            try:
                resp = requests.get(endpoint, headers=headers, timeout=timeout)
                if resp.status_code < 400:
                    return resp.json()
                err_text = _api_error_text(resp)
                errors.append(f"{endpoint} → HTTP {resp.status_code} {err_text}")
                if resp.status_code == 404:
                    continue
                all_404 = False
                if _is_definitive_error(resp):
                    _raise_api_error(resp)
                continue
            except requests.RequestException as exc:
                all_404 = False
                errors.append(f"{endpoint} → {exc}")
                continue
    if all_404:
        raise ValueError("接口地址不存在（所有候选路径都返回 404）。\n"
                         "解决办法：检查「AI 设置 → API 请求地址」是否正确；中转站一般填写根地址即可，"
                         "例如 https://api.baipiao.eu.org，程序会自动补全 /v1。")
    raise ValueError("暂时无法获取模型列表（网络或服务商不稳定），已尝试：\n" + "\n".join(errors))


def ai_extract(url, page_text, cfg, on_log=None, existing=None, context=None,
              image_urls=None, category=None):
    """调用 AI 提取产品字段，返回字段字典。

    - image_urls：产品图片 URL 列表（最多 3 张），仅在配置 ai.vision=true
      且模型支持视觉时随请求发送，让模型直接看图；否则只读网页文本。
    - category：数据类别（常规/京东/淘宝/拼多多）。京东时按京东字段
      （商品名称/商品详情/商品参数/商品价格/参考图）提取，其余按通用字段。
    """
    def log(msg):
        if on_log:
            on_log(msg)

    ai_cfg = cfg["ai"]
    is_jd = str(category or "") == "京东"
    fields = JD_FIELD_KEYS if is_jd else FIELD_KEYS
    system_prompt = JD_SYSTEM_PROMPT if is_jd else SYSTEM_PROMPT
    aliases = JD_FIELD_ALIASES if is_jd else _FIELD_ALIASES
    api_key = (ai_cfg.get("api_key") or "").strip()
    if not api_key:
        raise ValueError("未配置 AI API Key，请先在「AI 设置」中填写")
    model = (ai_cfg.get("model") or "").strip()
    if not model:
        raise ValueError("未配置 AI 模型名称，请先在「AI 设置」中获取模型列表并选择")
    max_chars = max(1000, int(ai_cfg.get("max_chars", 20000)))
    page_text = (page_text or "")[:max_chars]
    vision = bool(ai_cfg.get("vision", False))
    imgs = [str(u).strip() for u in (image_urls or []) if str(u or "").strip()]
    use_vision = vision and bool(imgs)

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",
             "content": _build_user_content(page_text, existing, context,
                                            imgs if use_vision else None,
                                            category)},
        ],
        "temperature": float(ai_cfg.get("temperature", 0.2)),
    }
    timeout = int(ai_cfg.get("timeout", 120))
    if use_vision:
        log(f"调用 AI（{model}，附 {len(imgs[:3])} 张产品图片）…")
    else:
        log(f"调用 AI（{model}）…")
    data = _post_retry(endpoint_candidates(ai_cfg.get("base_url")), payload, headers,
                       timeout, log=log)

    message = (data.get("choices") or [{}])[0].get("message") or {}
    content = message.get("content") or ""
    if not content.strip():
        reasoning = message.get("reasoning_content") or ""
        if reasoning:
            raise ValueError(
                "该模型只返回了思考过程、没有给出正式回答，请更换模型"
                "（例如 gpt-5.5 / deepseek-v4-flash 等）或稍后重试。")
        raise ValueError("AI 未返回有效内容，请更换模型或重试。")
    result = _normalize_result(_extract_json(content), fields=fields, aliases=aliases)
    # 京东字段与通用字段互相同步，保证两种表头都能取到值
    if is_jd:
        for gk, jk in (("产品名称", "商品名称"), ("设备介绍", "商品详情"),
                       ("设备参数", "商品参数"), ("参考图片", "参考图")):
            if not result.get(gk) and result.get(jk):
                result[gk] = result[jk]
            elif not result.get(jk) and result.get(gk):
                result[jk] = result[gk]

    # 防幻觉：网页里没有真实参数/价格时，模型常把产品名称原样填到各字段。
    # 识别后清空这些“复制名称”的字段，避免导出垃圾数据。
    name = str(result.get("商品名称") or result.get("产品名称") or "").strip()
    if name:
        for k in list(result):
            v = str(result.get(k) or "").strip()
            if v and k not in ("商品名称", "产品名称") and v == name:
                result[k] = ""
    log("AI 提取完成")
    return result


def test_connection(cfg, on_log=None):
    """测试 AI 配置连通性。

    - API 正确：返回“连接成功：模型 <模型名>，耗时 <N> 毫秒”，并附模型回复；
    - API 信息不对：返回“测试不通：<原因>”；
    - 聊天接口不稳定时，用模型列表接口兜底判断连通性。
    """
    ai_cfg = cfg["ai"]
    api_key = (ai_cfg.get("api_key") or "").strip()
    if not api_key:
        raise ValueError("测试不通：未配置 API Key")
    base_url = (ai_cfg.get("base_url") or "").strip()
    if not base_url:
        raise ValueError("测试不通：未配置 API 请求地址")
    model = (ai_cfg.get("model") or "").strip()
    if not model:
        raise ValueError("测试不通：未配置模型名称（可点击「获取模型列表」选择，或手动输入）")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    chat_error = None

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "请只回复两个字母：OK"}],
        "max_tokens": 128,
        "temperature": 0,
    }
    t0 = time.time()
    try:
        data = _post_retry(endpoint_candidates(base_url), payload, headers, 30)
        elapsed_ms = int((time.time() - t0) * 1000)
        message = (data.get("choices") or [{}])[0].get("message") or {}
        reply = (message.get("content") or "").strip()
        if reply:
            return f"连接成功：模型 {model}，耗时 {elapsed_ms} 毫秒（回复：{reply[:30]}）"
        return f"连接成功：模型 {model}，耗时 {elapsed_ms} 毫秒（模型已响应，未返回文字）"
    except Exception as exc:
        chat_error = exc

    # 聊天失败时用模型列表接口兜底
    try:
        models = fetch_models(cfg)
        elapsed_ms = int((time.time() - t0) * 1000)
        return (f"接口可连通（已获取 {len(models)} 个模型，耗时 {elapsed_ms} 毫秒）；"
                f"模型 {model} 调用未成功：{chat_error}\n"
                f"请检查所选模型是否在该 API 的模型列表中，或更换模型后重试。")
    except Exception as model_exc:
        raise ValueError("测试不通：聊天接口 - " + str(chat_error)
                         + "；模型列表接口 - " + str(model_exc))


# 模型缓存有效期（秒）：期间内启动直接用缓存，不重复联网
MODELS_CACHE_TTL = 6 * 3600


def models_cache_file(cfg=None):
    """模型缓存文件路径（放在输出目录，避免每次启动都联网拉取）。"""
    import os
    try:
        import config as _cfgmod
        out = _cfgmod.get_output_dir(cfg) if cfg else os.getcwd()
    except Exception:
        out = os.getcwd()
    return os.path.join(out, "models_cache.json")


def load_models_cache(cfg):
    """读取模型缓存；仅当 base_url/api_key 未变化且缓存未过期时返回列表，否则返回 None。"""
    import os
    import time
    try:
        path = models_cache_file(cfg)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        key = (cfg["ai"].get("base_url") or "").strip() + "|" + (cfg["ai"].get("api_key") or "").strip()
        if data.get("key") != key:
            return None
        age = time.time() - float(data.get("ts") or 0)
        if age > MODELS_CACHE_TTL:
            return None  # 缓存过期，需要重新联网获取
        ids = [str(m) for m in (data.get("models") or []) if str(m).strip()]
        return ids or None
    except Exception:
        return None


def save_models_cache(cfg, models):
    """把获取到的模型列表写入本地缓存。"""
    import os
    import time
    try:
        path = models_cache_file(cfg)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        key = (cfg["ai"].get("base_url") or "").strip() + "|" + (cfg["ai"].get("api_key") or "").strip()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "key": key,
                       "models": list(models or [])}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def fetch_models(cfg, on_log=None):
    """获取该 API 支持的模型列表（中转站别名），返回模型 ID 列表；成功后写入本地缓存。"""
    ai_cfg = cfg["ai"]
    api_key = (ai_cfg.get("api_key") or "").strip()
    if not api_key:
        raise ValueError("未配置 AI API Key")
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    timeout = int(ai_cfg.get("timeout", 120))
    data = _get_retry(models_endpoint_candidates(ai_cfg.get("base_url")), headers, timeout)
    ids = [str(m.get("id")) for m in data.get("data", []) if m.get("id")]
    ids = sorted(set(ids))
    if not ids:
        raise ValueError("接口返回的模型列表为空")
    try:
        save_models_cache(cfg, ids)
    except Exception:
        pass
    return ids
