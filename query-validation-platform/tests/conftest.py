import pytest
from unittest.mock import patch

FAKE_IMAGE = {"hash": "abc123", "image_url": "https://example.com/i.png", "model_version": "z-image-turbo"}
FAKE_SEARCH = [{"title": "来源", "url": "https://example.com/src", "summary": "成立于1990年"}]
FAKE_VERIFY = "成立于1990年"
FAKE_IMAGES = [{"title": "实景图", "image_url": "https://example.com/real.png", "source": "bing", "engine": "bing"}]


@pytest.fixture(autouse=True)
def mock_external_calls():
    # 测试环境不真实调用生图/联网搜索/搜图 API
    with patch("src.gateway.image_gen.generate_image", return_value=FAKE_IMAGE), \
         patch("src.gateway.web_search.web_search", return_value=FAKE_SEARCH), \
         patch("src.gateway.web_search.deepseek_verify", return_value=FAKE_VERIFY), \
         patch("src.gateway.image_search.search_image", return_value=FAKE_IMAGES):
        yield
