"""_heuristic_split_subjects 单元测试：compare 模式主体拆分的兜底启发式。"""
from src.pipeline.nodes import _heuristic_split_subjects


def test_split_with_he():
    assert _heuristic_split_subjects("小米17 Pro 和 荣耀600 Pro 怎么选") == \
        ("小米17 Pro", "荣耀600 Pro")


def test_split_with_vs():
    assert _heuristic_split_subjects("拉萨八中 vs 实验中学：小升初怎么选") == \
        ("拉萨八中", "实验中学")


def test_split_with_duibi():
    assert _heuristic_split_subjects("骆驼冷风机对比格力冷风机") == \
        ("骆驼冷风机", "格力冷风机")


def test_split_strips_trailing_question():
    assert _heuristic_split_subjects("曲靖一中和衡水中学哪个好？") == \
        ("曲靖一中", "衡水中学")


def test_no_connector_returns_none():
    assert _heuristic_split_subjects("空气炸锅选购指南") is None


def test_empty_returns_none():
    assert _heuristic_split_subjects("") is None
    assert _heuristic_split_subjects(None) is None
