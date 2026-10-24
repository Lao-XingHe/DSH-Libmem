"""mcp_tools — 路由层拆分后的按域独立模块。

每个子模块导出 register(app) 函数，将自己域内的 endpoint 挂载到 FastAPI app。
最终由 services/C2_mcp_server/mcp_server.py 统一调用 register_all(app)。
"""
from . import memory, knowledge, chat, system

def register_all(app):
    """把所有域的 endpoint 注册到 app"""
    memory.register(app)
    knowledge.register(app)
    chat.register(app)
    system.register(app)
    return app
