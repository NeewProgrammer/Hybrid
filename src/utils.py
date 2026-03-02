import json
import os
import base64
import hashlib
import hmac
import datetime
import logging
from pathlib import Path
from email.utils import formatdate
from urllib.parse import urlparse

# 配置相关常量
CONFIG_DIR = Path.home() / ".hybrid_subtitle_tool"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Gemini 默认翻译系统提示词
# NOTE: 以英文写给 API 效果更佳，但用户可在界面中自由修改为中文
DEFAULT_GEMINI_SYSTEM_PROMPT = (
    "You are a professional subtitle translator for movies and TV shows. "
    "You have deep expertise in colloquial speech patterns, slang, idioms, and cultural references "
    "in both English and Japanese. "
    "Translate naturally into Simplified Chinese that sounds like real spoken dialogue — "
    "never stiff or overly literal. "
    "Preserve the speaker's tone, personality, humor, sarcasm, and emotional nuance. "
    "Adapt idioms and cultural references to natural Chinese equivalents when appropriate. "
    "Keep each translation concise to fit subtitle constraints. "
    "For Japanese: handle keigo, casual speech, and gendered speech patterns correctly. "
    "For English: handle contractions, slang, and informal register correctly."
)

# 日志配置
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def format_timestamp(seconds: float) -> str:
    """
    将秒数转换为 SRT 时间戳格式 (HH:MM:SS,mmm)
    例如: 1.5 -> 00:00:01,500
    """
    if seconds < 0:
        seconds = 0
    
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"

class ConfigManager:
    """
    配置管理器，用于保存和加载用户设置
    """
    def __init__(self):
        self._ensure_config_dir()

    def _ensure_config_dir(self):
        if not CONFIG_DIR.exists():
            try:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.error(f"无法创建配置目录: {e}")

    def load_config(self) -> dict:
        """加载配置，如果文件不存在则返回空字典（含默认值）"""
        if not CONFIG_FILE.exists():
            return {"gemini_system_prompt": DEFAULT_GEMINI_SYSTEM_PROMPT}

        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            # 兼容旧配置：补充默认提示词字段
            if "gemini_system_prompt" not in config:
                config["gemini_system_prompt"] = DEFAULT_GEMINI_SYSTEM_PROMPT
            return config
        except Exception as e:
            logger.error(f"加载配置失败: {e}")
            return {"gemini_system_prompt": DEFAULT_GEMINI_SYSTEM_PROMPT}

    def save_config(self, config: dict):
        """保存配置到本地文件"""
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

def get_xf_sign(app_id: str, api_key: str, api_secret: str, body: bytes) -> dict:
    """
    生成讯飞机器翻译 API 的鉴权 Header
    文档参考: https://itrans.xfyun.cn/v2/its
    """
    url = "https://itrans.xfyun.cn/v2/its"
    parsed_url = urlparse(url)
    host = parsed_url.netloc
    path = parsed_url.path

    # 1. 生成 Date
    date = formatdate(timeval=None, localtime=False, usegmt=True)

    # 2. 生成 Digest
    digest = "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode('utf-8')

    # 3. 生成 Signature String
    signature_origin = f"host: {host}\ndate: {date}\nPOST {path} HTTP/1.1\ndigest: {digest}"
    
    # 4. HMAC-SHA256 签名
    signature_sha = hmac.new(
        api_secret.encode('utf-8'),
        signature_origin.encode('utf-8'),
        digestmod=hashlib.sha256
    ).digest()
    signature_sha_base64 = base64.b64encode(signature_sha).decode('utf-8')

    # 5. 构造 Authorization Header
    authorization_origin = (
        f'api_key="{api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line digest", signature="{signature_sha_base64}"'
    )

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Host": host,
        "Date": date,
        "Digest": digest,
        "Authorization": authorization_origin
    }
    
    return headers
