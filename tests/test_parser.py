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
