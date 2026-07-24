from concurrent.futures import ThreadPoolExecutor
import json

from v8serialize import dumps

from wechat_crawler.crawler.discoverers.content_flow_cache_discoverer import (
    WechatContentFlowCacheDiscoverer,
)


def test_content_flow_cache_discoverer_extracts_structured_article_and_redacts_session(tmp_path):
    biz = "MzExampleFlow=="
    wechat_id = "gh_example_flow"
    url = (
        "https://mp.weixin.qq.com/s?__biz=MzExampleFlow=="
        "&mid=2649902603&idx=1&sn=abc&chksm=def&scene=126&sessionid=secret#rd"
    )
    inner = {
        "base_resp": {"ret": 0, "errmsg": "ok", "wxtoken": 777, "sessionid": "svr_secret"},
        "user_name": wechat_id,
        "nick_name": "刘晓光Savvy",
        "alias": "liuxiaoguang",
        "link": url,
        "mid": 2649902603,
        "idx": 1,
        "create_time": "2026-06-25 12:05",
        "ori_create_time": 1782360300,
        "author": "",
        "title": "第一行标题\n\n第二段正文",
        "content_noencode": "第一行标题\n\n第二段正文",
        "signature": "分享人生经验，提高知识水平",
        "cdn_url": "https://mmbiz.qpic.cn/cover.jpg",
    }
    outer = {
        "mp.weixin.qq.com/s?__biz=MzExampleFlow%3D%3D&mid=2649902603&idx=1": {
            "Url": url,
            "Content": json.dumps(inner, ensure_ascii=False),
        }
    }
    blob = tmp_path / "IndexedDB" / "weixin_xworker_0.indexeddb.blob" / "1" / "00" / "1"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(
        b"\xff\x15\xfe"
        + dumps(
            {
                "name": "__batch_get_appmsg_data",
                "data": json.dumps(outer, ensure_ascii=False),
            }
        )
    )

    articles = WechatContentFlowCacheDiscoverer(tmp_path, biz, wechat_id).discover()

    assert len(articles) == 1
    article = articles[0]
    assert article.account_name == "刘晓光Savvy"
    assert article.account_id == biz
    assert article.wechat_id == wechat_id
    assert article.title == "第一行标题"
    assert article.publish_time == "2026-06-25 12:05"
    assert article.content_text == "第一行标题\n\n第二段正文"
    assert "scene=" not in article.source_url
    assert "sessionid" not in article.source_url
    assert "wxtoken" not in article.raw_html
    assert "sessionid" not in article.raw_html


def test_content_flow_cache_discoverer_ignores_other_accounts(tmp_path):
    outer = {
        "mp.weixin.qq.com/s?__biz=OTHER&mid=1&idx=1": {
            "Url": "https://mp.weixin.qq.com/s?__biz=OTHER&mid=1&idx=1&sn=x",
            "Content": json.dumps(
                {
                    "user_name": "gh_other",
                    "nick_name": "其他号",
                    "link": "https://mp.weixin.qq.com/s?__biz=OTHER&mid=1&idx=1&sn=x",
                    "title": "不应导入",
                    "content_noencode": "不应导入",
                },
                ensure_ascii=False,
            ),
        }
    }
    blob = tmp_path / "blob"
    blob.write_bytes(dumps({"name": "__batch_get_appmsg_data", "data": json.dumps(outer, ensure_ascii=False)}))

    articles = WechatContentFlowCacheDiscoverer(tmp_path, "MzExampleFlow==", "gh_example_flow").discover()

    assert articles == []


def test_content_flow_cache_discoverer_converts_cached_html_to_clean_text(tmp_path):
    biz = "MzExampleFlow=="
    wechat_id = "gh_example_flow"
    url = "https://mp.weixin.qq.com/s?__biz=MzExampleFlow==&mid=3&idx=1&sn=abc"
    inner = {
        "user_name": wechat_id,
        "nick_name": "刘晓光Savvy",
        "link": url,
        "create_time": "2026-07-03 11:30",
        "content_noencode": '<p style="line-height: 2em;">第一段<br/>第二行</p><script>x()</script><p>第三段</p>',
    }
    blob = tmp_path / "blob"
    blob.write_bytes(
        dumps(
            {
                "name": "__batch_get_appmsg_data",
                "data": json.dumps({url: {"Url": url, "Content": json.dumps(inner, ensure_ascii=False)}}, ensure_ascii=False),
            }
        )
    )

    articles = WechatContentFlowCacheDiscoverer(tmp_path, biz, wechat_id).discover()

    assert len(articles) == 1
    assert articles[0].title == "第一段"
    assert articles[0].content_text == "第一段\n第二行\n\n第三段"
    assert "<p" not in articles[0].content_text


def test_content_flow_cache_read_works_from_automation_worker_thread(tmp_path):
    cache = tmp_path / "blob"
    cache.write_bytes(b"worker-safe")
    discoverer = WechatContentFlowCacheDiscoverer(tmp_path, "biz", "gh_id")

    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(discoverer._read_bytes, cache).result(timeout=2) == b"worker-safe"
