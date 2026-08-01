"""AI Greeter - Generate personalized greeting messages with self-review and fallback."""

import json
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
MAX_GENERATE_RETRIES = 3       # 单次生成最大重试
MAX_REVIEW_RETRIES = 2         # 自评最大重试（失败不阻塞）
GLOBAL_REQUEST_INTERVAL = 3.0  # 🆕 全局请求间隔(秒)，防止触碰RPM上限

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
请直接输出招呼语文本，不要加任何标记或解释。
"""

REVIEW_PROMPT = """请评估以下BOSS直聘招呼语的质量。

## 岗位
{title} @ {company}

## 招呼语
{greeting}

## 评估维度（每项1-10分）
1. 自然度：是否像真人发的IM消息，而非模板
2. 相关性：是否针对该岗位突出匹配点
3. 差异化：是否能从众多招呼中脱颖而出

请严格按JSON格式输出，不要输出其他内容：
{{"naturalness": 8, "relevance": 7, "differentiation": 6, "avg": 7.0, "critique": "改进建议（20字内）"}}
"""


def _get_resume_summary(config: dict) -> str:
    resume_path = Path(config.get("profile", {}).get("resume_path", "./resume.md"))
    if not resume_path.exists():
        return ""
    content = resume_path.read_text(encoding="utf-8")
    return content[:1500]


def _call_ai(prompt: str, config: dict, max_tokens: int = 300, attempt: int = 1) -> str | None:
    """根据 config.ai 配置调用 OpenAI 兼容模型（含429指数退避）"""
    ai_cfg = config.get("ai", {})
    provider = ai_cfg.get("provider", "openai_compatible")

    if provider != "openai_compatible":
        console.print(f"[red]不支持的 AI provider: {provider}[/red]")
        return None

    base_url = ai_cfg.get("base_url", "").rstrip("/")
    api_key = ai_cfg.get("api_key", "")
    model = ai_cfg.get("model", "")

    # ─── 调试：打印实际使用的配置 ───────────────────────
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
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }

    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=60)

        # ─── 调试：打印响应状态码和原始内容 ───────────────
        console.print(f"[dim]📡 HTTP {resp.status_code} | 响应长度: {len(resp.text)}[/dim]")
        if resp.status_code != 200:
            console.print(f"[red]❌ API错误响应: {resp.text[:500]}[/red]")

        # 🆕 429 智能退避：不再立即抛异常，而是原地等待后重试
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                wait_time = float(retry_after) + random.uniform(0.5, 1.5)
            else:
                wait_time = min(60, 3 * (2 ** (attempt - 1))) + random.uniform(0, 2)

            console.print(
                f"[yellow]⏳ 触发RPM限流，等待 {wait_time:.1f}s 后重试 "
                f"(第{attempt}/{MAX_GENERATE_RETRIES}次)[/yellow]"
            )
            time.sleep(wait_time)
            raise AIRequestError(kind="token_quota", message=f"HTTP 429: RPM exhausted, waited {wait_time:.1f}s")

        resp.raise_for_status()
        data = resp.json()

        # ─── 调试：打印解析后的消息结构 ───────────────────
        choices = data.get("choices", [])
        if not choices:
            console.print(f"[red]❌ 响应中无 choices 字段: {str(data)[:300]}[/red]")
            return None

        msg = choices[0].get("message", {})
        content = msg.get("content") or msg.get("reasoning_content") or msg.get("text") or ""

        if not content:
            console.print(f"[yellow]⚠ AI返回空内容，完整message: {msg}[/yellow]")
            return None

        console.print(f"[dim]✅ AI返回成功，内容长度: {len(content)}[/dim]")
        return content.strip()

    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        body = exc.response.text[:500]
        console.print(f"[red]❌ HTTP {status}: {body}[/red]")
        if status in (401, 403):
            kind = "auth"
        elif status == 429:
            kind = "token_quota"
        elif status == 400 and "context" in body.lower():
            kind = "context_limit"
        else:
            kind = "unknown"
        raise AIRequestError(kind=kind, message=f"HTTP {status}: {body}") from exc
    except Exception as exc:
        # 避免重复打印已在上面处理过的429日志
        if isinstance(exc, AIRequestError) and exc.kind == "token_quota":
            raise
        console.print(f"[red]❌ 请求异常: {type(exc).__name__}: {exc}[/red]")
        raise AIRequestError(kind="network", message=str(exc)) from exc


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
    response = _call_ai(prompt, config, max_tokens, attempt=attempt)
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

    greeting = _call_ai(prompt, config, max_tokens, attempt=attempt)
    if not greeting:
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
            if exc.kind in ("token_quota", "auth"):
                # 🆕 429 已在 _call_ai 内部等待过，这里直接继续下一次重试
                if exc.kind == "auth":
                    console.print(f"[red]  AI 凭证问题（{exc.kind}），停止重试[/red]")
                    break
                # token_quota 不break，让循环继续（_call_ai已经sleep过了）
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
            if exc.kind == "auth":
                break
            # 429 已在 _call_ai 内部等待过，继续重试
        except Exception:
            pass
    return None


def generate_greetings(config: dict) -> int:
    """
    Generate greetings for approved jobs.
    - AI 成功 → 使用 AI 招呼语
    - AI 全部失败 → 使用默认招呼语 "您好"
    - 无论哪种，状态都设为 "ready"（待发送）
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

            # ─── 有简历才尝试 AI 生成 ───────────────────────
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
                        break  # 重试耗尽，跳出迭代

                    best_greeting = greeting

                    if max_iterations == 0:
                        break

            # ─── 兜底：AI 全部失败 → 默认招呼语 ─────────────
            if not best_greeting:
                best_greeting = DEFAULT_GREETING
                fallback_count += 1
                console.print(
                    f"  [yellow]⚠ {job['company']}｜{job['title']} "
                    f"AI生成失败，使用默认招呼语「{DEFAULT_GREETING}」[/yellow]"
                )

            # ─── 写入数据库，状态 → ready（待发送）───────────
            update_job_greeting(db, job["id"], best_greeting)
            update_job_status(db, job["id"], "ready")
            count += 1
            progress.update(task, advance=1)

            # 🆕 全局速率节流：每个岗位处理完后主动等待，避免连续请求触发RPM
            if index < len(jobs):
                jitter = random.uniform(0, 1.0)
                sleep_time = GLOBAL_REQUEST_INTERVAL + jitter
                console.print(f"[dim]💤 全局节流: 等待 {sleep_time:.1f}s 后处理下一个岗位[/dim]")
                time.sleep(sleep_time)

    db.close()

    console.print(f"\n[green]✓ 招呼语生成完成：{count} 个岗位[/green]")
    if fallback_count:
        console.print(f"[yellow]  其中 {fallback_count} 个使用默认招呼语「{DEFAULT_GREETING}」[/yellow]")
    console.print("[dim]  下一步：运行 bosshunter send 发送[/dim]")

    return count