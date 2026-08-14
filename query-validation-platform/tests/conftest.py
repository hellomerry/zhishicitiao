import pytest
from unittest.mock import patch

FAKE_IMAGE = {"hash": "abc123", "image_url": "https://example.com/i.png", "model_version": "z-image-turbo"}


@pytest.fixture(autouse=True)
def mock_image_gen():
    # 测试环境不真实调用生图 API，mock 掉 generate_image
    with patch("src.gateway.image_gen.generate_image", return_value=FAKE_IMAGE) as m:
        yield m
