"""Read an article's own primary identity, never the requested URL or body links."""
from __future__ import annotations

import ast
import html
import re

from bs4 import BeautifulSoup

from .biz_push_reader import public_identity

FIELDS = {"biz": 0, "mid": 1, "appmsgid": 1, "idx": 2, "itemidx": 2, "sn": 3}


def primary_page_identity(text):
    soup = BeautifulSoup(text, "html.parser")
    candidates = set()
    for tag in soup.select('link[rel="canonical"],meta[property="og:url"]'):
        if key := public_identity(tag.get("href") or tag.get("content")):
            candidates.add(key)
    fields = [set() for _ in range(4)]
    names = "|".join((*FIELDS, "msg_link", "msgLink", "share_link"))
    pattern = re.compile(r"(?:\bvar\s+|\bwindow\.)(" + names + r")\s*=\s*(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|\d+)")
    for script in soup.find_all("script"):
        for match in pattern.finditer(script.get_text()):
            try:
                value = html.unescape(str(ast.literal_eval(match[2])))
            except (ValueError, SyntaxError):
                continue
            if match[1] in FIELDS:
                fields[FIELDS[match[1]]].add(value)
            elif key := public_identity(value):
                candidates.add(key)
    if all(len(values) == 1 for values in fields):
        key = tuple(next(iter(values)) for values in fields)
        # The same public identity validator is used throughout the archive.
        from .biz_push_reader import permanent_url
        if public_identity(permanent_url(key)):
            candidates.add(key)
    if len(candidates) != 1:
        return None
    result = next(iter(candidates))
    if any(values and values != {result[index]} for index, values in enumerate(fields)):
        return None
    return result
