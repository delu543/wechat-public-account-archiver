from wechat_crawler.crawler.fetcher import ArticleFetcher


def test_fetcher_marks_scan_to_open_full_service_page_as_restricted():
    text = "<span>微信扫一扫可打开此内容</span><span>使用完整服务</span>"

    status = ArticleFetcher()._status_from_response(200, text)

    assert status == "restricted:unsupported"


def test_fetcher_allows_article_body_with_scan_to_open_overlay():
    text = """
    <div id="js_content" class="rich_media_content">正文</div>
    <span>微信扫一扫可打开此内容</span><span>使用完整服务</span>
    """

    status = ArticleFetcher()._status_from_response(200, text)

    assert status == "ok"


def test_fetcher_allows_public_short_content_with_scan_to_open_overlay():
    text = """
    <script>
      bizuin: 'MzShort==',
      user_name: 'gh_short',
      nick_name: '短内容号',
      window.msg_title = window.title = '短内容正文';
    </script>
    <span>微信扫一扫可打开此内容</span><span>使用完整服务</span>
    """

    status = ArticleFetcher()._status_from_response(200, text)

    assert status == "ok"


def test_fetcher_marks_captcha_redirect_url_as_restricted():
    status = ArticleFetcher()._status_from_response(
        200,
        "<html></html>",
        "https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=redacted",
    )

    assert status == "restricted:captcha"
