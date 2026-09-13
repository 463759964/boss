"""Score module - AI-powered job-resume matching with multi-model concurrency."""
import json
import re
import time
import threading
import random  # 导入 random 模块
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Tuple, List, Dict, Any

import httpx
from rich.console import Console
from rich.status import Status

from bosshunter.ai.credentials import AIRequestError, normalize_ai_error
from bosshunter.ai.prefilter import quick_score
from bosshunter.db import get_db, get_jobs_by_status, update_job_status, update_job_score

console = Console()

# ─── 硬编码配置 ─────────────────────────────────────────────
SCORE_MAX_WORKERS = 3
SCORE_RPM_LIMIT = 120
AI_MAX_TOKENS = 800
MAX_SCORE_RETRIES = 3
REASONING_EFFORT_EXCLUDE = ["kimi-k3","sensenova-u1-fast"]

# ─── Prompt 模板（保持不变）─────────────────────────────────
SYSTEM_PROMPT = (
    "你是一位资深技术招聘顾问。评估简历与岗位的匹配度。\n"
    "【输出强制约束】\n"
    "1. 必须且只能输出合法的JSON对象\n"
    "2. 格式严格为：{\"score\": int(0-100), \"reason\": \"50字内简述\"}\n"
    "3. 禁止输出任何思考过程、分析文字、Markdown标记或额外字段\n"
    "4. reason中涉及候选人身份时，必须基于【当前时间】计算毕业时长，默认候选人本科学历"
    "使用'X届，已毕业Y年'格式，严禁对非当年毕业生使用'应届生'标签"
)

USER_PROMPT_TEMPLATE = """## 当前时间 {current_date}
## 简历摘要
{resume_summary}
## 岗位信息
- 职位：{title}
- 公司：{company}
- 薪资：{salary}
- 岗位描述：{jd}
## 评分维度
1. 技术栈匹配度（25%）
2. 经验年限匹配（10%）
3. 业务领域相关性（40%）
4. 薪资匹配度（15%）
5. 发展空间（10%）
⚠ 重要：当前是{current_year}年，若候选人毕业于{current_year}年之前，
禁止使用"应届生"标签，应根据实际毕业年份描述（如"25届，已毕业1年"）。
请根据上述信息评估匹配程度。"""

RE_EVAL_PROMPT_TEMPLATE = """## 二次独立评估请求
当前时间：{current_date}
上一轮你对该岗位的评分为 {first_score} 分，理由：{first_reason}
请抛开先前判断，仅基于以下核心信息重新独立评估：
- 职位：{title} @ {company}
- 简历关键匹配点：{resume_snippet}
- JD核心要求：{jd_snippet}
⚠ 重申：当前是{current_year}年，请基于此时间判断候选人毕业时长，
严禁对非当年毕业生使用"应届生"标签。
请重新给出你的独立判断。"""

# ─── 日志工具 ───────────────────────────────────────────────
def _log(msg: str, style: str = "", icon: str = ""):
    """统一日志输出，保持界面整洁"""
    prefix = f"{icon} " if icon else ""
    console.print(f"{prefix}{msg}", style=style, highlight=False)

class AdaptiveRateLimiter:
    """线程安全的滑动窗口限流器（静默自适应）"""
    def __init__(self, initial_rpm: int, min_rpm: int = 5):
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
    text = resume_path.read_text(encoding="utf-8")
    head = text[:300]
    edu_section = ""
    for keyword in ["教育背景", "教育经历", "Education"]:
        idx = text.find(keyword)
        if idx != -1:
            edu_section = text[idx:idx + 500]
            break
    summary = f"{head}\n\n{edu_section}".strip()
    if len(summary) < 2000:
        remaining = text[len(summary):]
        summary += "\n" + remaining[:2000 - len(summary)]
    return summary[:2000]

def _get_models(config: dict) -> List[str]:
    ai_cfg = config.get("ai", {})
    models = ai_cfg.get("models")
    if models and isinstance(models, list) and len(models) > 0:
        return [str(m) for m in models]
    single = ai_cfg.get("model")
    if single:
        return [single]
    return []

def _call_ai(
    messages: List[Dict[str, str]],
    config: dict,
    model: str,
    max_tokens: int,
    attempt: int = 1,
) -> Optional[Tuple[str, str]]:
    """调用 AI 接口，静默处理限流与重试，支持多Key轮询，并返回(内容, Key指纹)"""
    ai_cfg = config.get("ai", {})
    provider = ai_cfg.get("provider", "openai_compatible")
    if provider != "openai_compatible":
        return None

    base_url = ai_cfg.get("base_url", "").rstrip("/")

    # --- 支持从列表中随机选择一个 Key ---
    api_keys_cfg = ai_cfg.get("api_keys", "")
    if isinstance(api_keys_cfg, list) and len(api_keys_cfg) > 0:
        api_keys = random.choice(api_keys_cfg)
    else:
        api_keys = api_keys_cfg
    # --- 结束 ---

    if not base_url or not api_keys:
        from bosshunter.ai.credentials import get_ai_base_url, get_ai_api_keys
        base_url = get_ai_base_url(config) or ""
        api_keys = get_ai_api_keys(config) or ""
        if isinstance(api_keys, list) and len(api_keys) > 0:
             api_keys = random.choice(api_keys)

    if not base_url or not api_keys:
        return None

    url = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_keys}", "Content-Type": "application/json"}

    temperature = 1.0 if model == "kimi-k3" else 0.1
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
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

            # 返回内容和 Key 指纹（前8位）
            key_fingerprint = api_keys[:8] + "..."
            return (content.strip() if content else None), key_fingerprint

        except httpx.HTTPStatusError as exc:
            raise normalize_ai_error(exc) from exc
        except AIRequestError:
            raise
        except Exception as exc:
            raise AIRequestError("network", str(exc)) from exc
    raise AIRequestError("rate_limit", "429 retries exhausted")

def _call_score_ai(messages, config, model, max_tokens, attempt=1):
    """调用 AI 并解析 JSON"""
    response_tuple = _call_ai(messages, config, model, max_tokens, attempt=attempt)
    if not response_tuple:
        return None

    response, key_fp = response_tuple  # 解包获取内容和Key指纹
    if not response:
        return None

    cleaned = response.strip()
    # 1. 去除 Markdown 代码块包裹
    if cleaned.startswith("```"):
        first_nl = cleaned.index("\n") if "\n" in cleaned else 3
        cleaned = cleaned[first_nl + 1:].rstrip("`").strip()

    # 2. 尝试直接解析整个字符串
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result, key_fp  # 返回解析结果和Key指纹
    except (json.JSONDecodeError, TypeError):
        pass

    # 3. 提取首个 {...} 子串进行解析
    start, end = cleaned.find("{"), cleaned.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            result = json.loads(cleaned[start:end])
            if isinstance(result, dict):
                return result, key_fp
        except (json.JSONDecodeError, TypeError):
            pass

    # 4. 正则兜底：从非标准文本中提取 score 和 reason
    score_match = re.search(r'(?:score|分数|匹配度)[^\d]*(\d{1,3})', cleaned)
    reason_match = re.search(r'(?:reason|原因|总结)[：:]\s*(.{10,80})', cleaned)
    if score_match:
        raw_score = int(score_match.group(1))
        if 0 <= raw_score <= 100 and reason_match:
            return {"score": raw_score, "reason": reason_match.group(1).strip()}, key_fp
    return None

def _score_single_job(job, resume_summary, config, model, max_tokens):
    """单岗位评分（逻辑不变）"""
    now = datetime.now()
    current_date = now.strftime("%Y年%m月%d日")
    current_year = now.year
    threshold = float(config.get("scoring", {}).get("threshold", 50))
    max_retries = int(config.get("scoring", {}).get("max_retries", MAX_SCORE_RETRIES))

    best_score, best_reason, last_error_kind = None, "", None
    meta = {"total_attempts": 0, "rounds_used": 0, "model": model, "key": "?"}

    for eval_round in range(1, 3):
        meta["rounds_used"] = eval_round
        if eval_round == 2:
            user_prompt = RE_EVAL_PROMPT_TEMPLATE.format(
                current_date=current_date,
                current_year=current_year,
                first_score=best_score,
                first_reason=best_reason,
                title=job["title"],
                company=job["company"],
                resume_snippet=resume_summary[:300],
                jd_snippet=(job.get("jd") or "")[:300]
            )
            time.sleep(2)
        else:
            user_prompt = USER_PROMPT_TEMPLATE.format(
                current_date=current_date,
                current_year=current_year,
                resume_summary=resume_summary,
                title=job["title"],
                company=job["company"],
                salary=job.get("salary") or "面议",
                jd=(job.get("jd") or "")[:800],
            )

        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}]
        last_error_kind = None

        for attempt in range(1, max_retries + 1):
            meta["total_attempts"] += 1
            try:
                result_tuple = _call_score_ai(messages, config, model, max_tokens, attempt=attempt)
                if result_tuple is not None:
                    parsed_result, key_fp = result_tuple
                    meta["key"] = key_fp  # 存入 meta 供日志使用

                    score = max(0, min(100, int(parsed_result["score"])))
                    reason = str(parsed_result.get("reason", ""))[:200]
                    if best_score is None or score > best_score:
                        best_score, best_reason = score, reason
                    if score >= threshold:
                        return score, reason, None, meta
                    if eval_round == 1:
                        break
                    else:
                        return best_score, best_reason, None, meta
                else:
                    last_error_kind = "parse_error"
            except AIRequestError as exc:
                last_error_kind = exc.kind
                if exc.kind == "auth":
                    return None, None, "auth", meta
            except Exception:
                last_error_kind = "unknown"

        if eval_round == 1 and best_score is None:
            break

    return best_score, best_reason, last_error_kind, meta

def score_jobs(config: dict) -> Tuple[int, int]:
    db = get_db()
    jobs = get_jobs_by_status(db, "pending")
    if not jobs:
        _log("没有待评分岗位，请先运行 crawl", "yellow", "⚠")
        db.close(); return 0, 0

    resume_summary = _get_resume_summary(config)
    if not resume_summary:
        _log("无法读取简历，检查 profile.resume_path", "red", "✗")
        db.close(); return 0, 0

    models = _get_models(config)
    if not models:
        _log("未配置 AI 模型，检查 config.yaml", "red", "✗")
        db.close(); return 0, 0

    max_workers = min(SCORE_MAX_WORKERS, len(models))
    threshold = float(config.get("scoring", {}).get("threshold", 60))

    global _limiter
    _limiter = AdaptiveRateLimiter(initial_rpm=SCORE_RPM_LIMIT, min_rpm=5)

    # --- 启动时打印加载的 Key 数量和指纹 ---
    ai_cfg = config.get("ai", {})
    api_keys_cfg = ai_cfg.get("api_keys", "")
    if isinstance(api_keys_cfg, list):
        key_fingerprints = [f"{k[:8]}..." for k in api_keys_cfg]
        _log(f"🔑 已加载 {len(api_keys_cfg)} 个 API Key: {', '.join(key_fingerprints)}", "cyan")
    # ----------------------------------------------

    # ▶ 启动摘要（仅一行）
    _log(f"评分启动 | 岗位={len(jobs)} 模型={len(models)} 并发={max_workers} RPM={SCORE_RPM_LIMIT} 阈值={threshold}", "cyan", "🚀")

    approved = filtered = pre_filtered = skipped = 0

    # 预筛（静默执行，不逐条打印）
    jobs_to_score = []
    for job in jobs:
        ps, pr = quick_score(job, config)
        if ps == 0:
            update_job_score(db, job["id"], 0, pr)
            update_job_status(db, job["id"], "filtered")
            filtered += 1; pre_filtered += 1
        else:
            jobs_to_score.append(job)

    if not jobs_to_score:
        _log(f"全部被预筛过滤 ({pre_filtered})，无需 AI 评分", "green", "✓")
        db.close(); return 0, filtered

    total = len(jobs_to_score)
    status = Status(f"[bold green]评分中 0/{total}", console=console)
    status.start()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(_score_single_job, job, resume_summary, config, models[i % len(models)], AI_MAX_TOKENS): job
            for i, job in enumerate(jobs_to_score)
        }
        done_count = 0
        for future in as_completed(future_map):
            job = future_map[future]
            done_count += 1
            status.update(f"[bold green]评分中 {done_count}/{total}")
            tag = f"{job['company']}｜{job['title']}"
            try:
                score, reason, err, meta = future.result()
                mdl = meta.get("model", "?")
                key_info = meta.get("key", "?")  # 获取Key指纹
                att = meta.get("total_attempts", 0)
                rnd = meta.get("rounds_used", 1)
                if score is None:
                    if err == "auth":
                        status.stop()
                        _log(f"认证失败，终止评分 | {tag} | {mdl}", "red bold", "🛑")
                        for f in future_map:
                            if not f.done():
                                f.cancel()
                        break
                    skipped += 1
                    _log(f"跳过 | {tag} | {err or 'UNKNOWN'} | T{att}", "yellow", "⏭")
                    continue

                update_job_score(db, job["id"], score, reason)
                if score >= threshold:
                    update_job_status(db, job["id"], "approved"); approved += 1
                    _log(f"{score:>3} ✓ | {tag:<28} | {mdl} | {key_info} | R{rnd}/T{att} | {(reason or '')[:35]}", "green")
                else:
                    update_job_status(db, job["id"], "filtered"); filtered += 1
                    _log(f"{score:>3} ✗ | {tag:<28} | {mdl} | {key_info} | R{rnd}/T{att} | {(reason or '')[:35]}", "dim")
            except Exception as exc:
                skipped += 1
                _log(f"异常 | {tag} | {exc}", "red", "💥")

    status.stop()
    db.close()

    # ▶ 完成摘要
    _log(f"完成 | ✓{approved} ✗{filtered}(预筛{pre_filtered}) ⏭{skipped}", "green bold", "📊")
    if approved > 0:
        _log("自动衔接后续流程...", "cyan", "→")
        try:
            from bosshunter.ai.greeter import generate_greetings
            generate_greetings(config)
            from bosshunter.executor.sender import send_greetings
            send_greetings(config, force=True)
        except Exception as exc:
            _log(f"后续流程异常: {exc}", "red", "✗")
    else:
        _log("无通过岗位，跳过后续流程", "yellow", "⚠")

    return approved, filtered