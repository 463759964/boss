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
from bosshunter.ai.prefilter import quick_score
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
- 必须输出合法的 JSON 对象
- 格式：{{"score": 75, "reason": "50字内简述"}}
- 禁止输出任何分析过程、解释文字或 Markdown 标记
"""

# ─── 配置常量 ───────────────────────────────────────────────
MAX_SCORE_RETRIES = 3
DEFAULT_SCORE_ON_FAILURE = 100
RETRY_DELAY_SECONDS = 2
GLOBAL_REQUEST_INTERVAL = 3.0   # 全局请求间隔(秒)，防止触碰RPM上限
MAX_RETRY_WAIT = 10.0           # 429 限流最大等待秒数


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

    # 🆕 针对 DeepSeek V4 Flash 优化的 payload
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.1,
        "reasoning_effort": "none",                  # 关闭思考模式，节省 token 和时间
        "response_format": {"type": "json_object"}   # 启用原生 JSON 输出，杜绝格式问题
    }

    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=60)

        console.print(f"[dim]📡 [Score] HTTP {resp.status_code} | 响应长度: {len(resp.text)}[/dim]")

        # 429 智能退避（带上限）
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                wait_time = min(float(retry_after) + random.uniform(0.5, 1.5), MAX_RETRY_WAIT)
            else:
                wait_time = min(
                    3 * (2 ** (attempt - 1)) + random.uniform(0, 2),
                    MAX_RETRY_WAIT
                )
            console.print(
                f"[yellow]⏳ [Score] 触发RPM/TPM限流，等待 {wait_time:.1f}s "
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

    # 1. 去除 Markdown 代码块（虽然启用了 json_object，但保留作为安全网）
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

    # 3.5 从尾部向前找最后一个完整 JSON 对象（跳过思考文本）
    last_brace = cleaned.rfind("}")
    if last_brace > 0:
        depth = 0
        for i in range(last_brace, -1, -1):
            if cleaned[i] == "}":
                depth += 1
            elif cleaned[i] == "{":
                depth -= 1
            if depth == 0:
                try:
                    result = json.loads(cleaned[i:last_brace + 1])
                    if isinstance(result, dict) and "score" in result:
                        return result
                except (json.JSONDecodeError, TypeError):
                    pass
                break

    # 4. 正则兜底
    score_match = re.search(r'(?:score|分数|匹配度)[^\d]*(\d{1,3})', cleaned)
    reason_match = re.search(r'(?:reason|原因|总结)[：:]\s*(.{10,80})', cleaned)
    if score_match:
        fallback_score = max(0, min(100, int(score_match.group(1))))
        fallback_reason = reason_match.group(1).strip() if reason_match else "AI未返回标准JSON，从文本中提取"
        console.print(f"[yellow]⚠ [Score] 正则兜底提取: score={fallback_score}[/yellow]")
        return {"score": fallback_score, "reason": fallback_reason}

    console.print(f"[red]❌ [Score] 所有解析方式均失败，兜底100分 | 内容: {cleaned[:200]}[/red]")
    return {"score": 100, "reason": "AI响应解析失败，兜底满分"}


def _score_single_job(job: dict, resume_summary: str, config: dict) -> tuple[int, str]:
    base_prompt = SCORE_PROMPT.format(
        resume_summary=resume_summary,
        title=job["title"],
        company=job["company"],
        salary=job.get("salary") or "面议",
        jd=(job.get("jd") or "")[:800],
    )

    # 正确获取嵌套在 scoring 下的及格线阈值
    scoring_cfg = config.get("scoring", {})
    threshold = float(scoring_cfg.get("threshold", 50))

    # 用于记录第一次评分结果，以便在低分时进行二次评估
    first_score: int | None = None
    first_reason: str = ""
    last_error: Exception | None = None

    # 最多进行两轮评分：第一轮正常评分，如果低分则触发第二轮
    for eval_round in range(1, 3):
        # 二次评估使用差异化 Prompt，避免 AI 返回相同结果
        if eval_round == 2:
            prompt = base_prompt + (
                "\n\n## ⚠️ 二次评估提醒\n"
                "这是对该岗位的重新独立评估。请抛开先前可能存在的偏见，"
                "重新审视简历与岗位的匹配度，给出你的独立判断。"
            )
            console.print(f"[yellow]  ⏳ 二次评估前等待 2 秒，避免触发限流...[/yellow]")
            time.sleep(2)
        else:
            prompt = base_prompt

        last_error = None  # 每轮重置错误记录

        # 原有的重试机制保持不变
        for attempt in range(1, MAX_SCORE_RETRIES + 1):
            console.print(f"[dim]🔄 [Score] {job['company']}｜{job['title']} 第 {eval_round} 轮第 {attempt}/{MAX_SCORE_RETRIES} 次[/dim]")
            try:
                result = _call_score_ai(prompt, config, attempt=attempt)

                # 打印一下 AI 返回的原始数据，方便排查
                console.print(f"[dim]   📥 AI原始返回: {result}[/dim]")

                if result and isinstance(result.get("score"), (int, float)):
                    score = max(0, min(100, int(result["score"])))
                    reason = str(result.get("reason", ""))[:200]

                    # === 核心逻辑：判断是否需要二次评估 ===
                    if eval_round == 1 and score < threshold:
                        first_score = score
                        first_reason = reason
                        console.print(f"[yellow]  初次评分 {score} 低于阈值 {threshold}，准备触发二次评估...[/yellow]")
                        break  # 跳出 attempt 循环，进入 eval_round 2

                    # 如果是第二轮，计算两次分数的平均值
                    if first_score is not None and eval_round == 2:
                        avg_score = max(0, min(100, int(round((first_score + score) / 2))))
                        combined_reason = (
                            f"二次评估平均分。"
                            f"第一次({first_score}分): {first_reason}；"
                            f"第二次({score}分): {reason}"
                        )[:200]
                        return avg_score, combined_reason
                    else:
                        return score, reason

                # 如果没拿到有效分数，记录错误
                last_error = ValueError(f"AI返回格式异常或缺少score: {result}")

            except AIRequestError as exc:
                last_error = exc
                if exc.kind == "auth":
                    console.print(f"[red]  ❌ AI 凭证问题，停止重试[/red]")
                    return DEFAULT_SCORE_ON_FAILURE, f"AI凭证错误: {exc}"
            except Exception as exc:
                last_error = exc

            if attempt < MAX_SCORE_RETRIES:
                console.print(f"[yellow]  评分第 {attempt} 次失败，重试中...[/yellow]")

        # 如果第一轮重试全部失败且未获得有效分数，跳出外层循环进入全局兜底
        if eval_round == 1 and first_score is None:
            break

    # 全局兜底（仅当两轮都未返回有效结果时到达此处）
    console.print(
        f"[red]  ❌ {job['company']}｜{job['title']} 评分失败，使用默认分数 {DEFAULT_SCORE_ON_FAILURE}[/red]"
    )
    error_type = type(last_error).__name__ if last_error else "unknown"
    reason = f"AI评分失败（{error_type}），使用默认分数"
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

    scoring_cfg = config.get("scoring", {})
    try:
        threshold = float(scoring_cfg.get("threshold", 60))
    except (TypeError, ValueError):
        threshold = 60.0

    approved_count = 0
    filtered_count = 0
    pre_filtered_count = 0  # 预筛淘汰计数

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

            # ═══ 预筛硬过滤（不调用 LLM，零成本） ═══
            pre_score, pre_reason = quick_score(job, config)
            if pre_score == 0:
                update_job_score(db, job["id"], 0, pre_reason)
                update_job_status(db, job["id"], "filtered")
                console.print(
                    f"  [dim]✗ 预筛淘汰: {pre_reason} | "
                    f"{job['company']}｜{job['title']}[/dim]"
                )
                filtered_count += 1
                pre_filtered_count += 1
                progress.update(task, advance=1)
                continue  # 跳过 LLM，省 token

            # ═══ 通过预筛 → AI 打分 ═══
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

            # 全局速率节流
            if index < len(jobs):
                jitter = random.uniform(0, 1.0)
                time.sleep(GLOBAL_REQUEST_INTERVAL + jitter)

    db.close()
    console.print(
        f"\n[green]✓ 评分完成: {approved_count} approved / "
        f"{filtered_count} filtered (其中预筛淘汰 {pre_filtered_count})[/green]"
    )

    # ═══ 自动衔接：评分 → 招呼语 → 发送 ═══
    if approved_count > 0:
        console.print("\n[bold cyan]━━━ 自动进入招呼语生成 ━━━[/bold cyan]\n")
        try:
            from bosshunter.ai.greeter import generate_greetings
            generate_greetings(config)
        except Exception as exc:
            console.print(f"[red]招呼语生成阶段异常: {exc}[/red]")
            console.print("[yellow]可手动执行: bosshunter greet[/yellow]")
            return approved_count, filtered_count

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