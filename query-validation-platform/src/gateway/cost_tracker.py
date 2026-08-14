# 模型价格（元 / 1M tokens，缓存未命中，来自客户确认 2026-08-14）
PRICING_CNY_PER_1M = {
    "deepseek": {"prompt": 3.0, "completion": 6.0},   # DeepSeek V4-Pro
    "kimi": {"prompt": 20.0, "completion": 100.0},     # Kimi K3 旗舰
    "gpt-4o": {"prompt": 18.0, "completion": 72.0},    # GPT-4o（汇率 7.2）
    "default": {"prompt": 3.0, "completion": 6.0},
}


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    m = model.lower()
    if "deepseek" in m:
        p = PRICING_CNY_PER_1M["deepseek"]
    elif "moonshot" in m or "kimi" in m or "k3" in m:
        p = PRICING_CNY_PER_1M["kimi"]
    elif "gpt" in m:
        p = PRICING_CNY_PER_1M["gpt-4o"]
    else:
        p = PRICING_CNY_PER_1M["default"]
    return (prompt_tokens / 1_000_000 * p["prompt"]
            + completion_tokens / 1_000_000 * p["completion"])
