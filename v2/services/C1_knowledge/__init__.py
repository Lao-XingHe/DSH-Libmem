"""C1 知识库（架构书 §5.10）。"""
from .kb_indexer import (
    # 建索引
    scan_knowledge_base, reindex_knowledge,
    # 检索
    search_knowledge_index, related_memories_for,
    # 读文件
    read_any_file, read_kb_file_content,
    # 清单与现状
    list_kb_files, list_kb_tree, knowledge_stats,
    docs_in_index, reader_availability,
    TEXT_EXTS, DOC_EXTS, DEFAULT_MAX_CHARS,
)

__all__ = [
    "scan_knowledge_base", "reindex_knowledge",
    "search_knowledge_index", "related_memories_for",
    "read_any_file", "read_kb_file_content",
    "list_kb_files", "list_kb_tree", "knowledge_stats",
    "docs_in_index", "reader_availability",
    "TEXT_EXTS", "DOC_EXTS", "DEFAULT_MAX_CHARS",
]
