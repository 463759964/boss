"""Score module - AI-powered job-resume matching with retry, auto-confirm, and auto-pipeline."""

import json
import re
import time
import random
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from bosshunter.ai.credentials import AIRequestError
from bosshunter.db import get_db, get_jobs_by_status, update_job_status, update_job_score

console = Console()

SCORE_PROMPT = """你是一位资深技术招聘顾问。请根据以下简历和岗位描述，评估匹配程度。

## 简历摘要
{resume_summary}

## 岗位信息
- 职位：{title}
- 公司：{company}
- 薪资：{salary}
- 岗位描述：{jd}

## 评分维度
1. 技术栈匹配度（40%）
2. 经验年限匹配（20%）
3. 业务领域相关性（20%）
4. 薪资匹配度（10%）
5. 发展空间（10%）

## ⚠️ 输出格式要求（严格遵守）
- 禁止输出任何分析过程、解释文字或Markdown标记
- 禁止使用 ```json 代码块包裹
- 仅输出一个合法JSON对象，不要有任何前缀或后缀
- 格式：{{"score": 75, "reason": "50字内简述"}}
"""

# ─── 配置常量 ───────────────────────────────────────────────
MAX_SCORE_RETRIES = 5
DEFAULT_SCORE_ON_FAILURE = 100
RETRY_DELAY_SECONDS = 2
GLOBAL_REQUEST_INTERVAL = 3.0  # 🆕 全局请求间隔(秒)，防止触碰RPM上限


def _get_resume_summary(config: dict) -> str:
    resume_path = Path(config.get("profile", {}).get("resume_path", "./resume.md"))
    if not resume_path.exists():
        return ""
    return resume_path.read_text(encoding="utf-8")[:2000]


def _call_ai(prompt: str, config: dict, max_tokens: int = 500, attempt: int = 1) -> str | None:
    """根据 config.ai 配置调用 OpenAI 兼容模型（含429指数退避）"""
    ai_cfg = config.get("ai", {})
    provider = ai_cfg.get("provider", "openai_compatible")

    if provider != "openai_compatible":
        console.print(f"[red]不支持的 AI provider: {provider}[/red]")
        return None

    base_url = ai_cfg.get("base_url", "").rstrip("/")
    api_key = ai_cfg.get("api_key", "")
    model = ai_cfg.get("model", "")

    masked_key = f"{api_key[:8]}..." if len(api_key) > 8 else "(empty)"
    console.print(f"[dim]🔧 [Score] AI配置: model={model}, base_url={base_url}, key={masked_key}[/dim]")

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
        "temperature": 0.1,  # 🆕 低温度提高格式遵循度
    }

    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=60)

        console.print(f"[dim]📡 [Score] HTTP {resp.status_code} | 响应长度: {len(resp.text)}[/dim]")

        # 🆕 429 智能退避
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                wait_time = float(retry_after) + random.uniform(0.5, 1.5)
            else:
                wait_time = min(60, 3 * (2 ** (attempt - 1))) + random.uniform(0, 2)
            console.print(
                f"[yellow]⏳ [Score] 触发RPM限流，等待 {wait_time:.1f}s "
                f"(第{attempt}/{MAX_SCORE_RETRIES}次)[/yellow]"
            )
            time.sleep(wait_time)
            raise AIRequestError(kind="token_quota", message=f"HTTP 429: waited {wait_time:.1f}s")

        if resp.status_code != 200:
            console.print(f"[red]❌ [Score] API错误响应: {resp.text[:500]}[/red]")

        resp.raise_for_status()
        data = resp.json()

        choices = data.get("choices", [])
        if not choices:
            console.print(f"[red]❌ [Score] 响应中无 choices 字段: {str(data)[:300]}[/red]")
            return None

        msg = choices[0].get("message", {})
        content = msg.get("content") or msg.get("reasoning_content") or msg.get("text") or ""

        if not content:
            console.print(f"[yellow]⚠ [Score] AI返回空内容，完整message: {msg}[/yellow]")
            return None

        console.print(f"[dim]✅ [Score] AI返回成功，内容长度: {len(content)}[/dim]")
        return content.strip()

    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        body = exc.response.text[:500]
        console.print(f"[red]❌ [Score] HTTP {status}: {body}[/red]")
        if status in (401, 403):
            kind = "auth"
        elif status == 429:
            kind = "token_quota"
        elif status == 400 and "context" in body.lower():
            kind = "context_limit"
        else:
            kind = "unknown"
        raise AIRequestError(kind=kind, message=f"HTTP {status}: {body}") from exc
    except AIRequestError:
        raise  # 429 已在上面处理过，直接透传
    except Exception as exc:
        console.print(f"[red]❌ [Score] 请求异常: {type(exc).__name__}: {exc}[/red]")
        raise AIRequestError(kind="network", message=str(exc)) from exc


def _call_score_ai(prompt: str, config: dict, attempt: int = 1) -> dict | None:
    """Call AI for scoring, return parsed dict or None."""
    response = _call_ai(prompt, config, max_tokens=500, attempt=attempt)
    if not response:
        return None

    console.print(f"[dim]📝 [Score] 原始响应预览: {response[:300]}[/dim]")

    cleaned = response.strip()

    # 1. 去除 Markdown 代码块
    if cleaned.startswith("```"):
        first_nl = cleaned.index("\n") if "\n" in cleaned else 3
        cleaned = cleaned[first_nl + 1:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    # 2. 尝试直接解析
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, TypeError):
        pass

    # 3. 花括号截取
    start = cleaned.find("{")
    end = cleaned.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            result = json.loads(cleaned[start:end])
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, TypeError):
            pass

    # 4. 正则兜底
    score_match = re.search(r'(?:score|分数|匹配度)[^\d]*(\d{1,3})', cleaned)
    reason_match = re.search(r'(?:reason|原因|总结)[：:]\s*(.{10,80})', cleaned)
    if score_match:
        fallback_score = max(0, min(100, int(score_match.group(1))))
        fallback_reason = reason_match.group(1).strip() if reason_match else "AI未返回标准JSON，从文本中提取"
        console.print(f"[yellow]⚠ [Score] 正则兜底提取: score={fallback_score}[/yellow]")
        return {"score": fallback_score, "reason": fallback_reason}

    console.print(f"[red]❌ [Score] 所有解析方式均失败 | 内容: {cleaned[:200]}[/red]")
    return None


def _score_single_job(job: dict, resume_summary: str, config: dict) -> tuple[int, str]:
    prompt = SCORE_PROMPT.format(
        resume_summary=resume_summary,
        title=job["title"],
        company=job["company"],
        salary=job.get("salary") or "面议",
        jd=(job.get("jd") or "")[:800],
    )

    last_error: Exception | None = None

    for attempt in range(1, MAX_SCORE_RETRIES + 1):
        console.print(f"[dim]🔄 [Score] {job['company']}｜{job['title']} 第 {attempt}/{MAX_SCORE_RETRIES} 次[/dim]")
        try:
            result = _call_score_ai(prompt, config, attempt=attempt)
            if result and isinstance(result.get("score"), (int, float)):
                score = max(0, min(100, int(result["score"])))
                reason = str(result.get("reason", ""))[:200]
                return score, reason
            last_error = ValueError(f"AI返回格式异常: {result}")
        except AIRequestError as exc:
            last_error = exc
            if exc.kind == "auth":
                console.print(f"[red]  AI 凭证问题，停止重试[/red]")
                break
            # token_quota 已在 _call_ai 内部等待过，继续下一次重试
        except Exception as exc:
            last_error = exc

        if attempt < MAX_SCORE_RETRIES:
            console.print(f"[yellow]  评分第 {attempt} 次失败，重试中...[/yellow]")

    console.print(
        f"[red]  {job['company']}｜{job['title']} 评分失败，使用默认分数 {DEFAULT_SCORE_ON_FAILURE}[/red]"
    )
    reason = f"AI评分失败（{type(last_error).__name__}），使用默认分数"
    return DEFAULT_SCORE_ON_FAILURE, reason


def score_jobs(config: dict) -> tuple[int, int]:
    """Score pending jobs, then auto-generate greetings. Returns (approved_count, filtered_count)."""
    db = get_db()
    jobs = get_jobs_by_status(db, "pending")

    if not jobs:
        console.print("[yellow]没有待评分的岗位。请先运行 `bosshunter crawl`。[/yellow]")
        db.close()
        return 0, 0

    resume_summary = _get_resume_summary(config)
    if not resume_summary:
        console.print("[red]无法读取简历，请检查 profile.resume_path 配置[/red]")
        db.close()
        return 0, 0

    ai_cfg = config.get("ai", {})
    try:
        threshold = float(ai_cfg.get("score_threshold", 60))
    except (TypeError, ValueError):
        threshold = 60.0

    approved_count = 0
    filtered_count = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(f"AI 评分 (0/{len(jobs)})", total=len(jobs))

        for index, job in enumerate(jobs, start=1):
            progress.update(
                task,
                description=f"评分: {job['company'][:10]} - {job['title'][:15]} ({index}/{len(jobs)})",
            )

            score, reason = _score_single_job(job, resume_summary, config)
            update_job_score(db, job["id"], score, reason)

            if score >= threshold:
                update_job_status(db, job["id"], "approved")
                console.print(
                    f"  [green]✓ {score}分 → approved[/green]  "
                    f"{job['company']}｜{job['title']}"
                )
                approved_count += 1
            else:
                update_job_status(db, job["id"], "filtered")
                console.print(
                    f"  [dim]✗ {score}分 → filtered[/dim]  "
                    f"{job['company']}｜{job['title']}"
                )
                filtered_count += 1

            progress.update(task, advance=1)

            # 🆕 全局速率节流
            if index < len(jobs):
                jitter = random.uniform(0, 1.0)
                time.sleep(GLOBAL_REQUEST_INTERVAL + jitter)

    db.close()
    console.print(f"\n[green]✓ 评分完成: {approved_count} approved / {filtered_count} filtered[/green]")

    # ═══════════════════════════════════════════════════════════
    # 自动衔接：评分 → 招呼语 → 发送
    if approved_count > 0:
        console.print("\n[bold cyan]━━━ 自动进入招呼语生成 ━━━[/bold cyan]\n")
        try:
            from bosshunter.ai.greeter import generate_greetings
            generate_greetings(config)
        except Exception as exc:
            console.print(f"[red]招呼语生成阶段异常: {exc}[/red]")
            console.print("[yellow]可手动执行: bosshunter greet[/yellow]")
            return approved_count, filtered_count

        # ── 自动发送 ──────────────────────────────────────────
        console.print("\n[bold cyan]━━━ 自动进入发送 ━━━[/bold cyan]\n")
        try:
            from bosshunter.executor.sender import send_greetings
            send_greetings(config, force=True)
        except Exception as exc:
            console.print(f"[red]发送阶段异常: {exc}[/red]")
            console.print("[yellow]可手动执行: bosshunter send[/yellow]")
    else:
        console.print("[yellow]没有通过评分的岗位，跳过。[/yellow]")

    return approved_count, filtered_count