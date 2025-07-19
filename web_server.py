import uvicorn
import json
import aiofiles
import os
import glob
import asyncio
import sys
from dotenv import dotenv_values
from fastapi import FastAPI, Request, HTTPException
from prompt_generator import generate_criteria, update_config_with_new_task
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List, Optional


class Task(BaseModel):
    task_name: str
    enabled: bool
    keyword: str
    max_pages: int
    personal_only: bool
    min_price: Optional[str] = None
    max_price: Optional[str] = None
    ai_prompt_base_file: str
    ai_prompt_criteria_file: str


class TaskUpdate(BaseModel):
    task_name: Optional[str] = None
    enabled: Optional[bool] = None
    keyword: Optional[str] = None
    max_pages: Optional[int] = None
    personal_only: Optional[bool] = None
    min_price: Optional[str] = None
    max_price: Optional[str] = None
    ai_prompt_base_file: Optional[str] = None
    ai_prompt_criteria_file: Optional[str] = None


class TaskGenerateRequest(BaseModel):
    task_name: str
    keyword: str
    description: str
    personal_only: bool = True
    min_price: Optional[str] = None
    max_price: Optional[str] = None


class PromptUpdate(BaseModel):
    content: str


app = FastAPI(title="闲鱼智能监控机器人")

# --- Globals for process management ---
scraper_process = None

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Setup templates
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """
    提供 Web UI 的主页面。
    """
    return templates.TemplateResponse("index.html", {"request": request})

# --- API Endpoints ---

CONFIG_FILE = "config.json"

@app.get("/api/tasks")
async def get_tasks():
    """
    读取并返回 config.json 中的所有任务。
    """
    try:
        async with aiofiles.open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            content = await f.read()
            tasks = json.loads(content)
            # 为每个任务添加一个唯一的 id
            for i, task in enumerate(tasks):
                task['id'] = i
            return tasks
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"配置文件 {CONFIG_FILE} 未找到。")
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail=f"配置文件 {CONFIG_FILE} 格式错误。")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取任务配置时发生错误: {e}")


@app.post("/api/tasks/generate", response_model=dict)
async def generate_task(req: TaskGenerateRequest):
    """
    使用 AI 生成一个新的分析标准文件，并据此创建一个新任务。
    """
    print(f"收到 AI 任务生成请求: {req.task_name}")
    
    # 1. 为新标准文件生成一个唯一的文件名
    safe_keyword = "".join(c for c in req.keyword.lower().replace(' ', '_') if c.isalnum() or c in "_-").rstrip()
    output_filename = f"prompts/{safe_keyword}_criteria.txt"
    
    # 2. 调用 AI 生成分析标准
    try:
        generated_criteria = await generate_criteria(
            user_description=req.description,
            reference_file_path="prompts/macbook_criteria.txt" # 使用默认的macbook标准作为参考
        )
        if not generated_criteria:
            raise HTTPException(status_code=500, detail="AI未能生成分析标准。")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"调用AI生成标准时出错: {e}")

    # 3. 将生成的文本保存到新文件
    try:
        os.makedirs("prompts", exist_ok=True)
        async with aiofiles.open(output_filename, 'w', encoding='utf-8') as f:
            await f.write(generated_criteria)
        print(f"新的分析标准已保存到: {output_filename}")
    except IOError as e:
        raise HTTPException(status_code=500, detail=f"保存分析标准文件失败: {e}")

    # 4. 创建新任务对象
    new_task = {
        "task_name": req.task_name,
        "enabled": True,
        "keyword": req.keyword,
        "max_pages": 3, # 默认值
        "personal_only": req.personal_only,
        "min_price": req.min_price,
        "max_price": req.max_price,
        "ai_prompt_base_file": "prompts/base_prompt.txt",
        "ai_prompt_criteria_file": output_filename
    }

    # 5. 将新任务添加到 config.json
    success = await update_config_with_new_task(new_task, CONFIG_FILE)
    if not success:
        # 如果更新失败，最好能把刚刚创建的文件删掉，以保持一致性
        if os.path.exists(output_filename):
            os.remove(output_filename)
        raise HTTPException(status_code=500, detail="更新配置文件 config.json 失败。")

    # 6. 返回成功创建的任务（包含ID）
    async with aiofiles.open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        tasks = json.loads(await f.read())
    new_task_with_id = new_task.copy()
    new_task_with_id['id'] = len(tasks) - 1

    return {"message": "AI 任务创建成功。", "task": new_task_with_id}


@app.post("/api/tasks", response_model=dict)
async def create_task(task: Task):
    """
    创建一个新任务并将其添加到 config.json。
    """
    try:
        async with aiofiles.open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            tasks = json.loads(await f.read())
    except (FileNotFoundError, json.JSONDecodeError):
        tasks = []

    new_task_data = task.dict()
    tasks.append(new_task_data)

    try:
        async with aiofiles.open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(tasks, ensure_ascii=False, indent=2))
        
        new_task_data['id'] = len(tasks) - 1
        return {"message": "任务创建成功。", "task": new_task_data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入配置文件时发生错误: {e}")


@app.patch("/api/tasks/{task_id}", response_model=dict)
async def update_task(task_id: int, task_update: TaskUpdate):
    """
    更新指定ID任务的属性。
    """
    try:
        async with aiofiles.open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            tasks = json.loads(await f.read())
    except (FileNotFoundError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=500, detail=f"读取或解析配置文件失败: {e}")

    if not (0 <= task_id < len(tasks)):
        raise HTTPException(status_code=404, detail="任务未找到。")

    # 更新数据
    task_changed = False
    update_data = task_update.dict(exclude_unset=True)
    
    if update_data:
        original_task = tasks[task_id].copy()
        tasks[task_id].update(update_data)
        if tasks[task_id] != original_task:
            task_changed = True

    if not task_changed:
        return JSONResponse(content={"message": "数据无变化，未执行更新。"}, status_code=200)

    # 异步写回文件
    try:
        async with aiofiles.open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(tasks, ensure_ascii=False, indent=2))
        
        updated_task = tasks[task_id]
        updated_task['id'] = task_id
        return {"message": "任务更新成功。", "task": updated_task}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入配置文件时发生错误: {e}")


@app.post("/api/tasks/start-all", response_model=dict)
async def start_all_tasks():
    """
    启动所有在 config.json 中启用的任务。
    """
    global scraper_process
    if scraper_process and scraper_process.returncode is None:
        raise HTTPException(status_code=400, detail="监控任务已在运行中。")

    try:
        # 设置日志目录和文件
        os.makedirs("logs", exist_ok=True)
        log_file_path = os.path.join("logs", "scraper.log")
        
        # 以追加模式打开日志文件，如果不存在则创建。
        # 子进程将继承这个文件句柄。
        log_file_handle = open(log_file_path, 'a', encoding='utf-8')

        # 使用与Web服务器相同的Python解释器来运行爬虫脚本
        # 增加 -u 参数来禁用I/O缓冲，确保日志实时写入文件
        scraper_process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "spider_v2.py",
            stdout=log_file_handle,
            stderr=log_file_handle
        )
        print(f"启动爬虫进程，PID: {scraper_process.pid}，日志输出到 {log_file_path}")
        return {"message": "所有启用任务已启动。"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"启动爬虫进程时出错: {e}")


@app.post("/api/tasks/stop-all", response_model=dict)
async def stop_all_tasks():
    """
    停止当前正在运行的监控任务。
    """
    global scraper_process
    if not scraper_process or scraper_process.returncode is not None:
        raise HTTPException(status_code=400, detail="没有正在运行的监控任务。")

    try:
        scraper_process.terminate()
        await scraper_process.wait()
        print(f"爬虫进程 {scraper_process.pid} 已终止。")
        scraper_process = None
        return {"message": "所有任务已停止。"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"停止爬虫进程时出错: {e}")


@app.get("/api/logs")
async def get_logs():
    """
    获取爬虫日志文件的内容。
    """
    log_file_path = os.path.join("logs", "scraper.log")
    if not os.path.exists(log_file_path):
        return JSONResponse(content={"content":"日志文件不存在或尚未创建。"},status_code = 200)
    try:
        # 先尝试用 utf-8 读取
        try:
            async with aiofiles.open(log_file_path, 'r', encoding = 'utf-8') as f:
                content = await f.read()
        except UnicodeDecodeError:
            # 如果 utf-8 失败，尝试用 gbk 读取
            async with aiofiles.open(log_file_path,'r', encoding = 'gbk') as f:
                content = await f.read()
        return {"content": content}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取日志文件时出错: {e}")


@app.delete("/api/tasks/{task_id}", response_model=dict)
async def delete_task(task_id: int):
    """
    从 config.json 中删除指定ID的任务。
    """
    try:
        async with aiofiles.open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            tasks = json.loads(await f.read())
    except (FileNotFoundError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=500, detail=f"读取或解析配置文件失败: {e}")

    if not (0 <= task_id < len(tasks)):
        raise HTTPException(status_code=404, detail="任务未找到。")

    deleted_task = tasks.pop(task_id)

    try:
        async with aiofiles.open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(tasks, ensure_ascii=False, indent=2))
        
        return {"message": "任务删除成功。", "task_name": deleted_task.get("task_name")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入配置文件时发生错误: {e}")


@app.get("/api/results/files")
async def list_result_files():
    """
    列出所有生成的 .jsonl 结果文件。
    """
    files = glob.glob("*.jsonl")
    return {"files": files}


@app.get("/api/results/{filename}")
async def get_result_file_content(filename: str, page: int = 1, limit: int = 20, recommended_only: bool = False):
    """
    读取指定的 .jsonl 文件内容，支持分页和筛选。
    """
    if not filename.endswith(".jsonl") or "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="无效的文件名。")
    
    if not os.path.exists(filename):
        raise HTTPException(status_code=404, detail="结果文件未找到。")

    results = []
    try:
        async with aiofiles.open(filename, 'r', encoding='utf-8') as f:
            async for line in f:
                try:
                    record = json.loads(line)
                    if recommended_only:
                        if record.get("ai_analysis", {}).get("is_recommended") is True:
                            results.append(record)
                    else:
                        results.append(record)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取结果文件时出错: {e}")

    results.reverse()
    
    total_items = len(results)
    start = (page - 1) * limit
    end = start + limit
    paginated_results = results[start:end]

    return {
        "total_items": total_items,
        "page": page,
        "limit": limit,
        "items": paginated_results
    }


@app.get("/api/settings/status")
async def get_system_status():
    """
    检查系统关键文件和配置的状态。
    """
    global scraper_process
    env_config = dotenv_values(".env")

    # 检查进程是否仍在运行
    is_running = False
    if scraper_process:
        if scraper_process.returncode is None:
            is_running = True
        else:
            # 进程已结束，重置
            print(f"检测到爬虫进程 {scraper_process.pid} 已结束，返回码: {scraper_process.returncode}。")
            scraper_process = None
    
    status = {
        "scraper_running": is_running,
        "login_state_file": {
            "exists": os.path.exists("xianyu_state.json"),
            "path": "xianyu_state.json"
        },
        "env_file": {
            "exists": os.path.exists(".env"),
            "openai_api_key_set": bool(env_config.get("OPENAI_API_KEY")),
            "openai_base_url_set": bool(env_config.get("OPENAI_BASE_URL")),
            "openai_model_name_set": bool(env_config.get("OPENAI_MODEL_NAME")),
            "ntfy_topic_url_set": bool(env_config.get("NTFY_TOPIC_URL")),
        }
    }
    return status


PROMPTS_DIR = "prompts"

@app.get("/api/prompts")
async def list_prompts():
    """
    列出 prompts/ 目录下的所有 .txt 文件。
    """
    if not os.path.isdir(PROMPTS_DIR):
        return []
    return [f for f in os.listdir(PROMPTS_DIR) if f.endswith(".txt")]


@app.get("/api/prompts/{filename}")
async def get_prompt_content(filename: str):
    """
    获取指定 prompt 文件的内容。
    """
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="无效的文件名。")
    
    filepath = os.path.join(PROMPTS_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Prompt 文件未找到。")
    
    async with aiofiles.open(filepath, 'r', encoding='utf-8') as f:
        content = await f.read()
    return {"filename": filename, "content": content}


@app.put("/api/prompts/{filename}")
async def update_prompt_content(filename: str, prompt_update: PromptUpdate):
    """
    更新指定 prompt 文件的内容。
    """
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="无效的文件名。")

    filepath = os.path.join(PROMPTS_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Prompt 文件未找到。")

    try:
        async with aiofiles.open(filepath, 'w', encoding='utf-8') as f:
            await f.write(prompt_update.content)
        return {"message": f"Prompt 文件 '{filename}' 更新成功。"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入 Prompt 文件时出错: {e}")


@app.on_event("shutdown")
async def shutdown_event():
    """
    应用退出时，确保终止所有子进程。
    """
    global scraper_process
    if scraper_process and scraper_process.returncode is None:
        print(f"Web服务器正在关闭，正在终止爬虫进程 {scraper_process.pid}...")
        scraper_process.terminate()
        try:
            await asyncio.wait_for(scraper_process.wait(), timeout=5.0)
            print("爬虫进程已成功终止。")
        except asyncio.TimeoutError:
            print("等待爬虫进程终止超时，将强制终止。")
            scraper_process.kill()
        scraper_process = None


if __name__ == "__main__":
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    print("启动 Web 管理界面，请在浏览器访问 http://127.0.0.1:8000")

    uvicorn.run(app, host="0.0.0.0", port=8000)
