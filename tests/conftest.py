"""
Set fake env vars before monitor.py is imported so the credential check
at module-load time doesn't exit(1) in test runs.
"""
import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:fake_token_for_tests")
os.environ.setdefault("TELEGRAM_CHAT_ID", "000000000")
