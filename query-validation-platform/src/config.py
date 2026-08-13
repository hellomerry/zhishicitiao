from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://qvp:qvp@localhost:5432/qvp"
    redis_url: str = "redis://localhost:6379/0"
    # 文本模型（spec §1.1 已确定选型）
    deepseek_api_key: str = "sk-xxx"      # DeepSeek 4 Pro：正文生成 + 生图提示词
    kimi_api_key: str = "sk-yyy"          # Kimi K3：校稿检查（一轮）
    # 图片模型
    chatgpt_api_key: str = "sk-zzz"       # ChatGPT：生图
    chatgpt_proxy_url: str = ""           # 网络中转机器地址
    # 搜索（豆包搜索 API 管证据包网页，豆包方舟管搜实景图多模态，两套 key 分开）
    doubao_search_key: str = "sk-www"     # 豆包搜索 API：证据包
    doubao_ark_key: str = "sk-vvv"        # 豆包方舟：搜实景图
    # 系统配置
    environment: str = "dev"
    log_level: str = "INFO"
    heartbeat_timeout_seconds: int = 30
    auto_suspend_timeout_seconds: int = 5400
    sampling_rate: float = 0.20
    anomaly_min_seconds: int = 5
    anomaly_max_seconds: int = 3600


settings = Settings()
