from datetime import datetime
from zoneinfo import ZoneInfo

from wechat_crawler.crawler.parser import WechatArticleParser


SAMPLE_HTML = """
<!doctype html>
<html>
  <head>
    <meta name="description" content="这是一段文章摘要">
    <meta property="og:image" content="https://mmbiz.qpic.cn/cover.jpg">
    <title>页面标题</title>
  </head>
  <body>
    <h1 id="activity-name"> 测试文章标题 </h1>
    <div id="meta_content">
      <a id="js_name"> 测试公众号 </a>
      <em id="js_author"> 作者A </em>
      <em id="publish_time">2024-01-02 08:00</em>
    </div>
    <div id="js_content">
      <p>正文第一段</p>
      <script>window.bad = true;</script>
      <p>正文第二段 <img data-src="https://mmbiz.qpic.cn/body.png" /></p>
    </div>
    <script>var user_name = "wechat_id_001";</script>
  </body>
</html>
"""


def test_parse_wechat_article_extracts_identity_and_clean_content():
    url = "https://mp.weixin.qq.com/s?__biz=MzIxExample==&mid=2247483650&idx=1&sn=abc123"

    article = WechatArticleParser().parse(SAMPLE_HTML, url)

    assert article.title == "测试文章标题"
    assert article.account_name == "测试公众号"
    assert article.account_id == "MzIxExample=="
    assert article.wechat_id == "wechat_id_001"
    assert article.author == "作者A"
    assert article.publish_time == "2024-01-02 08:00"
    assert article.summary == "这是一段文章摘要"
    assert "正文第一段" in article.content_text
    assert "正文第二段" in article.content_text
    assert "<script" not in article.content_html.lower()
    assert article.image_urls == ["https://mmbiz.qpic.cn/body.png"]
    assert article.cover_image_url == "https://mmbiz.qpic.cn/cover.jpg"
    assert len(article.content_hash) == 64
    assert article.status == "parsed"


def test_parse_keeps_inline_spans_in_one_sentence():
    html = SAMPLE_HTML.replace(
        "<p>正文第一段</p>",
        "<p><span>正文</span><font><span>第一段</span></font><span>。</span></p>",
    )

    article = WechatArticleParser().parse(
        html,
        "https://mp.weixin.qq.com/s?__biz=MzIxExample==&mid=2247483650&idx=1&sn=abc123",
    )

    assert article.content_text == "正文第一段。\n\n正文第二段"


def test_parse_public_short_content_script_fallback():
    html = """
    <!doctype html>
    <html>
      <head><title>微信短内容</title></head>
      <body>
        <script>
          bizuin: 'MzShort==',
          user_name: 'gh_short',
          nick_name: '短内容号',
          window.ct = '1710000000' || '';
          window.msg_title = window.title = '第一行标题\\n\\n第二段正文';
        </script>
        <span>微信扫一扫可打开此内容</span><span>使用完整服务</span>
      </body>
    </html>
    """

    article = WechatArticleParser().parse(html, "https://mp.weixin.qq.com/s/short")

    assert article.title == "第一行标题"
    assert article.account_name == "短内容号"
    assert article.account_id == "MzShort=="
    assert article.wechat_id == "gh_short"
    assert article.publish_time
    assert article.content_text == "第一行标题\n\n第二段正文"
    assert "<p>第二段正文</p>" in article.content_html


def test_script_epoch_publish_time_uses_beijing_timezone():
    published = datetime(2026, 10, 8, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    html = f"""
    <html><body>
      <script>
        bizuin: 'MzShort==',
        nick_name: '测试号',
        window.ct = '{int(published.timestamp())}' || '';
        window.msg_title = window.title = '一段正文';
      </script>
    </body></html>
    """

    article = WechatArticleParser().parse(html, "https://mp.weixin.qq.com/s/short")

    assert article.publish_time == "2026-10-08T00:30:00+08:00"


def test_parse_public_short_content_uses_decoded_full_summary_as_body():
    html = r"""
    <!doctype html>
    <html>
      <head>
        <meta name="description" content="第一段足够长，用于确认短内容页面里的完整正文会被保留下来。\x0a\x0a第二段继续补充正文内容\x26lt;a class=\x26quot;wx_img_refer_link\x26quot;\x26gt;图二\x26lt;/a\x26gt;">
        <title>短内容</title>
      </head>
      <body>
        <script>
          bizuin: 'MzShort==',
          user_name: 'gh_short',
          nick_name: '短内容号',
          window.msg_title = window.title = '短内容';
        </script>
      </body>
    </html>
    """

    article = WechatArticleParser().parse(html, "https://mp.weixin.qq.com/s/short")

    assert article.title == "短内容"
    expected = "第一段足够长，用于确认短内容页面里的完整正文会被保留下来。\n\n第二段继续补充正文内容图二"
    assert article.summary == expected
    assert article.content_text == expected
    assert "\\x0a" not in article.content_text
    assert "wx_img_refer_link" not in article.content_text


def test_parse_reprint_panel_uses_embedded_content_when_js_content_is_empty():
    html = r"""
    <!doctype html>
    <html>
      <head>
        <meta name="description" content="转载文章摘要">
        <title>转载文章标题</title>
      </head>
      <body>
        <h1 id="activity-name">转载文章标题</h1>
        <a id="js_name">目标公众号</a>
        <em id="publish_time">2026-08-31 08:01</em>
        <div id="js_content" class="original_panel_content" aria-hidden="true"></div>
        <script>
          var user_name = "gh_target";
          window.cgiData = {
            content_noencode: '\x3csection\x3e第一段正文\x3c/section\x3e\x3csection\x3e第二段正文\x3c/section\x3e'
          };
        </script>
      </body>
    </html>
    """
    url = "https://mp.weixin.qq.com/s?__biz=MzTarget==&mid=1&idx=1&sn=abc"

    article = WechatArticleParser().parse(html, url)

    assert article.title == "转载文章标题"
    assert article.account_name == "目标公众号"
    assert article.account_id == "MzTarget=="
    assert article.content_text == "第一段正文\n第二段正文"
    assert "第一段正文" in article.content_html
    assert article.publish_time == "2026-08-31 08:01"
