"""B7 审计与数据主权

规格：《书房 OS 迁入 dsh · 执行任务清单 P1-5》
integrity_hash = sha256(header_snapshot || payload_ref)
覆盖 search / write / drilldown / export / delete 全动作
API：审计查询 / 导出 / 单条删除

与 B3 共用同一个 SQLite（audit_log + frames 表在同一份 header.db 里）。
"""
import uuid, json, datetime, os
from typing import Optional


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _integrity_hash(header_snapshot: str, payload_ref: str) -> str:
    """P1-5 规定：sha256(header_snapshot + payload_ref)"""
    import hashlib
    raw = (header_snapshot or "") + "|" + (payload_ref or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


ACTIONS = {"search", "write", "drilldown", "export", "delete"}


class AuditLogger:
    """审计日志落盘 + 数据主权 API。

    全动作覆盖：search / write / drilldown / export / delete。
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path  # None = 使用 B3 默认路径
        self._trace_stack = []  # 可选：嵌套 trace

    def _get_conn(self):
        from services.B3_header_engine import get_conn
        return get_conn(self.db_path)

    def push_trace(self, trace_id: str):
        self._trace_stack.append(trace_id)

    def pop_trace(self) -> Optional[str]:
        return self._trace_stack.pop() if self._trace_stack else None

    # ---------- 核心落盘 ----------

    def log(
        self,
        action: str,
        header_snapshot: dict,
        payload_ref: str,
        request_id: str = "",
        session_id: str = "",
        actor: str = "agent",
        trace_id: str = "",
        decided_by: str = "user",
        conn=None,
    ) -> str:
        """写一条审计记录。返回 trace_id。

        `conn`：由调用方给出时复用**这条**连接且不关闭它 —— 一次检索要写
        `search` + `write(heat)` 两条记录，原先各开一次连接（每次含 3 条 PRAGMA），
        而 B4 的升温写库也要连接。2026-10-28 起这三笔副作用共用一条连接。

        刻意**不做**"丢给后台线程"的异步化：审计是证据链，丢了就等于没发生，
        而 B6 的"重试不重复升温"正是靠它和升温同 trace 落盘来证明的。
        省时间靠减少连接往返，不靠放弃落盘。
        """
        if action not in ACTIONS:
            # 非标准动作也允许（扩展），但记个 warning
            pass

        if not trace_id:
            trace_id = self._trace_stack[-1] if self._trace_stack else uuid.uuid4().hex[:16]

        hs_json = json.dumps(header_snapshot, ensure_ascii=False, default=str)
        ihash = _integrity_hash(hs_json, payload_ref)

        sql = """INSERT OR REPLACE INTO audit_log
                 (trace_id, request_id, session_id, ts, actor, action,
                  header_snapshot, payload_ref, decided_by, integrity_hash)
                 VALUES (?,?,?,?,?,?,?,?,?,?)"""
        row = (
            trace_id, request_id, session_id, _now_iso(), actor, action,
            hs_json, payload_ref, decided_by, ihash,
        )
        conn_ = conn if conn is not None else self._get_conn()
        try:
            conn_.execute(sql, row)
        finally:
            if conn is None:
                conn_.close()
        return trace_id

    # ---------- 便捷方法（全动作覆盖） ----------

    def log_search(self, header_snapshot: dict, memory_ids: list,
                   request_id: str = "", **kw) -> str:
        return self.log("search", header_snapshot,
                        json.dumps(memory_ids, ensure_ascii=False),
                        request_id=request_id, **kw)

    def log_write(self, header_snapshot: dict, memory_ids: list,
                  request_id: str = "", **kw) -> str:
        return self.log("write", header_snapshot,
                        json.dumps(memory_ids, ensure_ascii=False),
                        request_id=request_id, **kw)

    def log_drilldown(self, header_snapshot: dict, memory_id: str,
                      request_id: str = "", **kw) -> str:
        return self.log("drilldown", header_snapshot,
                        memory_id, request_id=request_id, **kw)

    def log_export(self, header_snapshot: dict, path: str,
                   request_id: str = "", **kw) -> str:
        return self.log("export", header_snapshot, path,
                        request_id=request_id, **kw)

    def log_delete(self, header_snapshot: dict, memory_id: str,
                   request_id: str = "", **kw) -> str:
        return self.log("delete", header_snapshot, memory_id,
                        request_id=request_id, **kw)

    # ---------- 审计查询 ----------

    def query(
        self,
        trace_id: str = "",
        action: str = "",
        actor: str = "",
        session_id: str = "",
        limit: int = 100,
    ) -> list[dict]:
        where = []
        params: list = []
        if trace_id:
            where.append("trace_id LIKE ?")
            params.append(f"%{trace_id}%")
        if action:
            where.append("action = ?")
            params.append(action)
        if actor:
            where.append("actor = ?")
            params.append(actor)
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)

        sql = "SELECT * FROM audit_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)

        conn = self._get_conn()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    # ---------- 数据主权 API ----------

    def export_all(self, output_path: Optional[str] = None) -> str:
        """全量导出（含表头 JSONL）。

        Returns: 导出文件路径（output_path 或临时路径）
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM memories ORDER BY updated_at DESC"
            ).fetchall()
        finally:
            conn.close()

        path = output_path or os.path.join(
            os.path.dirname(self.db_path) if self.db_path else "data/index",
            f"export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
        )
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                d = dict(r)
                f.write(json.dumps(d, ensure_ascii=False) + "\n")

        self.log_export({"total": len(rows)}, path)
        return path

    def delete_entry(self, memory_id: str) -> bool:
        """单条删除（数据主权操作）。会写 audit_log(action=delete)。"""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM memories WHERE memory_id = ?",
                (memory_id,)
            ).fetchone()
            if not row:
                return False

            # 先写审计（保留删除动作证据链）
            hs = json.dumps(dict(row), ensure_ascii=False, default=str)
            self.log_delete({"action": "delete_entry_pre",
                             "memory_id": memory_id},
                            memory_id)

            conn.execute("DELETE FROM memories WHERE memory_id = ?",
                         (memory_id,))
        finally:
            conn.close()
        return True

    # ---------- hash 校验 ----------

    def verify_integrity(self, trace_id: str) -> tuple[bool, str]:
        """复算 hash 校验完整性。

        一条 trace 可能有多个动作（search + write …），**逐条校验**，
        全部通过才算通过 —— 只查第一行会漏掉被篡改的那条。
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE trace_id = ? ORDER BY action",
                (trace_id,)
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return (False, "trace_id 不存在")

        broken = []
        for row in rows:
            recomputed = _integrity_hash(row["header_snapshot"], row["payload_ref"])
            if recomputed != row["integrity_hash"]:
                broken.append(row["action"])
        if broken:
            return (False, f"hash 不匹配的动作：{', '.join(broken)}")
        return (True, f"完整性校验通过（{len(rows)} 条动作：{', '.join(r['action'] for r in rows)}）")


# ---------- 模块级便捷函数 ----------

_default_logger = AuditLogger()


def get_recent_logs(limit: int = 20) -> list:
    """最近审计日志（便捷函数）。"""
    return _default_logger.query(limit=limit)


def verify_integrity(memory_id_or_trace_id: str) -> dict:
    """校验完整性。输入可以是 audit_log.trace_id 或 memory_id。

    Returns: {"ok": bool, "message": str, "mode": "memory"|"audit"|"unknown"}

    关于 memories 分支：`memories` 表**没有** integrity_hash 列
    （schema.sql 只在 audit_log 上定义了它），所以逐条记忆的完整性校验
    **当前无法进行**。原先这里直接 `SELECT integrity_hash FROM memories`，
    任何一次调用都会抛 `no such column: integrity_hash` —— 是段必崩的死代码。
    现在改为返回明确的限制说明，而不是崩掉。
    """
    from services.B3_header_engine import get_conn
    conn = get_conn()
    try:
        # 先看这个 id 是不是一条记忆
        row = conn.execute(
            "SELECT memory_id FROM memories WHERE memory_id = ?",
            (memory_id_or_trace_id,)
        ).fetchone()

        # 再试 audit_log（一条 trace 可能有多条动作，逐条校验）
        rows2 = conn.execute(
            "SELECT trace_id, action, integrity_hash, header_snapshot, payload_ref "
            "FROM audit_log WHERE trace_id = ? ORDER BY action",
            (memory_id_or_trace_id,)
        ).fetchall()
        if rows2:
            broken = [r["action"] for r in rows2
                      if _integrity_hash(r["header_snapshot"], r["payload_ref"]) != r["integrity_hash"]]
            ok = not broken
            return {
                "ok": ok,
                "message": (f"审计完整性通过（{len(rows2)} 条动作）" if ok
                            else f"hash 不匹配的动作：{', '.join(broken)}"),
                "mode": "audit",
                "trace_id": rows2[0]["trace_id"],
                "actions": [r["action"] for r in rows2],
            }

        # 是记忆，但记忆层没有可校验的 hash
        if row:
            return {
                "ok": False,
                "message": ("memories 表没有 integrity_hash 列，无法逐条校验记忆完整性；"
                            "请用审计 trace_id 校验（B7 的 hash 只覆盖 audit_log）"),
                "mode": "memory",
                "memory_id": row["memory_id"],
            }

        return {"ok": False, "message": "未找到该 ID 的记录", "mode": "unknown"}
    finally:
        conn.close()
