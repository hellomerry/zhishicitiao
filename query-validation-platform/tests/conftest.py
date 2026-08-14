import pytest
from unittest.mock import patch

FAKE_IMAGE = {"hash": "abc123", "image_url": "https://example.com/i.png", "model_version": "z-image-turbo"}
FAKE_SEARCH = "搜索结果摘要"


@pytest.fixture(autouse=True)
def mock_external_calls():
    # 测试环境不真实调用生图/联网搜索 API
    with patch("src.gateway.image_gen.generate_image", return_value=FAKE_IMAGE), \
         patch("src.gateway.web_search.web_search", return_value=FAKE_SEARCH):
        yield
