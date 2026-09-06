"""AI Greeter - Generate personalized greeting messages with multi-model concurrency."""

import json
import time
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Tuple, List, Dict, Any

import httpx
from rich.console import Console
from rich.status import Status

from bosshunter.ai.credentials import AIRequestError, normalize_ai_error
from bosshunter.db import get_db, get_jobs_by_status, update_job_greeting, update_job_status

console = Console()

# ─── 模块内默认配置 ─────────────────────────────────────────────
DEFAULT_MAX_WORKERS = 4
INITIAL_RPM_LIMIT = 30
MIN_RPM_LIMIT = 5
MAX_GENERATE_RETRIES = 4
MAX_REVIEW_RETRIES = 3
DEFAULT_GREETING = "过往经验很匹配"
DEFAULT_CRITIQUE = "请确保语言自然、突出匹配优势、避免模板化开头"
AI_MAX_GENERATE_TOKENS = 300
AI_MAX_REVIEW_TOKENS = 120
REASONING_EFFORT_EXCLUDE = ["kimi-k3", "sensenova-6.8-flash-lite"]


# ─── Prompt 模板（保持不变）─────────────────────────────────────
_SYSTEM_PROMPT = (
    "你是文本生成器，只输出最终文本。严禁输出思考过程或元描述。"
    "每句话必须锚定用户真实素材，无信息增量的句子一律删除。"
    "禁止使用任何求职打招呼模板句式。"
)

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
3. 内容：只突出1-2个与JD强相关的匹配优势；每提一个自身经历，必须能对应到JD中的具体要求；不谄媚；严禁把JD内容当作我的经历。严禁说我的学历。
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


# ─── 日志工具 ───────────────────────────────────────────────────
def _log(msg: str, style: str = "", icon: str = ""):
    """统一日志输出"""
    prefix = f"{icon} " if icon else ""
    console.print(f"{prefix}{msg}", style=style, highlight=False)


class AdaptiveRateLimiter:
    """线程安全的滑动窗口限流器（静默自适应）"""
    def __init__(self, initial_rpm: int, min_rpm: int):
        self.max_requests = initial_rpm
        self.min_rpm = min_rpm
        self.initial_rpm = initial_rpm
        self.window = 60.0
        self.timestamps: List[float] = []
        self.lock = threading.Lock()

    def wait(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.timestamps = [t for t in self.timestamps if now - t < self.window]
                if len(self.timestamps) < self.max_requests:
                    self.timestamps.append(now)
                    return
                sleep_time = self.timestamps[0] + self.window - now
            time.sleep(max(0, sleep_time))

    def decrease_limit(self):
        with self.lock:
            self.max_requests = max(self.min_rpm, self.max_requests // 2)

    def increase_limit(self):
        with self.lock:
            new_limit = min(self.initial_rpm, self.max_requests + 2)
            if new_limit != self.max_requests:
                self.max_requests = new_limit


_limiter = None


def _get_resume_summary(config: dict) -> str:
    resume_path = Path(config.get("profile", {}).get("resume_path", "./resume.md"))
    if not resume_path.exists():
        return ""
    content = resume_path.read_text(encoding="utf-8")
    return content[:1500]


def _get_models(config: dict) -> List[str]:
    ai_cfg = config.get("ai", {})
    models = ai_cfg.get("models")
    if models and isinstance(models, list) and len(models) > 0:
        return [str(m) for m in models]
    single = ai_cfg.get("model")
    if single:
        return [single]
    return []


def _truncate_prompt_text(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    marker = "\n...[已裁剪]...\n"
    available = max(limit - len(marker), 2)
    head = max(int(available * 0.7), 1)
    return f"{text[:head]}{marker}{text[-(available - head):]}"


def _parse_json_response(response: str) -> dict | None:
    if not response:
        return None
    cleaned = response.strip()
    if cleaned.startswith("```"):
        first_nl = cleaned.index("\n") if "\n" in cleaned else 3
        cleaned = cleaned[first_nl + 1:].rstrip("`").strip()
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


def _call_ai(
    messages: list[dict],
    config: dict,
    model: str,
    max_tokens: int,
    attempt: int = 1,
    json_mode: bool = False,
) -> str | None:
    """调用 AI 接口，静默处理限流与重试"""
    ai_cfg = config.get("ai", {})
    provider = ai_cfg.get("provider", "openai_compatible")
    if provider != "openai_compatible":
        return None

    base_url = ai_cfg.get("base_url", "").rstrip("/")
    api_key = ai_cfg.get("api_key", "")
    if not base_url or not api_key:
        from bosshunter.ai.credentials import get_ai_base_url, get_ai_api_key
        base_url = get_ai_base_url(config) or ""
        api_key = get_ai_api_key(config) or ""
    if not base_url or not api_key:
        return None

    url = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    temperature = 1.0 if model == "kimi-k3" else 0.7
    payload = {
        "model": model, "messages": messages, "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if model not in REASONING_EFFORT_EXCLUDE:
        payload["reasoning_effort"] = "none"

    global _limiter
    if _limiter:
        _limiter.wait()

    for retry_429 in range(3):
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=60)
            if resp.status_code == 429:
                if _limiter:
                    _limiter.decrease_limit()
                wait_time = min(float(resp.headers.get("Retry-After", 2)), 5.0)
                time.sleep(wait_time)
                continue
            if resp.status_code == 200 and _limiter:
                _limiter.increase_limit()
            if resp.status_code != 200:
                raise normalize_ai_error(httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                ))
            data = resp.json()
            choices = data.get("choices", [])
            if not choices:
                return None
            finish_reason = choices[0].get("finish_reason")
            if finish_reason in ("content_filter", "length"):
                return None
            content = choices[0].get("message", {}).get("content")
            return content.strip() if content else None
        except httpx.HTTPStatusError as exc:
            raise normalize_ai_error(exc) from exc
        except AIRequestError:
            raise
        except Exception as exc:
            raise AIRequestError("network", str(exc)) from exc
    raise AIRequestError("rate_limit", "429 retries exhausted")


def _generate_greeting_once(job, resume_summary, config, model, critique="", *, compact=False, max_tokens=AI_MAX_GENERATE_TOKENS, attempt=1):
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

    main_prompt = GREETING_PROMPT.format(
        resume_summary=_truncate_prompt_text(resume_summary, resume_limit),
        title=job.get("title", "未知岗位"), company=job.get("company", "未知公司"),
        salary=job.get("salary") or "面议", jd_summary=jd_summary,
        match_reason=_truncate_prompt_text(job.get("score_reason", ""), 240),
        extra_highlights=_truncate_prompt_text(extra_highlights, 500),
    )
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": main_prompt}]
    if critique:
        messages.append({"role": "user", "content": f"上次生成的问题：{critique}，请避免此问题并重新生成。"})

    greeting_raw = _call_ai(messages, config, model, max_tokens=max_tokens, attempt=attempt, json_mode=True)
    if not greeting_raw:
        return None

    data = _parse_json_response(greeting_raw)
    if not data:
        return None

    greeting = data.get("greeting", "").strip().strip('"\'')
    if not greeting:
        return None

    # 超长截断
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


def _generate_with_retry(job, resume_summary, config, model, critique=""):
    last_error = None
    for attempt in range(1, MAX_GENERATE_RETRIES + 1):
        try:
            result = _generate_greeting_once(job, resume_summary, config, model, critique, attempt=attempt)
            if result:
                return result
            last_error = ValueError("AI returned empty")
        except AIRequestError as exc:
            last_error = exc
            if exc.kind in ("auth", "non_retryable"):
                raise
            if exc.kind == "context_limit" and attempt < MAX_GENERATE_RETRIES:
                try:
                    result = _generate_greeting_once(job, resume_summary, config, model, critique, compact=True, max_tokens=160, attempt=attempt)
                    if result:
                        return result
                except AIRequestError:
                    pass
        except Exception as exc:
            last_error = exc
    return None


def _review_with_retry(greeting, job, config, model):
    for attempt in range(1, MAX_REVIEW_RETRIES + 1):
        try:
            result = _review_greeting(greeting, job, config, model, attempt=attempt)
            if result:
                return result
        except AIRequestError as exc:
            if exc.kind in ("auth", "non_retryable"):
                raise
        except Exception:
            pass
    return None


def _review_greeting(greeting, job, config, model, attempt=1):
    prompt = REVIEW_PROMPT.format(title=job["title"], company=job["company"], greeting=greeting)
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
    response = _call_ai(messages, config, model, max_tokens=AI_MAX_REVIEW_TOKENS, attempt=attempt, json_mode=True)
    return _parse_json_response(response)


def _process_single_job(job, resume_summary, config, model) -> Tuple[Optional[str], str, Dict[str, Any]]:
    """处理单个岗位，返回 (greeting, status_flag, meta)"""
    max_iterations = int(config.get("ai", {}).get("greeting_max_iterations", 2) or 0)
    best_greeting = None
    used_fallback = False
    meta = {"iterations": 0, "reviews": 0}

    if not resume_summary:
        return DEFAULT_GREETING, "fallback", meta

    for iteration in range(max_iterations + 1):
        meta["iterations"] = iteration + 1
        critique = ""

        if iteration > 0 and best_greeting:
            meta["reviews"] += 1
            try:
                review = _review_with_retry(best_greeting, job, config, model)
            except AIRequestError as exc:
                if exc.kind == "auth":
                    raise
                review = None

            if review and review.get("pass", False):
                break
            critique = review.get("critique", DEFAULT_CRITIQUE) if review else DEFAULT_CRITIQUE

        try:
            greeting = _generate_with_retry(job, resume_summary, config, model, critique)
        except AIRequestError as exc:
            if exc.kind == "auth":
                raise
            greeting = None

        if not greeting:
            break
        best_greeting = greeting
        if max_iterations == 0:
            break

    if not best_greeting:
        best_greeting = DEFAULT_GREETING
        used_fallback = True

    return best_greeting, "fallback" if used_fallback else "success", meta


def generate_greetings(config: dict) -> int:
    db = get_db()
    jobs = get_jobs_by_status(db, "approved")

    _workbench_job_ids = {str(jid) for jid in config.get("_workbench_job_ids", [])}
    if _workbench_job_ids:
        jobs = [j for j in jobs if str(j["id"]) in _workbench_job_ids]

    if not jobs:
        _log("没有已确认岗位可生成招呼语", "yellow", "⚠")
        db.close()
        return 0

    resume_summary = _get_resume_summary(config)
    models = _get_models(config)
    if not models:
        _log("未配置 AI 模型，检查 config.yaml", "red", "✗")
        db.close()
        return 0

    # 并发配置
    greeting_cfg = config.get("greeting", {})
    scoring_cfg = config.get("scoring", {})
    max_workers = min(
        int(greeting_cfg.get("max_workers", scoring_cfg.get("max_workers", DEFAULT_MAX_WORKERS))),
        len(models)
    )
    initial_rpm = int(greeting_cfg.get("initial_rpm", scoring_cfg.get("initial_rpm", INITIAL_RPM_LIMIT)))
    min_rpm = int(greeting_cfg.get("min_rpm", scoring_cfg.get("min_rpm", MIN_RPM_LIMIT)))

    global _limiter
    _limiter = AdaptiveRateLimiter(initial_rpm=initial_rpm, min_rpm=min_rpm)

    # ▶ 启动摘要
    _log(f"招呼语生成 | 岗位={len(jobs)} 模型={len(models)} 并发={max_workers} RPM={initial_rpm}", "cyan", "🚀")

    count = fallback_count = 0
    total = len(jobs)
    completed = 0
    status = Status(f"[bold green]生成中 0/{total}", console=console)
    status.start()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(_process_single_job, job, resume_summary, config, models[i % len(models)]): job
            for i, job in enumerate(jobs)
        }

        for future in as_completed(future_map):
            job = future_map[future]
            completed += 1
            status.update(f"[bold green]生成中 {completed}/{total}")
            tag = f"{job['company']}｜{job['title']}"

            try:
                greeting, flag, meta = future.result()
                iters = meta.get("iterations", 1)
                reviews = meta.get("reviews", 0)
                preview = (greeting or "").replace("\n", " ")[:50]

                update_job_greeting(db, job["id"], greeting)
                update_job_status(db, job["id"], "ready")
                count += 1

                if flag == "fallback":
                    fallback_count += 1
                    _log(f"⚠ 兜底 | {tag:<28} | I{iters}/R{reviews} | {preview}", "yellow")
                else:
                    _log(f"✓ {tag:<28} | I{iters}/R{reviews} | {preview}", "green")

            except AIRequestError as exc:
                if exc.kind == "auth":
                    status.stop()
                    _log(f"认证失败，终止生成 | {tag}", "red bold", "🛑")
                    for f in future_map:
                        if not f.done():
                            f.cancel()
                    break
                _log(f"✗ 失败 | {tag} | {exc.user_message}", "red")
            except Exception as exc:
                _log(f"✗ 异常 | {tag} | {exc}", "red")

    status.stop()
    db.close()

    # ▶ 完成摘要
    summary = f"完成 | ✓{count}"
    if fallback_count:
        summary += f" ⚠兜底{fallback_count}"
    _log(summary, "green bold", "📊")

    return count