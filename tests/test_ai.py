"""
双模型 AI 管线测试（pytest 兼容）
PyCharm 右键 Run / pytest tests/test_ai.py -v -s
"""

import json
import time

import httpx
import pytest

# ─── 双模型配置 ─────────────────────────────────────────────
MODELS = [
    {
        "name": "deepseek-v4-flash",
        "base_url": "https://token.sensenova.cn/v1",
        "api_key": "sk-pcn6lz9VTuWFJ9UeOIZvAPBr2bIItpqE",
        "model": "deepseek-v4-flash",
    },
    {
        "name": "agnes-2.5-flash",
        "base_url": "https://apihub.agnes-ai.com/v1",
        "api_key": "sk-e6ffPzEYl73RFCQ4MqU7ooDFSGmJ1avqvs0z4H8AEgZXeRoA",
        "model": "agnes-2.5-flash",
    },
]

MODEL_IDS = [m["name"] for m in MODELS]

# ─── 假数据 ─────────────────────────────────────────────────
FAKE_RESUME = """# 张三 - UI/UX设计师
- 5年UI设计经验，3年车载HMI设计
- 精通Figma、Sketch、Principle
- 主导过某品牌智能座舱HMI改版项目
- 熟悉Android Automotive、QNX平台
- 某大学 视觉传达设计 本科"""

FAKE_JOBS = [
    {
        "title": "智能座舱HMI设计师",
        "company": "某科技公司",
        "salary": "12-16K",
        "jd": "负责智能座舱HMI界面设计，熟悉车载交互系统，3年以上UI设计经验，精通Figma。",
    },
    {
        "title": "Java后端开发",
        "company": "某互联网公司",
        "salary": "15-25K",
        "jd": "负责后端微服务开发，精通Spring Cloud、MySQL、Redis，5年以上经验。",
    },
    {
        "title": "座舱产品经理",
        "company": "某车企",
        "salary": "11-14K",
        "jd": "负责智能座舱产品规划，了解车载系统生态，2年以上产品经验。",
    },
]

THRESHOLD = 60


# ─── 工具 ───────────────────────────────────────────────────
def call_api(cfg: dict, messages: list[dict], max_tokens: int = 500) -> str:
    """调用 OpenAI 兼容接口，返回模型文本内容。"""
    resp = httpx.post(
        f"{cfg['base_url'].rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        },
        json={
            "model": cfg["model"],
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.7,
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()

    choice = data["choices"][0]
    msg = choice["message"]

    # 优先取 content
    content = msg.get("content") or ""

    # 某些推理模型把正文放在 reasoning_content 字段
    if not content.strip():
        content = msg.get("reasoning_content") or ""

    # 仍然为空 → 打印原始响应便于排查
    if not content.strip():
        print(f"\n  ⚠️ [{cfg['name']}] content 为空，原始响应:")
        print(f"  {json.dumps(data, ensure_ascii=False, indent=2)[:1000]}")

    return content.strip()


# ─── 测试用例 ───────────────────────────────────────────────
@pytest.mark.parametrize("cfg", MODELS, ids=MODEL_IDS)
class TestAI:
    """双模型测试套件"""

    def test_connection(self, cfg):
        """API 可达性测试"""
        t0 = time.time()
        reply = call_api(cfg, [{"role": "user", "content": "回复OK"}], max_tokens=50)
        elapsed = time.time() - t0
        print(f"\n  [{cfg['name']}] 响应: \"{reply}\" ({elapsed:.1f}s)")
        assert len(reply) > 0

    def test_scoring(self, cfg):
        """评分：3个岗位匹配度"""
        for job in FAKE_JOBS:
            prompt = f"""你是求职顾问。根据简历和岗位给匹配度评分(0-100)和一句话理由。

## 简历
{FAKE_RESUME}

## 岗位
- 职位：{job['title']} | 公司：{job['company']} | 薪资：{job['salary']}
- JD：{job['jd']}

严格输出JSON，不要输出任何其他内容：{{"score": 75, "reason": "理由"}}"""

            reply = call_api(
                cfg,
                [{"role": "user", "content": prompt}],
                max_tokens=500,
            )

            # 空回复时重试一次
            if not reply:
                time.sleep(2)
                reply = call_api(
                    cfg,
                    [{"role": "user", "content": prompt}],
                    max_tokens=500,
                )

            s, e = reply.find("{"), reply.rfind("}") + 1
            assert s != -1, f"未返回JSON: {reply!r}"

            data = json.loads(reply[s:e])
            score = int(data["score"])
            status = "approved" if score >= THRESHOLD else "filtered"
            print(
                f"\n  [{cfg['name']}] {job['title']} → "
                f"{score}分 ({status}) | {data['reason']}"
            )
            assert 0 <= score <= 100

    def test_greeting(self, cfg):
        """招呼语生成"""
        job = FAKE_JOBS[0]
        prompt = f"""你是求职者，在BOSS直聘给HR发打招呼消息。

## 我的背景
{FAKE_RESUME}

## 目标岗位
- {job['title']} | {job['company']} | {job['salary']}
- JD：{job['jd']}

## 要求
- 50-150字，像真人IM消息
- 突出1-2个匹配优势
- 不要"您好，我是"开头
- 不捏造经历

直接输出招呼语："""

        greeting = call_api(
            cfg,
            [{"role": "user", "content": prompt}],
            max_tokens=300,
        )
        print(f"\n  [{cfg['name']}] 招呼语({len(greeting)}字): \"{greeting}\"")
        assert 10 <= len(greeting) <= 200
        assert not greeting.startswith("您好，我是")