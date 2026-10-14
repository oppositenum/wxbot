import os

# 单测默认不拦管理页登录，避免所有 test_client 变 401。线上容器不设此变量，默认要登录。
os.environ.setdefault("WXBOT_UI_AUTH", "0")
