"""联网搜索（对话面板可选项，用户主动开启才发起）。

- 这是全程序唯二允许的对外请求之二（另一个是模型 API）：只发检索请求，不上传任何文档内容——
  发出去的只有用户当轮的提问关键词。
- provider 配置只存本机 config.json 的 "search" 节（同样 BYOK），或环境变量覆盖。
- 三态诚实降级：没配/失败都不假装搜过，返回的 note 由对话原样告诉用户。
"""
from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
import urllib.request

from src.llm import settings

TIMEOUT = 9
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")


class SearchUnavailable(Exception):
    pass


def search_config() -> dict:
    """config.json 的 search 节 + 环境变量覆盖。provider 默认 bing（免 Key）。"""
    cfg = settings.load()
    section = cfg.get("search") if isinstance(cfg.get("search"), dict) else {}
    provider = (os.environ.get("MIRROR_SEARCH_PROVIDER", "").strip()
                or str(section.get("provider") or "bing").strip().lower())
    key = (os.environ.get("MIRROR_SEARCH_KEY", "").strip()
           or str(section.get("api_key") or "").strip())
    return {
        "provider": provider,
        "api_key": key,
        "max_results": int(section.get("max_results") or 6),
    }


def availability() -> dict:
    cfg = search_config()
    need_key = cfg["provider"] in ("tavily", "bocha")
    return {"available": (not need_key) or bool(cfg["api_key"]),
            "provider": cfg["provider"]}


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", str(text or ""))
    return html.unescape(text).strip()


def _get(url: str, headers: dict | None = None, data: bytes | None = None,
         timeout: int = TIMEOUT) -> bytes:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _bing(query: str, max_results: int) -> list[dict]:
    """必应国内版免 Key：抓结果页解析 b_algo 块。页面改版会导致失败——失败就诚实报错。"""
    url = ("https://cn.bing.com/search?q=" + urllib.parse.quote(query)
           + "&setmkt=zh-CN&count=" + str(max_results))
    page = _get(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}).decode("utf-8", "replace")
    results = []
    pattern = re.compile(
        r'<li class="b_algo".*?<h2[^>]*><a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a></h2>(.*?)</li>',
        re.S)
    for href, title, rest in pattern.findall(page)[:max_results]:
        snippet = ""
        m = re.search(r'<p[^>]*>(.*?)</p>', rest, re.S)
        if m:
            snippet = _clean(m.group(1))
        results.append({"title": _clean(title), "url": href, "snippet": snippet[:220]})
    if not results:
        raise SearchUnavailable("搜索页面解析不到结果（可能是必应改版或网络不通）")
    return results


def _tavily(query: str, max_results: int, key: str) -> list[dict]:
    body = _get("https://api.tavily.com/search", data=json.dumps({
        "api_key": key, "query": query, "max_results": max_results,
        "search_depth": "basic",
    }).encode("utf-8"), headers={"Content-Type": "application/json"})
    data = json.loads(body.decode("utf-8"))
    out = []
    for item in data.get("results", [])[:max_results]:
        out.append({"title": _clean(item.get("title")), "url": str(item.get("url") or ""),
                    "snippet": _clean(item.get("content"))[:220]})
    return out


def _bocha(query: str, max_results: int, key: str) -> list[dict]:
    body = _get("https://api.bochaai.com/v1/web-search", data=json.dumps({
        "query": query, "count": max_results, "summary": True, "freshness": "noLimit",
    }).encode("utf-8"), headers={"Content-Type": "application/json",
                                 "Authorization": f"Bearer {key}"})
    data = json.loads(body.decode("utf-8"))
    pages = ((data.get("data") or {}).get("webPages") or {}).get("value") or []
    out = []
    for item in pages[:max_results]:
        out.append({"title": _clean(item.get("name")), "url": str(item.get("url") or ""),
                    "snippet": _clean(item.get("summary") or item.get("snippet"))[:220]})
    return out


def search_web(query: str) -> tuple[list[dict], str]:
    """返回 (结果列表, note)。失败抛 SearchUnavailable，由调用方决定怎么说实话。"""
    query = str(query or "").strip()
    if not query:
        return [], ""
    cfg = search_config()
    provider, key = cfg["provider"], cfg["api_key"]
    limit = max(3, min(int(cfg["max_results"] or 6), 8))
    if provider == "tavily":
        if not key:
            raise SearchUnavailable("Tavily 需要 API Key：config.json 的 search.api_key")
        results = _tavily(query, limit, key)
    elif provider == "bocha":
        if not key:
            raise SearchUnavailable("博查需要 API Key：config.json 的 search.api_key")
        results = _bocha(query, limit, key)
    elif provider == "bing":
        results = _bing(query, limit)
    else:
        raise SearchUnavailable(f"不认识这个搜索源：{provider}（支持 bing/tavily/bocha）")
    return results, ""
