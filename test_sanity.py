import sys
from pathlib import Path
sys.path.append(r"d:\AI\Hybrid\src")

from utils import format_timestamp, ConfigManager, get_xf_sign
import os

def test_timestamp():
    print("Testing timestamp format...")
    assert format_timestamp(1.5) == "00:00:01,500"
    assert format_timestamp(3661.123) == "01:01:01,123"
    print("Timestamp test passed.")

def test_config():
    print("Testing ConfigManager...")
    cm = ConfigManager()
    # Save dummy
    cm.save_config({"test": "value"})
    # Load
    cfg = cm.load_config()
    assert cfg.get("test") == "value"
    print(f"Config test passed. Config file at: {cm.CONFIG_FILE}")

def test_sign():
    print("Testing Signature generation...")
    headers = get_xf_sign("appid", "apikey", "apisecret", b"{}")
    assert "Authorization" in headers
    assert "hmac-sha256" in headers["Authorization"]
    print("Signature test passed.")

if __name__ == "__main__":
    test_timestamp()
    test_config()
    test_sign()
