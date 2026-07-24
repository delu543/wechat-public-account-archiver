from wechat_crawler.crawler.normalizer import normalize_url, stable_url_key


def test_normalize_wechat_article_url_keeps_stable_identity_params():
    raw = (
        "https://mp.weixin.qq.com/s?__biz=MzIxExample==&mid=2247483650"
        "&idx=1&sn=abc123&chksm=zzz&scene=27#wechat_redirect"
    )

    normalized = normalize_url(raw)

    assert normalized == (
        "https://mp.weixin.qq.com/s?__biz=MzIxExample%3D%3D"
        "&mid=2247483650&idx=1&sn=abc123&chksm=zzz"
    )
    assert stable_url_key(raw) == "wechat:MzIxExample==:2247483650:1:abc123"


def test_normalize_non_wechat_url_removes_fragment_and_sorts_query():
    raw = "https://example.com/path?b=2&a=1&utm_source=x#part"

    assert normalize_url(raw) == "https://example.com/path?a=1&b=2"
    assert stable_url_key(raw) == "https://example.com/path?a=1&b=2"
