"""mcp_tools.knowledge — 知识库域：扫描、搜索、文件管理（上传/移动/删除/文件夹）。

从 V1 routes.py 拆分而来。
"""
import os
from fastapi import Request, UploadFile, File, Form


def register(app):
    """把知识库域 endpoint 注册到 FastAPI app"""

    @app.get("/knowledge/list")
    async def knowledge_list():
        from services.C1_knowledge import list_kb_files, list_kb_tree
        files = list_kb_files()
        tree = list_kb_tree()
        return {"files": files, "count": len(files), "tree": tree}

    @app.post("/knowledge/scan")
    async def knowledge_scan():
        from services.C1_knowledge import scan_knowledge_base
        count = scan_knowledge_base()
        return {"status": "ok", "files": count}

    @app.post("/knowledge/search")
    async def knowledge_search(request: Request):
        from services.C1_knowledge import search_knowledge_index
        body = await request.json()
        query = body.get("query", "")
        top_k = body.get("top_k", 10)
        results = search_knowledge_index(query, top_k)
        return {"query": query, "count": len(results), "results": results}

    @app.post("/knowledge/upload")
    async def knowledge_upload(file: UploadFile = File(...), folder: str = Form("")):
        from services.shared.config import get_config
        cfg = get_config()
        kb_dir = cfg.get("paths", {}).get("knowledge_dir", "./data/knowledge")
        target_dir = os.path.normpath(os.path.join(kb_dir, folder.lstrip("/\\"))) if folder else kb_dir
        if not target_dir.startswith(os.path.normpath(kb_dir)):
            return {"status": "error", "message": "非法路径"}
        os.makedirs(target_dir, exist_ok=True)
        fp = os.path.join(target_dir, file.filename)
        content = await file.read()
        with open(fp, "wb") as f:
            f.write(content)
        return {"status": "ok", "filename": file.filename, "size": len(content), "folder": folder}

    @app.post("/knowledge/upload-multi")
    async def knowledge_upload_multi(files: list[UploadFile] = File(...), folder: str = Form("")):
        from services.shared.config import get_config
        cfg = get_config()
        kb_dir = cfg.get("paths", {}).get("knowledge_dir", "./data/knowledge")
        target_dir = os.path.normpath(os.path.join(kb_dir, folder.lstrip("/\\"))) if folder else kb_dir
        if not target_dir.startswith(os.path.normpath(kb_dir)):
            return {"status": "error", "message": "非法路径"}
        os.makedirs(target_dir, exist_ok=True)
        uploaded = []
        for file in files:
            try:
                content = await file.read()
                fp = os.path.join(target_dir, file.filename)
                with open(fp, "wb") as f:
                    f.write(content)
                uploaded.append({"filename": file.filename, "size": len(content), "status": "ok"})
            except Exception as e:
                uploaded.append({"filename": file.filename, "status": "error", "error": str(e)[:100]})
        return {"status": "ok", "uploaded": uploaded, "count": len(uploaded)}

    @app.post("/knowledge/folder/create")
    async def knowledge_folder_create(request: Request):
        from services.shared.config import get_config
        cfg = get_config()
        kb_dir = cfg.get("paths", {}).get("knowledge_dir", "./data/knowledge")
        body = await request.json()
        folder_name = body.get("folder", "").strip().strip("/\\")
        if not folder_name:
            return {"status": "error", "message": "文件夹名不能为空"}
        target = os.path.normpath(os.path.join(kb_dir, folder_name))
        if not target.startswith(os.path.normpath(kb_dir)):
            return {"status": "error", "message": "非法路径"}
        os.makedirs(target, exist_ok=True)
        return {"status": "ok", "folder": folder_name}

    @app.post("/knowledge/folder/delete")
    async def knowledge_folder_delete(request: Request):
        import shutil
        from services.shared.config import get_config
        cfg = get_config()
        kb_dir = cfg.get("paths", {}).get("knowledge_dir", "./data/knowledge")
        body = await request.json()
        folder_path = body.get("path", "")
        normalized = os.path.normpath(folder_path)
        if not normalized.startswith(os.path.normpath(kb_dir)):
            return {"status": "error", "message": "非法路径"}
        if normalized == os.path.normpath(kb_dir):
            return {"status": "error", "message": "不能删除根目录"}
        try:
            shutil.rmtree(normalized)
            return {"status": "ok", "message": f"已删除文件夹 {os.path.basename(normalized)}"}
        except Exception as e:
            return {"status": "error", "message": str(e)[:100]}

    @app.post("/knowledge/file/move")
    async def knowledge_file_move(request: Request):
        from services.shared.config import get_config
        cfg = get_config()
        kb_dir = cfg.get("paths", {}).get("knowledge_dir", "./data/knowledge")
        body = await request.json()
        src = body.get("src", "")
        dest_folder = body.get("dest_folder", "")
        src_norm = os.path.normpath(src)
        if not src_norm.startswith(os.path.normpath(kb_dir)):
            return {"status": "error", "message": "非法源路径"}
        dest_dir = os.path.normpath(os.path.join(kb_dir, dest_folder.lstrip("/\\"))) if dest_folder else kb_dir
        if not dest_dir.startswith(os.path.normpath(kb_dir)):
            return {"status": "error", "message": "非法目标路径"}
        try:
            os.makedirs(dest_dir, exist_ok=True)
            dest_path = os.path.join(dest_dir, os.path.basename(src_norm))
            os.rename(src_norm, dest_path)
            return {"status": "ok", "message": f"已移动到 {dest_folder or '根目录'}"}
        except Exception as e:
            return {"status": "error", "message": str(e)[:100]}

    @app.post("/knowledge/file/delete")
    async def knowledge_file_delete(request: Request):
        from services.shared.config import get_config
        cfg = get_config()
        kb_dir = cfg.get("paths", {}).get("knowledge_dir", "./data/knowledge")
        body = await request.json()
        file_path = body.get("path", "")
        normalized = os.path.normpath(file_path)
        if not normalized.startswith(os.path.normpath(kb_dir)):
            return {"status": "error", "message": "非法路径"}
        try:
            os.remove(normalized)
            return {"status": "ok", "message": f"已删除 {os.path.basename(normalized)}"}
        except FileNotFoundError:
            return {"status": "not_found", "message": "文件不存在"}
        except Exception as e:
            return {"status": "error", "message": str(e)[:100]}
