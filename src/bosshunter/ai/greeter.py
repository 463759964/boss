"""AI Greeter - Generate personalized greeting messages with self-review and fallback."""

import json
import re
import time
import random
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from bosshunter.ai.credentials import AIRequestError
from bosshunter.db import get_db, get_jobs_by_status, update_job_greeting, update_job_status

console = Console()

# ─── 兜底与限流配置 ───────────────────────────────────────────────
DEFAULT_GREETING = "您好"
MAX_GENERATE_RETRIES = 3
MAX_REVIEW_RETRIES = 2
# 优化: 全局节流从 3.0s 提升至 6.0s，适配 SenseNova RPM 限制，从源头减少 429 触发率
GLOBAL_REQUEST_INTERVAL = 6.0

# ─── Prompt 模板 ─────────────────────────────────────────────────
# 优化: System Prompt 精简为纯指令，确保每次请求完全一致以最大化 Prompt Cache 命中率
_SYSTEM_PROMPT = "你是一个文本生成器。只输出最终文本本身。严禁输出思考过程、任务复述、分析步骤、计划说明或任何元描述。"

# 优化: 招呼语生成改为结构化 JSON 输出，彻底消除后处理清理逻辑和无效 token
GREETING_PROMPT = """你是一位求职者，需要在BOSS直聘上给HR发送打招呼消息。请根据以下信息生成一条个性化、自然的招呼语。

## 我的背景
{resume_summary}

## 目标岗位
- 职位：{title}
- 公司：{company}
- 薪资：{salary}
- 岗位要求摘要：{jd_summary}
- 匹配分析：{match_reason}

## 额外亮点（适时融入，不要生硬罗列）
{extra_highlights}

## 要求
1. 字数控制在50-150字
2. 风格自然，像真人发的IM消息，不要太正式
3. 突出1-2个最匹配的优势
4. 表达对岗位的兴趣，但不要谄媚
5. 不要用"您好，我是xxx"这种模板开头，要有差异化
6. 适配手机端阅读
7. 在合适的位置自然带出作品集链接（不要每次都放，根据岗位匹配度决定）
8. 【严禁】不得捏造我没有的经历、头衔或身份，只能使用"我的背景"中明确提到的信息
9. 【严禁】不得把岗位JD中的描述（如公司头衔、项目名）当作我的经历来写
10. 严格使用"我的背景"中的原文描述，不得改写或美化
{critique_section}

【输出格式】请严格按以下JSON格式输出，不要输出任何其他内容：
{{"greeting": "这里放最终的招呼语文本"}}
"""

# 优化: Review Prompt 同样强制 JSON 输出，提升解析成功率
REVIEW_PROMPT = """请评估以下BOSS直聘招呼语的质量。

## 岗位
{title} @ {company}

## 招呼语
{greeting}

## 评估维度（每项1-10分）
1. 自然度：是否像真人发的IM消息，而非模板
2. 相关性：是否针对该岗位突出匹配点
3. 差异化：是否能从众多招呼中脱颖而出

请严格按以下JSON格式输出，不要输出其他内容：
{{"naturalness": 8, "relevance": 7, "differentiation": 6, "avg": 7.0, "critique": "改进建议（20字内）"}}
"""


# ─── 工具函数 ─────────────────────────────────────────────────────
def _get_resume_summary(config: dict) -> str:
    resume_path = Path(config.get("profile", {}).get("resume_path", "./resume.md"))
    if not resume_path.exists():
        return ""
    content = resume_path.read_text(encoding="utf-8")
    return content[:1500]


def _call_ai(
    prompt: str,
    config: dict,
    max_tokens: int = 300,
    attempt: int = 1,
    json_mode: bool = False,
) -> str | None:
    """根据 config.ai 配置调用 OpenAI 兼容模型（含429指数退避与错误分类）"""
    ai_cfg = config.get("ai", {})
    provider = ai_cfg.get("provider", "openai_compatible")

    if provider != "openai_compatible":
        console.print(f"[red]不支持的 AI provider: {provider}[/red]")
        return None

    base_url = ai_cfg.get("base_url", "").rstrip("/")
    api_key = ai_cfg.get("api_key", "")
    model = ai_cfg.get("model", "")

    masked_key = f"{api_key[:8]}..." if len(api_key) > 8 else "(empty)"
    console.print(f"[dim]🔧 AI配置: model={model}, base_url={base_url}, key={masked_key}[/dim]")

    if not all([base_url, api_key, model]):
        console.print("[red]AI 配置不完整，请检查 config.yaml 中 ai 段[/red]")
        return None

    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "reasoning_effort": "none",
    }
    # 优化: 启用 SenseNova JSON Mode，强制结构化输出
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=60)

        console.print(f"[dim]📡 HTTP {resp.status_code} | 响应长度: {len(resp.text)}[/dim]")
        if resp.status_code != 200:
            console.print(f"[red]❌ API错误响应: {resp.text[:500]}[/red]")

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                wait_time = float(retry_after) + random.uniform(0.5, 1.5)
            else:
                # 优化: RPM 退避基数从 3s 提升至 15s，避免反复撞击限流墙
                wait_time = min(60, 15 * (2 ** (attempt - 1))) + random.uniform(0, 2)

            console.print(
                f"[yellow]⏳ 触发RPM限流，等待 {wait_time:.1f}s 后重试 "
                f"(第{attempt}/{MAX_GENERATE_RETRIES}次)[/yellow]"
            )
            time.sleep(wait_time)
            raise AIRequestError(kind="rate_limit", user_message=f"HTTP 429: RPM exhausted, waited {wait_time:.1f}s")

        resp.raise_for_status()
        data = resp.json()

        choices = data.get("choices", [])
        if not choices:
            console.print(f"[red]❌ 响应中无 choices 字段: {str(data)[:300]}[/red]")
            return None

        msg = choices[0].get("message", {})
        content = msg.get("content") or ""

        if not content:
            console.print(f"[yellow]⚠ AI返回空content，完整message: {msg}[/yellow]")
            return None

        console.print(f"[dim]✅ AI返回成功，内容长度: {len(content)}[/dim]")
        return content.strip()

    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        body = exc.response.text[:500]
        console.print(f"[red]❌ HTTP {status}: {body}[/red]")

        # 优化: 细化错误分类，400/404/insufficient_quota 标记为不可重试
        err_data = {}
        try:
            err_data = exc.response.json().get("error", {})
        except Exception:
            pass
        err_code = err_data.get("code", "")

        if status in (401, 403):
            kind = "auth"
        elif status == 429 and err_code == "insufficient_quota":
            kind = "non_retryable"
        elif status == 429:
            kind = "rate_limit"
        elif status == 400 and "context" in body.lower():
            kind = "context_limit"
        elif status in (400, 404):
            kind = "non_retryable"
        else:
            kind = "unknown"
        raise AIRequestError(kind=kind, user_message=f"HTTP {status}: {body}") from exc
    except AIRequestError:
        raise
    except Exception as exc:
        console.print(f"[red]❌ 请求异常: {type(exc).__name__}: {exc}[/red]")
        raise AIRequestError(kind="network", user_message=str(exc)) from exc


def _truncate_prompt_text(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    marker = "\n...[为适配模型上下文已裁剪]...\n"
    available = max(limit - len(marker), 2)
    head = max(int(available * 0.7), 1)
    return f"{text[:head]}{marker}{text[-(available - head):]}"


def _notify(config: dict, message: str, *, error: bool = False) -> None:
    console.print(f"[{'red' if error else 'yellow'}]{message}[/{'red' if error else 'yellow'}]")
    callback = config.get("_workbench_log")
    if callable(callback):
        callback(message)


def _review_greeting(
    greeting: str,
    job: dict,
    config: dict,
    max_tokens: int = 300,
    attempt: int = 1,
) -> dict | None:
    prompt = REVIEW_PROMPT.format(
        title=job["title"],
        company=job["company"],
        greeting=greeting,
    )
    # 优化: Review 同样启用 JSON Mode
    response = _call_ai(prompt, config, max_tokens, attempt=attempt, json_mode=True)
    if not response:
        return None
    try:
        start = response.find("{")
        end = response.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(response[start:end])
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def _generate_greeting_once(
    job: dict,
    resume_summary: str,
    config: dict,
    critique: str = "",
    *,
    compact: bool = False,
    max_tokens: int = 300,
    attempt: int = 1,
) -> str | None:
    jd_limit = 250 if compact else 500
    resume_limit = 800 if compact else 1500
    jd_summary = _truncate_prompt_text(job.get("jd", ""), jd_limit) or "无详细描述"
    critique_section = f"\n7. 上次生成的问题: {critique}，请避免此问题\n" if critique else ""

    profile_cfg = config.get("profile", {})
    highlights = profile_cfg.get("extra_highlights", [])
    portfolio_url = profile_cfg.get("portfolio_url", "")
    highlight_lines = [f"- {h}" for h in highlights]
    if portfolio_url:
        highlight_lines.append(f"- 个人作品集网址：{portfolio_url}")
    extra_highlights = "\n".join(highlight_lines) if highlight_lines else "（无额外亮点配置）"

    prompt = GREETING_PROMPT.format(
        resume_summary=_truncate_prompt_text(resume_summary, resume_limit),
        title=job["title"],
        company=job["company"],
        salary=job["salary"] or "面议",
        jd_summary=jd_summary,
        match_reason=_truncate_prompt_text(job.get("score_reason", ""), 240),
        critique_section=critique_section,
        extra_highlights=_truncate_prompt_text(extra_highlights, 500),
    )

    # 优化: 启用 JSON Mode 生成招呼语
    greeting_raw = _call_ai(prompt, config, max_tokens, attempt=attempt, json_mode=True)
    if not greeting_raw:
        return None

    # 优化: 直接解析 JSON，移除原有的正则清理逻辑
    try:
        data = json.loads(greeting_raw)
        greeting = data.get("greeting", "").strip()
    except (json.JSONDecodeError, TypeError):
        console.print("[yellow]⚠ JSON解析失败，尝试提取纯文本兜底[/yellow]")
        # 兜底: 如果 JSON 解析失败，仍尝试提取内容，保证功能不中断
        start = greeting_raw.find("{")
        end = greeting_raw.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                data = json.loads(greeting_raw[start:end])
                greeting = data.get("greeting", "").strip()
            except Exception:
                greeting = ""
        else:
            greeting = ""

    if not greeting:
        console.print("[yellow]⚠ AI输出经解析后为空，视为失败[/yellow]")
        return None

    greeting = greeting.strip('"\'')
    if len(greeting) > 150:
        cut = greeting[:150]
        for sep in ("。", "！", "～", "！", "\n"):
            idx = cut.rfind(sep)
            if idx > 50:
                greeting = cut[:idx + 1]
                break
        else:
            greeting = cut

    return greeting


def _generate_with_retry(
    job: dict,
    resume_summary: str,
    config: dict,
    critique: str = "",
) -> str | None:
    """尝试生成招呼语，最多 MAX_GENERATE_RETRIES 次。全部失败返回 None。"""
    last_error: Exception | None = None

    for attempt in range(1, MAX_GENERATE_RETRIES + 1):
        try:
            result = _generate_greeting_once(job, resume_summary, config, critique, attempt=attempt)
            if result:
                return result
            last_error = ValueError("AI 返回空内容")
        except AIRequestError as exc:
            last_error = exc
            # 优化: 新增 non_retryable 类型，400/404/insufficient_quota 立即停止
            if exc.kind in ("auth", "non_retryable"):
                console.print(f"[red]  AI 请求不可重试（{exc.kind}），停止[/red]")
                break
            if exc.kind == "context_limit" and attempt < MAX_GENERATE_RETRIES:
                try:
                    result = _generate_greeting_once(
                        job, resume_summary, config, critique,
                        compact=True, max_tokens=160, attempt=attempt,
                    )
                    if result:
                        return result
                except AIRequestError:
                    pass
        except Exception as exc:
            last_error = exc

        if attempt < MAX_GENERATE_RETRIES:
            console.print(
                f"[yellow]  招呼语生成第 {attempt} 次失败，重试中...[/yellow]"
            )

    return None


def _review_with_retry(greeting: str, job: dict, config: dict) -> dict | None:
    """自评，失败不阻塞流程。"""
    for attempt in range(1, MAX_REVIEW_RETRIES + 1):
        try:
            result = _review_greeting(greeting, job, config, attempt=attempt)
            if result:
                return result
        except AIRequestError as exc:
            if exc.kind in ("auth", "non_retryable"):
                break
        except Exception:
            pass
    return None


def generate_greetings(config: dict) -> int:
    """
    Generate greetings for approved jobs.
    Returns count processed.
    """
    db = get_db()
    jobs = get_jobs_by_status(db, "approved")

    _workbench_job_ids = {str(job_id) for job_id in config.get("_workbench_job_ids", [])}
    if _workbench_job_ids:
        jobs = [job for job in jobs if str(job["id"]) in _workbench_job_ids]

    if not jobs:
        console.print("[yellow]没有已确认的岗位可生成招呼语。[/yellow]")
        db.close()
        return 0

    resume_summary = _get_resume_summary(config)
    if not resume_summary:
        console.print("[yellow]无法读取简历，将使用默认招呼语[/yellow]")

    ai_cfg = config.get("ai", {})
    review_threshold = ai_cfg.get("greeting_review_threshold", 7.0)
    try:
        max_iterations = max(0, int(ai_cfg.get("greeting_max_iterations", 4) or 0))
    except (TypeError, ValueError):
        max_iterations = 2

    count = 0
    fallback_count = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(f"生成招呼语 (0/{len(jobs)})", total=len(jobs))

        for index, job in enumerate(jobs, start=1):
            progress.update(
                task,
                description=f"招呼语: {job['company'][:10]} - {job['title'][:15]} ({index}/{len(jobs)})",
            )

            best_greeting: str | None = None

            if resume_summary:
                for iteration in range(max_iterations + 1):
                    critique = ""

                    if iteration > 0 and best_greeting:
                        review = _review_with_retry(best_greeting, job, config)
                        if review and review.get("avg", 10) >= review_threshold:
                            break
                        critique = review.get("critique", "") if review else ""

                    greeting = _generate_with_retry(job, resume_summary, config, critique)

                    if not greeting:
                        break

                    best_greeting = greeting

                    if max_iterations == 0:
                        break

            if not best_greeting:
                best_greeting = DEFAULT_GREETING
                fallback_count += 1
                console.print(
                    f"  [yellow]⚠ {job['company']}｜{job['title']} "
                    f"AI生成失败，使用默认招呼语「{DEFAULT_GREETING}」[/yellow]"
                )

            update_job_greeting(db, job["id"], best_greeting)
            update_job_status(db, job["id"], "ready")
            count += 1
            progress.update(task, advance=1)

            if index < len(jobs):
                jitter = random.uniform(0, 1.0)
                sleep_time = GLOBAL_REQUEST_INTERVAL + jitter
                console.print(f"[dim]💤 全局节流: 等待 {sleep_time:.1f}s 后处理下一个岗位[/dim]")
                time.sleep(sleep_time)

    db.close()

    console.print(f"\n[green]✓ 招呼语生成完成：{count} 个岗位[/green]")
    if fallback_count:
        console.print(f"[yellow]  其中 {fallback_count} 个使用默认招呼语「{DEFAULT_GREETING}」[/yellow]")

    return count