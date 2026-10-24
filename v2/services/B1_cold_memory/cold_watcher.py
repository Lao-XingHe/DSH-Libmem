import os, json, re, io
from datetime import datetime

def _get_vault_dir() -> str:
    from services.shared.config.loader import get_data_path
    path = get_data_path("cold_vault", "./data/cold_vault")
    os.makedirs(path, exist_ok=True)
    return path

def record_conversation(user_text: str, assistant_text: str) -> str:
    vault = _get_vault_dir()
    today = datetime.now().strftime("%Y-%m-%d")
    filepath = os.path.join(vault, f"{today}.jsonl")
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    entry = {"id": datetime.now().strftime("%H%M%S%f")[:12], "time": datetime.now().isoformat(), "user": user_text, "assistant": assistant_text}
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return f"{today}#{entry['id']}"

def _count_jsonl_entries(filepath: str) -> int:
    return sum(1 for _ in open(filepath, "r", encoding="utf-8"))

def _count_md_entries(filepath: str) -> int:
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    return len(re.findall(r'^## \d{12} \|', content, re.MULTILINE))

def _parse_md_cold_file(content: str) -> list:
    """解析 ## {id} | {timestamp} 格式的冷记忆 MD 文件，返回 JSONL 兼容的条目列表"""
    entries = []
    blocks = re.split(r'\n(?=## \d{12} \|)', content)

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        header_match = re.match(r'## (\d{12}) \| (.+)', block)
        if not header_match:
            continue
        entry_id = header_match.group(1)
        timestamp = header_match.group(2).strip()

        tools = ""
        tools_match = re.search(r'\*\*工具\*\*\s*[：:]\s*(.+)', block)
        if tools_match:
            tools = tools_match.group(1).strip()

        user_text = ""
        asst_text = ""

        user_match = re.search(r'\*\*用户\*\*\s*[：:]\s*\n?(.*?)(?=\n\*\*助手\*\*|\Z)', block, re.DOTALL)
        if user_match:
            user_text = user_match.group(1).strip()

        asst_match = re.search(r'\*\*助手\*\*\s*[：:]\s*\n?(.*?)(?=\n---|\Z)', block, re.DOTALL)
        if asst_match:
            asst_text = asst_match.group(1).strip()

        if user_text or asst_text:
            entries.append({
                "id": entry_id,
                "time": timestamp,
                "user": user_text,
                "assistant": asst_text,
            })

    return entries

def _auto_convert_md(filepath: str) -> str:
    """将 .md 冷记忆文件自动转换为 .jsonl，返回 jsonl 路径"""
    jsonl_path = filepath.replace(".md", ".jsonl")
    if os.path.exists(jsonl_path):
        return jsonl_path
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    entries = _parse_md_cold_file(content)
    if not entries:
        return None
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return jsonl_path

def get_cold_status() -> dict:
    vault = _get_vault_dir()
    dates = []
    seen_dates = set()
    if os.path.exists(vault):
        for fname in sorted(os.listdir(vault), reverse=True):
            fp = os.path.join(vault, fname)
            if fname.endswith(".jsonl"):
                date_str = fname.replace(".jsonl", "")
                if date_str in seen_dates:
                    for d in dates:
                        if d["date"] == date_str:
                            d["entries"] += _count_jsonl_entries(fp)
                            d["size_kb"] = max(d["size_kb"], round(os.path.getsize(fp) / 1024, 1))
                    continue
                seen_dates.add(date_str)
                size_kb = round(os.path.getsize(fp) / 1024, 1)
                count = _count_jsonl_entries(fp)
                dates.append({"date": date_str, "file": fname, "size_kb": size_kb, "entries": count})
            elif fname.endswith(".md"):
                date_str = fname.replace(".md", "")
                if date_str in seen_dates:
                    jsonl_path = _auto_convert_md(fp)
                    if jsonl_path:
                        for d in dates:
                            if d["date"] == date_str:
                                d["entries"] += _count_jsonl_entries(jsonl_path)
                                d["size_kb"] = round(os.path.getsize(jsonl_path) / 1024, 1)
                                d["file"] = os.path.basename(jsonl_path)
                    continue
                seen_dates.add(date_str)
                size_kb = round(os.path.getsize(fp) / 1024, 1)
                count = _count_md_entries(fp)
                dates.append({"date": date_str, "file": fname, "size_kb": size_kb, "entries": count})
                _auto_convert_md(fp)
    return {"dates": dates, "total_dates": len(dates), "total_entries": sum(d["entries"] for d in dates)}

def read_cold_memory(date_str: str, entry_id: str = None, max_entries: int = 50) -> list:
    vault = _get_vault_dir()
    jsonl_path = os.path.join(vault, f"{date_str}.jsonl")
    md_path = os.path.join(vault, f"{date_str}.md")

    if os.path.exists(md_path) and not os.path.exists(jsonl_path):
        _auto_convert_md(md_path)

    if os.path.exists(jsonl_path):
        results = []
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    if entry_id and entry.get("id") == entry_id:
                        return [entry]
                    results.append(entry)
                except:
                    pass
        return results[-max_entries:]

    if os.path.exists(md_path):
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()
        entries = _parse_md_cold_file(content)
        if entry_id:
            for e in entries:
                if e.get("id") == entry_id:
                    return [e]
            return []
        return entries[-max_entries:]

    return []

def export_as_markdown(date_str: str) -> str:
    entries = read_cold_memory(date_str, max_entries=99999)
    if not entries: return f"# 书房OS · 冷记忆 · {date_str}\n\n（该日无记录）\n"
    lines = [f"# 书房OS · 冷记忆 · {date_str}", "", f"共 {len(entries)} 条对话", ""]
    for e in entries:
        lines.append(f"## {e.get('id','')} | {e.get('time','')}")
        lines.append("")
        lines.append(f"**用户**：")
        lines.append(e.get('user',''))
        lines.append("")
        lines.append(f"**助手**：")
        lines.append(e.get('assistant',''))
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)

def export_all_cold_as_markdown() -> str:
    status = get_cold_status()
    lines = ["# 书房OS · 全部冷记忆", "", f"共 {status['total_dates']} 天，{status['total_entries']} 条对话", ""]
    for d in status["dates"]:
        lines.append("")
        lines.append(export_as_markdown(d["date"]))
    return "\n".join(lines)

def parse_md_file(content: str) -> list:
    """解析 Markdown 格式的冷记忆文件，返回对话对列表。支持同行和跨行两种格式"""
    pairs = []
    current = {"user": "", "assistant": ""}
    section = None

    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped:
            if current["user"] and current["assistant"]:
                pairs.append(dict(current))
                current = {"user": "", "assistant": ""}
            section = None
            continue

        m = re.match(r'\*\*用户\*\*\s*[：:]\s*', stripped)
        if m:
            text = stripped[m.end():].strip()
            if current["user"] and current["assistant"]:
                pairs.append(dict(current))
                current = {"user": "", "assistant": ""}
            if text:
                current["user"] = text
            section = "user"
            continue

        m = re.match(r'\*\*助手\*\*\s*[：:]\s*', stripped)
        if m:
            text = stripped[m.end():].strip()
            if text:
                current["assistant"] = text
            section = "assistant"
            continue

        if section == "user":
            if current["user"]:
                current["user"] += "\n" + stripped
            else:
                current["user"] = stripped
        elif section == "assistant":
            if current["assistant"]:
                current["assistant"] += "\n" + stripped
            else:
                current["assistant"] = stripped

    if current["user"] and current["assistant"]:
        pairs.append(dict(current))

    return pairs

def parse_txt_file(content: str) -> list:
    """解析纯文本对话文件，支持 '用户:'/'助手:' 或 '用户：'/'助手：' 前缀"""
    pairs = []
    current = {"user": "", "assistant": ""}
    section = None

    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped:
            if current["user"] and current["assistant"]:
                pairs.append(dict(current))
                current = {"user": "", "assistant": ""}
            section = None
            continue

        m = re.match(r'用户\s*[：:]\s*', stripped)
        if m:
            text = stripped[m.end():].strip()
            if text:
                if current["user"] and current["assistant"]:
                    pairs.append(dict(current))
                    current = {"user": "", "assistant": ""}
                current["user"] = text
                section = "user"
            continue

        m = re.match(r'助手\s*[：:]\s*', stripped)
        if m:
            text = stripped[m.end():].strip()
            if text:
                current["assistant"] = text
                section = "assistant"
            continue

        if section == "user":
            current["user"] += "\n" + stripped
        elif section == "assistant":
            current["assistant"] += "\n" + stripped

    if current["user"] and current["assistant"]:
        pairs.append(dict(current))

    return pairs

def import_from_text(content: str, target_date: str = None) -> dict:
    """从文本内容导入对话对到 JSONL，返回导入统计"""
    if target_date is None:
        target_date = datetime.now().strftime("%Y-%m-%d")

    # 尝试 MD 格式
    pairs = parse_md_file(content)
    if not pairs:
        # 回退到 TXT 格式
        pairs = parse_txt_file(content)

    if not pairs:
        return {"status": "empty", "imported": 0, "message": "未能从文件中识别出对话内容"}

    vault = _get_vault_dir()
    filepath = os.path.join(vault, f"{target_date}.jsonl")
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    count = 0
    with open(filepath, "a", encoding="utf-8") as f:
        for pair in pairs:
            entry = {
                "id": datetime.now().strftime("%H%M%S%f")[:12] + str(count),
                "time": datetime.now().isoformat(),
                "user": pair["user"],
                "assistant": pair["assistant"],
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            count += 1

    return {"status": "ok", "imported": count, "date": target_date}

def import_from_file(file_bytes: bytes, filename: str, target_date: str = None) -> dict:
    """从上传文件导入"""
    ext = os.path.splitext(filename)[1].lower()

    if ext == ".docx":
        try:
            import docx2txt
            content = docx2txt.process(io.BytesIO(file_bytes))
        except ImportError:
            return {"status": "error", "message": "需要安装 docx2txt: pip install docx2txt"}
        except Exception as e:
            return {"status": "error", "message": f"解析 docx 失败: {e}"}
    else:
        content = file_bytes.decode("utf-8", errors="replace")

    return import_from_text(content, target_date)


def delete_cold_entry(date_str: str, entry_id: str) -> dict:
    """从冷记忆 JSONL 中删除指定条目"""
    vault = _get_vault_dir()
    jsonl_path = os.path.join(vault, f"{date_str}.jsonl")
    if not os.path.exists(jsonl_path):
        md_path = os.path.join(vault, f"{date_str}.md")
        if os.path.exists(md_path):
            _auto_convert_md(md_path)
        if not os.path.exists(jsonl_path):
            return {"status": "not_found", "message": f"未找到 {date_str} 的冷记忆"}
    deleted = False
    new_lines = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                if entry.get("id") == entry_id:
                    deleted = True
                    continue
            except: pass
            new_lines.append(line)
    if not deleted:
        return {"status": "not_found", "message": f"未找到条目 {entry_id}"}
    with open(jsonl_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    return {"status": "ok", "message": f"已删除 {date_str}#{entry_id}"}
