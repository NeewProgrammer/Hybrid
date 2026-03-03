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
# NOTE: Gemini 和千问共享同一套默认翻译提示词，用户可在界面中分别自定义
_DEFAULT_TRANSLATION_PROMPT = (
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

DEFAULT_GEMINI_SYSTEM_PROMPT = _DEFAULT_TRANSLATION_PROMPT
DEFAULT_QWEN_SYSTEM_PROMPT = _DEFAULT_TRANSLATION_PROMPT

# 千问 DashScope OpenAI 兼容接口 Base URL
QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

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
    配置管理器类
    用于将用户在界面设置的 API Key、选择的模型、源语言等偏好数据，
    持久化保存到本地文件，以便下次启动时恢复状态。
    当前存储路径默认位于：`~/.hybrid_subtitle_tool/config.json`
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
        """
        加载配置文件。
        @returns 如果文件不存在或解析失败，则返回包含默认 Gemini 提示词的空字典；
                 否则返回读取到的配置字典，兼顾处理了旧版缺失提示词字段的升级逻辑。
        """
        if not CONFIG_FILE.exists():
            return {
                "gemini_system_prompt": DEFAULT_GEMINI_SYSTEM_PROMPT,
                "qwen_system_prompt": DEFAULT_QWEN_SYSTEM_PROMPT,
            }

        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            # 兼容旧配置：补充默认提示词字段
            if "gemini_system_prompt" not in config:
                config["gemini_system_prompt"] = DEFAULT_GEMINI_SYSTEM_PROMPT
            if "qwen_system_prompt" not in config:
                config["qwen_system_prompt"] = DEFAULT_QWEN_SYSTEM_PROMPT
            return config
        except Exception as e:
            logger.error(f"加载配置失败: {e}")
            return {
                "gemini_system_prompt": DEFAULT_GEMINI_SYSTEM_PROMPT,
                "qwen_system_prompt": DEFAULT_QWEN_SYSTEM_PROMPT,
            }

    def save_config(self, config: dict):
        """保存配置到本地文件"""
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

def get_xf_sign(app_id: str, api_key: str, api_secret: str, body: bytes) -> dict:
    """
    生成讯飞机器翻译 API 的鉴权 Header。
    鉴权过程包含几个关键步骤以确保请求安全：
    1. 生成基于 GMT 的标准 Date 字符串。
    2. 对请求 Body 进行 SHA-256 计算并进行 Base64 编码，生成 Digest。
    3. 构造签名原串（包含 host, date, request-line, digest）。
    4. 使用 API Secret 作为密钥，对签名原串进行 HMAC-SHA256 加密生成 Signature。
    5. 最后将各部分组装为 Authorization Header。
    
    官方文档参考: https://itrans.xfyun.cn/v2/its
    
    @param app_id 讯飞开放平台 AppID
    @param api_key 对应应用接口的 APIKey
    @param api_secret 对应应用接口的 APISecret
    @param body 请求参数体（JSON）的字节数据
    @returns 返回带有完整鉴权信息的 Http Headers 字典
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
