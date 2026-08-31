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

# ─── Prompt 模板（System/User 分离，最大化缓存命中） ───────────────────────────────────────
SYSTEM_PROMPT = (
    "你是一位资深技术招聘顾问。评估简历与岗位的匹配度。\n"
    "【输出强制约束】\n"
    "1. 必须且只能输出合法的JSON对象\n"
    "2. 格式严格为：{\"score\": int(0-100), \"reason\": \"50字内简述\"}\n"
    "3. 禁止输出任何思考过程、分析文字、Markdown标记或额外字段"
)

USER_PROMPT_TEMPLATE = """## 简历摘要
{resume_summary}

## 岗位信息
- 职位：{title}
- 公司：{company}
- 薪资：{salary}
- 岗位描述：{jd}

## 评分维度
1. 技术栈匹配度（40%）
2. 经验年限匹配（10%）
3. 业务领域相关性（30%）
4. 薪资匹配度（10%）
5. 发展空间（10%）

请根据上述信息评估匹配程度。"""

RE_EVAL_PROMPT_TEMPLATE = """## 二次独立评估请求
上一轮你对该岗位的评分为 {first_score} 分，理由：{first_reason}
请抛开先前判断，仅基于以下核心信息重新独立评估：
- 职位：{title} @ {company}
- 简历关键匹配点：{resume_snippet}
- JD核心要求：{jd_snippet}

请重新给出你的独立判断。"""


# ─── 配置常量 ───────────────────────────────────────────────
MAX_SCORE_RETRIES = 3
GLOBAL_REQUEST_INTERVAL = 3.0   # 全局请求间隔(秒)，防止触碰RPM上限
MAX_RETRY_WAIT = 10.0           # 429 限流最大等待秒数
AI_MAX_TOKENS = 150             # 评分JSON极短，150 tokens足够冗余


class RateLimiter:
    """简易滑动窗口限流器，精确控制请求间隔"""
    def __init__(self, interval: float):
        self.interval = interval
        self._last_call = 0.0

    def wait(self):
        now = time.monotonic()
        elapsed = now - self._last_call
        if elapsed < self.interval:
            sleep_time = self.interval - elapsed + random.uniform(0, 0.3)
            time.sleep(sleep_time)
        self._last_call = time.monotonic()


def _get_resume_summary(config: dict) -> str:
    resume_path = Path(config.get("profile", {}).get("resume_path", "./resume.md"))
    if not resume_path.exists():
        return ""
    return resume_path.read_text(encoding="utf-8")[:2000]


def _call_ai(messages: list[dict], config: dict, max_tokens: int = AI_MAX_TOKENS, attempt: int = 1) -> str | None:
    """根据 config.ai 配置调用 OpenAI 兼容模型（含429指数退避与缓存监控）"""
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

    # 针对 DeepSeek V4 Flash 优化的 payload
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.1,
        "reasoning_effort": "none",                  # 关闭思考模式，节省 token 和时间
        "response_format": {"type": "json_object"}   # 启用原生 JSON 输出
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

        # 💰 缓存监控日志
        usage = data.get("usage", {})
        cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
        total_prompt = usage.get("prompt_tokens", 0)
        if cached > 0:
            console.print(f"[dim]💰 [Score] 缓存命中: {cached}/{total_prompt} prompt tokens[/dim]")

        choices = data.get("choices", [])
        if not choices:
            console.print(f"[red]❌ [Score] 响应中无 choices 字段: {str(data)[:300]}[/red]")
            return None

        choice = choices[0]
        msg = choice.get("message", {})
        finish_reason = choice.get("finish_reason")

        # 防御性检查：合规拦截或截断
        if finish_reason == "content_filter":
            console.print(f"[yellow]⚠ [Score] 内容被合规审核拦截，跳过[/yellow]")
            return None
        if finish_reason == "length":
            console.print(f"[yellow]⚠ [Score] 输出被截断(finish_reason=length)，结果可能不完整[/yellow]")

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


def _call_score_ai(messages: list[dict], config: dict, attempt: int = 1) -> dict | None:
    """Call AI for scoring, return parsed dict or None."""
    response = _call_ai(messages, config, max_tokens=AI_MAX_TOKENS, attempt=attempt)
    if not response:
        return None

    console.print(f"[dim]📝 [Score] 原始响应预览: {response[:300]}[/dim]")

    cleaned = response.strip()

    # 1. 去除 Markdown 代码块（安全网）
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

    # 4. 正则兜底提取（增强校验）
    score_match = re.search(r'(?:score|分数|匹配度)[^\d]*(\d{1,3})', cleaned)
    reason_match = re.search(r'(?:reason|原因|总结)[：:]\s*(.{10,80})', cleaned)
    if score_match:
        raw_score = int(score_match.group(1))
        # 校验：分数必须在 0-100 且 reason 长度合理
        if 0 <= raw_score <= 100 and reason_match and len(reason_match.group(1).strip()) >= 5:
            fallback_reason = reason_match.group(1).strip()
            console.print(f"[yellow]⚠ [Score] 正则兜底提取: score={raw_score}[/yellow]")
            return {"score": raw_score, "reason": fallback_reason}
        else:
            console.print(f"[yellow]⚠ [Score] 正则提取值异常(score={raw_score})，视为解析失败[/yellow]")

    # 解析彻底失败 → 返回 None
    console.print(f"[red]❌ [Score] 所有解析方式均失败，跳过该岗位 | 内容: {cleaned[:200]}[/red]")
    return None


def _score_single_job(job: dict, resume_summary: str, config: dict) -> tuple[int, str, str | None]:
    """
    对单个岗位进行AI评分。
    返回: (score, reason, error_kind)
    - 成功时: (score, reason, None)
    - 失败时: (None, None, error_kind)  # error_kind 为 "auth", "network" 等，用于外层熔断
    """
    base_user_prompt = USER_PROMPT_TEMPLATE.format(
        resume_summary=resume_summary,
        title=job["title"],
        company=job["company"],
        salary=job.get("salary") or "面议",
        jd=(job.get("jd") or "")[:800],
    )

    scoring_cfg = config.get("scoring", {})
    threshold = float(scoring_cfg.get("threshold", 50))

    first_score: int | None = None
    first_reason: str = ""
    last_error_kind: str | None = None

    # 最多进行两轮评分：第一轮正常评分，如果低分则触发第二轮
    for eval_round in range(1, 3):
        # 构造 messages (System 保持不变以命中缓存，User 动态变化)
        if eval_round == 2:
            user_prompt = RE_EVAL_PROMPT_TEMPLATE.format(
                first_score=first_score,
                first_reason=first_reason,
                title=job["title"],
                company=job["company"],
                resume_snippet=resume_summary[:300],
                jd_snippet=(job.get("jd") or "")[:300]
            )
            console.print(f"[yellow]  ⏳ 二次评估前等待 2 秒，避免触发限流...[/yellow]")
            time.sleep(2)
        else:
            user_prompt = base_user_prompt

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ]

        last_error_kind = None  # 每轮重置错误记录

        for attempt in range(1, MAX_SCORE_RETRIES + 1):
            console.print(f"[dim]🔄 [Score] {job['company']}｜{job['title']} 第 {eval_round} 轮第 {attempt}/{MAX_SCORE_RETRIES} 次[/dim]")
            try:
                result = _call_score_ai(messages, config, attempt=attempt)

                console.print(f"[dim]   📥 AI原始返回: {result}[/dim]")

                if result and isinstance(result.get("score"), (int, float)):
                    score = max(0, min(100, int(result["score"])))
                    reason = str(result.get("reason", ""))[:200]

                    # 核心逻辑：判断是否需要二次评估
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
                        return avg_score, combined_reason, None
                    else:
                        return score, reason, None

                # AI返回了但缺少有效score字段
                last_error_kind = "parse_error"

            except AIRequestError as exc:
                last_error_kind = exc.kind
                if exc.kind == "auth":
                    console.print(f"[red]  ❌ AI 凭证问题，停止重试[/red]")
                    return None, None, "auth"  # 凭证错误直接返回，触发全局熔断
            except Exception as exc:
                last_error_kind = "unknown"

            if attempt < MAX_SCORE_RETRIES:
                console.print(f"[yellow]  评分第 {attempt} 次失败，重试中...[/yellow]")

        # 第一轮重试全部失败且未获得有效分数，跳出外层循环
        if eval_round == 1 and first_score is None:
            break

    # 两轮都失败时返回 None，由上层决定跳过
    console.print(
        f"[yellow]  ⏭ {job['company']}｜{job['title']} 评分失败({last_error_kind})，跳过，下次再评[/yellow]"
    )
    return None, None, last_error_kind


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
    pre_filtered_count = 0
    skipped_count = 0

    # 初始化滑动窗口限流器
    limiter = RateLimiter(GLOBAL_REQUEST_INTERVAL)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(f"AI 评分 (0/{len(jobs)})", total=len(jobs))

        for index, job in enumerate(jobs, start=1):
            # 更新进度条（嵌入实时统计）
            progress.update(
                task,
                description=(
                    f"评分: {job['company'][:8]}.. | "
                    f"[green]✓{approved_count}[/green] "
                    f"[dim]✗{filtered_count}[/dim] "
                    f"[yellow]⏭{skipped_count}[/yellow] | "
                    f"{index}/{len(jobs)}"
                ),
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
                continue

            # ═══ 通过预筛 → AI 打分 ═══
            limiter.wait()  # 精确控制请求间隔
            score, reason, error_kind = _score_single_job(job, resume_summary, config)

            # ★ 凭证熔断：检测到 auth 错误立即终止整个流程
            if error_kind == "auth":
                console.print("[red]🛑 检测到AI凭证错误，立即终止评分流程！请检查 config.yaml[/red]")
                break

            # ★ 失败跳过：保持 pending 状态，下次再评
            if score is None:
                skipped_count += 1
                console.print(
                    f"  [yellow]⏭ 跳过: {job['company']}｜{job['title']} (保持pending，下次再评)[/yellow]"
                )
                progress.update(task, advance=1)
                continue

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

    db.close()
    console.print(
        f"\n[green]✓ 评分完成: {approved_count} approved / "
        f"{filtered_count} filtered (预筛 {pre_filtered_count}) / "
        f"{skipped_count} skipped[/green]"
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
        console.print("[yellow]没有通过评分的岗位，跳过后续流程。[/yellow]")

    return approved_count, filtered_count