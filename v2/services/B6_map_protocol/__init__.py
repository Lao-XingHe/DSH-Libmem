"""B6 MAP v0.1 三包"""
from .protocol import (
    MemoryRequest, MemoryResponse, MemoryItem, MemoryDrilldown,
    MAPValidator, request_handler,
    # 2026-09-22：validator 接进链路时拆出来的两个入口
    #   request_handler        = 带校验的标准入口（MCP / FastAPI / CLI 都用它）
    #   guarded_request_handler= 同一实现，名字直说「带校验」
    #   raw_request_handler    = 裸管道，无校验（测试/对照用）
    guarded_request_handler, raw_request_handler,
    detect_ambiguity, PRONOUNS,
    # memory_drilldown 的落地：校验 + L2/L3 取原文 + B7 审计都在 service 层
    drilldown_handler, read_drilldown,
    DEPTHS, TASK_TYPES, LAYER_BUDGET,
)

__all__ = [
    "MemoryRequest", "MemoryResponse", "MemoryItem", "MemoryDrilldown",
    "MAPValidator", "request_handler",
    "guarded_request_handler", "raw_request_handler",
    "detect_ambiguity", "PRONOUNS",
    "drilldown_handler", "read_drilldown",
    "DEPTHS", "TASK_TYPES", "LAYER_BUDGET",
]
