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
DEFAULT_GREETING = "跟我很匹配呀"
MAX_GENERATE_RETRIES = 2
MAX_REVIEW_RETRIES = 2
GLOBAL_REQUEST_INTERVAL = 4.0
MAX_RETRY_WAIT = 15.0
DEFAULT_CRITIQUE = "请确保语言自然、突出匹配优势、避免模板化开头"
AI_MAX_GENERATE_TOKENS = 300
AI_MAX_REVIEW_TOKENS = 120


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


_limiter = RateLimiter(GLOBAL_REQUEST_INTERVAL)


# ─── Prompt 模板 ───────────────────────────────────────────────────
_SYSTEM_PROMPT = (
    "你是文本生成器，只输出最终文本。严禁输出思考过程或元描述。"
    "每句话必须锚定用户真实素材，无信息增量的句子一律删除。"
    "禁止使用任何求职打招呼模板句式。"
)

# ★ 修复：所有示例占位符已转义为 {{...}}，避免 KeyError
GREETING_PROMPT = """你在BOSS直聘给HR发招呼语，目标是让对方愿意回复。

## 输入
- 我的背景（仅可从此取信息，严禁捏造/改写）：{resume_summary}
- 岗位：{title} @ {company} | 薪资：{salary}
- JD摘要（内容锚点，必须优先响应）：{jd_summary}
- 匹配点（JD要求与我的经历的对应关系）：{match_reason}
- 额外亮点（自然融入，不罗列）：{extra_highlights}

## 创作规则
1. 风格：真人IM口语化，轻松自然，可用语气词但不卖萌。不要公文体/邮件感/AI客服感。
2. 开头：首句必须以JD核心关键词（技术栈/业务场景/项目类型）起笔，紧跟我的真实经历作为主语直接衔接。
   ✅ 正确范式："你们JD提到的{{jd_keyword}}，我在{{real_project}}中负责过{{specific_task}}"
   ✅ 正确范式："{{jd_keyword}}这块我比较熟，之前在{{company_or_project}}做过{{concrete_result}}"
   ❌ 错误范式：以"您好""看到""注意到""对...感兴趣"等人称代词或感知动词起笔
3. 内容：只突出1-2个与JD强相关的匹配优势；每提一个自身经历，必须能对应到JD中的具体要求；不谄媚；严禁把JD内容当作我的经历。
4. 结尾：必须陈述句收尾，且提供与JD相关的信息增量（如补充JD关注点的成果/匹配细节）。禁止问句、邀约、客套话（"方便聊聊""期待沟通""盼回复"等）。
5. 格式：50-146字，短句为主，适当换行，无Markdown/列表符号。

## 输出
以纯JSON对象返回，key为"greeting"，value为招呼语文本。不要包含markdown代码块标记。"""

REVIEW_PROMPT = """评估BOSS直聘招呼语质量。

## 输入
- 岗位：{title} @ {company}
- 招呼语：{greeting}

## 评分（每项1-10分）
1. 去模板化：首句以JD具体名词/数据起笔？避开所有人称代词/感知动词开头的套话？结尾为陈述句且无问句/邀约/客套？
2. 自然度：像真人IM消息，略微口语化、无公文/AI感？
3. 相关性：针对岗位突出1-2个匹配点，不泛泛而谈？
4. 真实性：仅用候选人真实背景，无捏造？
5. 可读性：短句为主，适配手机阅读？

若第1项<7分，critique须指出问题类型（如"首句以感知动词起笔"），并给出基于真实素材的改写方向（20字内）。
⚠ critique中严禁复述原文中的套话短语，只描述问题类别和改写方向。

## 输出
以纯JSON返回，仅包含以下3个字段：
{{"avg": float, "pass": bool, "critique": string}}
- pass: avg>=7 则为 true
- critique: 仅当 pass=false 时填写(≤30字)，否则为空字符串
不要包含其他字段。"""


# ─── 工具函数 ─────────────────────────────────────────────────────
def _get_resume_summary(config: dict) -> str:
    resume_path = Path(config.get("profile", {}).get("resume_path", "./resume.md"))
    if not resume_path.exists():
        console.print(f"[yellow]⚠ 简历文件不存在: {resume_path}[/yellow]")
        return ""
    content = resume_path.read_text(encoding="utf-8")
    console.print(f"[dim]📄 已加载简历摘要 ({len(content)} 字符)[/dim]")
    return content[:1500]


def _call_ai(
    messages: list[dict],
    config: dict,
    max_tokens: int = 300,
    attempt: int = 1,
    json_mode: bool = False,
) -> str | None:
    """调用 OpenAI 兼容模型，含限流、退避、finish_reason 防御"""
    ai_cfg = config.get("ai", {})
    provider = ai_cfg.get("provider", "openai_compatible")

    if provider != "openai_compatible":
        console.print(f"[red]❌ 不支持的 AI provider: {provider}[/red]")
        return None

    base_url = ai_cfg.get("base_url", "").rstrip("/")
    api_key = ai_cfg.get("api_key", "")
    model = ai_cfg.get("model", "")

    masked_key = f"{api_key[:8]}..." if len(api_key) > 8 else "(empty)"
    console.print(f"[dim]🔧 AI配置: model={model}, base_url={base_url}, key={masked_key}[/dim]")

    if not all([base_url, api_key, model]):
        missing = [k for k, v in {"base_url": base_url, "api_key": api_key, "model": model}.items() if not v]
        console.print(f"[red]❌ AI 配置不完整，缺少: {', '.join(missing)}[/red]")
        return None

    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "reasoning_effort": "none",
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    _limiter.wait()

    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=60)
        console.print(f"[dim]📡 HTTP {resp.status_code} | 响应长度: {len(resp.text)}[/dim]")

        if resp.status_code == 429:
            err_body = {}
            try:
                err_body = resp.json().get("error", {})
            except Exception:
                pass
            err_code = str(err_body.get("code", ""))
            err_msg = str(err_body.get("message", ""))

            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                wait_time = min(float(retry_after) + random.uniform(0.5, 1.5), MAX_RETRY_WAIT)
                console.print(f"[yellow]⏳ Retry-After 指定等待 {wait_time:.1f}s (第{attempt}次)[/yellow]")
            elif "tpm" in err_code.lower() or "tpm" in err_msg.lower():
                wait_time = min(3.0 + random.uniform(0, 2), MAX_RETRY_WAIT)
                console.print(f"[yellow]⏳ TPM限流({err_code})，短等待 {wait_time:.1f}s[/yellow]")
            else:
                wait_time = min(15 * (2 ** (attempt - 1)) + random.uniform(0, 2), MAX_RETRY_WAIT)
                console.print(f"[yellow]⏳ RPM限流，等待 {wait_time:.1f}s (第{attempt}次)[/yellow]")

            time.sleep(wait_time)
            raise AIRequestError(kind="rate_limit", user_message=f"HTTP 429: {err_code or 'rate_limited'}, waited {wait_time:.1f}s")

        if resp.status_code != 200:
            console.print(f"[red]❌ API错误响应: {resp.text[:500]}[/red]")

        resp.raise_for_status()
        data = resp.json()

        # 缓存监控
        usage = data.get("usage", {})
        cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
        total_prompt = usage.get("prompt_tokens", 0)
        if cached > 0:
            console.print(f"[dim]💰 缓存命中: {cached}/{total_prompt} prompt tokens[/dim]")

        choices = data.get("choices", [])
        if not choices:
            console.print(f"[red]❌ 响应中无 choices: {str(data)[:300]}[/red]")
            return None

        choice = choices[0]
        msg = choice.get("message", {})
        finish_reason = choice.get("finish_reason")

        if finish_reason == "content_filter":
            console.print("[yellow]⚠ 内容被合规审核拦截[/yellow]")
            return None
        if finish_reason == "length":
            console.print("[yellow]⚠ 输出被截断(finish_reason=length)[/yellow]")

        content = msg.get("content") or ""
        if not content:
            console.print(f"[yellow]⚠ AI返回空content: {msg}[/yellow]")
            return None

        console.print(f"[dim]✅ AI返回成功 ({len(content)} 字符)[/dim]")
        return content.strip()

    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        body = exc.response.text[:500]
        console.print(f"[red]❌ HTTP {status}: {body}[/red]")

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
        console.print(f"[red]❌ 网络异常: {type(exc).__name__}: {exc}[/red]")
        raise AIRequestError(kind="network", user_message=str(exc)) from exc


def _truncate_prompt_text(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    marker = "\n...[已裁剪]...\n"
    available = max(limit - len(marker), 2)
    head = max(int(available * 0.7), 1)
    return f"{text[:head]}{marker}{text[-(available - head):]}"


def _parse_json_response(response: str) -> dict | None:
    """统一 JSON 解析：先直接解析，失败则花括号截取兜底"""
    if not response:
        return None
    cleaned = response.strip()
    if cleaned.startswith("```"):
        first_nl = cleaned.index("\n") if "\n" in cleaned else 3
        cleaned = cleaned[first_nl + 1:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, TypeError):
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            result = json.loads(cleaned[start:end])
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def _review_greeting(greeting: str, job: dict, config: dict, attempt: int = 1) -> dict | None:
    prompt = REVIEW_PROMPT.format(title=job["title"], company=job["company"], greeting=greeting)
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    response = _call_ai(messages, config, max_tokens=AI_MAX_REVIEW_TOKENS, attempt=attempt, json_mode=True)
    return _parse_json_response(response)


def _generate_greeting_once(
    job: dict, resume_summary: str, config: dict, critique: str = "",
    *, compact: bool = False, max_tokens: int = AI_MAX_GENERATE_TOKENS, attempt: int = 1,
) -> str | None:
    jd_limit = 250 if compact else 500
    resume_limit = 800 if compact else 1500
    jd_summary = _truncate_prompt_text(job.get("jd", ""), jd_limit) or "无详细描述"

    profile_cfg = config.get("profile", {})
    highlights = profile_cfg.get("extra_highlights", [])
    portfolio_url = profile_cfg.get("portfolio_url", "")
    highlight_lines = [f"- {h}" for h in highlights]
    if portfolio_url:
        highlight_lines.append(f"- 个人作品集网址：{portfolio_url}")
    extra_highlights = "\n".join(highlight_lines) if highlight_lines else "（无额外亮点配置）"

    # ★ 安全获取所有字段，避免 KeyError
    main_prompt = GREETING_PROMPT.format(
        resume_summary=_truncate_prompt_text(resume_summary, resume_limit),
        title=job.get("title", "未知岗位"),
        company=job.get("company", "未知公司"),
        salary=job.get("salary") or "面议",
        jd_summary=jd_summary,
        match_reason=_truncate_prompt_text(job.get("score_reason", ""), 240),
        extra_highlights=_truncate_prompt_text(extra_highlights, 500),
    )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": main_prompt},
    ]
    if critique:
        messages.append({"role": "user", "content": f"上次生成的问题：{critique}，请避免此问题并重新生成。"})

    console.print(f"[cyan]🎯 生成招呼语 job_id={job.get('id')} attempt={attempt}{' (compact)' if compact else ''}[/cyan]")
    greeting_raw = _call_ai(messages, config, max_tokens=max_tokens, attempt=attempt, json_mode=True)
    if not greeting_raw:
        return None

    data = _parse_json_response(greeting_raw)
    if not data:
        console.print(f"[yellow]⚠ JSON解析失败，原始响应: {greeting_raw[:200]}[/yellow]")
        return None

    greeting = data.get("greeting", "").strip().strip('"\'')
    if not greeting:
        console.print("[yellow]⚠ AI输出经解析后为空[/yellow]")
        return None

    # 超长截断（保留句子完整性）
    if len(greeting) > 150:
        cut = greeting[:150]
        for sep in ("。", "！", "～", "\n"):
            idx = cut.rfind(sep)
            if idx > 50:
                greeting = cut[:idx + 1]
                break
        else:
            greeting = cut

    return greeting


def _generate_with_retry(job: dict, resume_summary: str, config: dict, critique: str = "") -> str | None:
    last_error: Exception | None = None
    for attempt in range(1, MAX_GENERATE_RETRIES + 1):
        try:
            result = _generate_greeting_once(job, resume_summary, config, critique, attempt=attempt)
            if result:
                return result
            last_error = ValueError("AI 返回空内容")
        except AIRequestError as exc:
            last_error = exc
            if exc.kind in ("auth", "non_retryable"):
                console.print(f"[red]🛑 AI请求不可重试({exc.kind})，停止[/red]")
                raise
            if exc.kind == "context_limit" and attempt < MAX_GENERATE_RETRIES:
                try:
                    result = _generate_greeting_once(job, resume_summary, config, critique, compact=True, max_tokens=160, attempt=attempt)
                    if result:
                        return result
                except AIRequestError:
                    pass
        except Exception as exc:
            last_error = exc
            console.print(f"[red]❌ 生成异常(尝试{attempt}): {type(exc).__name__}: {exc}[/red]")

        if attempt < MAX_GENERATE_RETRIES:
            console.print(f"[yellow]⟳ 招呼语生成第{attempt}次失败，重试中...[/yellow]")

    return None


def _review_with_retry(greeting: str, job: dict, config: dict) -> dict | None:
    for attempt in range(1, MAX_REVIEW_RETRIES + 1):
        try:
            result = _review_greeting(greeting, job, config, attempt=attempt)
            if result:
                return result
        except AIRequestError as exc:
            if exc.kind in ("auth", "non_retryable"):
                raise
        except Exception as exc:
            console.print(f"[red]❌ Review异常(尝试{attempt}): {type(exc).__name__}: {exc}[/red]")
    return None


def generate_greetings(config: dict) -> int:
    """Generate greetings for approved jobs. Returns count processed."""
    import os
    console.print(f"\n[bold]📂 greeter模块: {os.path.abspath(__file__)}[/bold]")
    db = get_db()
    jobs = get_jobs_by_status(db, "approved")

    _workbench_job_ids = {str(jid) for jid in config.get("_workbench_job_ids", [])}
    if _workbench_job_ids:
        jobs = [j for j in jobs if str(j["id"]) in _workbench_job_ids]

    if not jobs:
        console.print("[yellow]没有已确认的岗位可生成招呼语。[/yellow]")
        db.close()
        return 0

    console.print(f"[green]📋 待处理岗位: {len(jobs)} 个[/green]")

    resume_summary = _get_resume_summary(config)
    if not resume_summary:
        console.print("[yellow]⚠ 无法读取简历，将使用默认招呼语[/yellow]")

    ai_cfg = config.get("ai", {})
    try:
        max_iterations = max(0, int(ai_cfg.get("greeting_max_iterations", 4) or 0))
    except (TypeError, ValueError):
        max_iterations = 2

    count = 0
    fallback_count = 0

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
        task = progress.add_task(f"生成招呼语 (0/{len(jobs)})", total=len(jobs))

        for index, job in enumerate(jobs, start=1):
            progress.update(task, description=f"招呼语: {job['company'][:10]} - {job['title'][:15]} ({index}/{len(jobs)})")
            best_greeting: str | None = None

            if resume_summary:
                for iteration in range(max_iterations + 1):
                    critique = ""
                    if iteration > 0 and best_greeting:
                        try:
                            review = _review_with_retry(best_greeting, job, config)
                        except AIRequestError as exc:
                            if exc.kind == "auth":
                                console.print("[red]🛑 AI凭证错误，终止生成[/red]")
                                db.close()
                                return count
                            review = None

                        if review and review.get("pass", False):
                            console.print(f"[dim]✅ Review通过 (avg={review.get('avg')})[/dim]")
                            break
                        critique = review.get("critique", DEFAULT_CRITIQUE) if review else DEFAULT_CRITIQUE
                        console.print(f"[yellow]🔄 Review未通过，迭代{iteration+1}: {critique}[/yellow]")

                    try:
                        greeting = _generate_with_retry(job, resume_summary, config, critique)
                    except AIRequestError as exc:
                        if exc.kind == "auth":
                            console.print("[red]🛑 AI凭证错误，终止生成[/red]")
                            db.close()
                            return count
                        greeting = None

                    if not greeting:
                        break
                    best_greeting = greeting
                    if max_iterations == 0:
                        break

            if not best_greeting:
                best_greeting = DEFAULT_GREETING
                fallback_count += 1
                console.print(f"[yellow]⚠ {job['company']}｜{job['title']} AI生成失败，使用默认「{DEFAULT_GREETING}」[/yellow]")

            update_job_greeting(db, job["id"], best_greeting)
            update_job_status(db, job["id"], "ready")
            count += 1
            progress.update(task, advance=1)
            console.print(f"[green]📝 {best_greeting}[/green]")

    db.close()
    console.print(f"\n[bold green]✓ 招呼语生成完成: {count} 个岗位[/bold green]")
    if fallback_count:
        console.print(f"[yellow]  其中 {fallback_count} 个使用默认招呼语[/yellow]")
    return count