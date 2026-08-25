"""text_similarity 单元测试：出图文字质检门的相似度判定。"""
from src.pipeline.nodes import text_similarity


def test_identical_is_one():
    assert text_similarity("第1页核心要点", "第1页核心要点") == 1.0


def test_punctuation_and_whitespace_ignored():
    # OCR 常带换行/丢标点，不应拉低相似度
    assert text_similarity("空气炸锅，怎么选？", "空气炸锅\n怎么选") > 0.99


def test_garbled_text_scores_low():
    assert text_similarity("第1页核心要点", "◆■□乱码xyz") < 0.5


def test_empty_expected_passes():
    assert text_similarity("", "任意文字") == 1.0
    assert text_similarity(None, "任意文字") == 1.0


def test_empty_actual_scores_zero():
    assert text_similarity("第1页核心要点", "") == 0.0


def test_partial_match_in_between():
    sim = text_similarity("第1页核心要点", "第1页核心要")  # 缺一字
    assert 0.5 < sim < 1.0
