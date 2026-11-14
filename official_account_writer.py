"""Generate an article draft with the OpenAI Responses API."""
import json

import requests


FIELDS = ("title", "summary", "body")
DEFAULT_MODEL = "gpt-5.4-mini"


def validate_draft(draft):
    if not isinstance(draft, dict):
        raise ValueError("草稿格式无效。")
    if any(not isinstance(draft.get(name), str) for name in FIELDS):
        raise ValueError("草稿须包含标题、摘要和正文。")
    result = {name: draft[name].strip() for name in FIELDS}
    if not result["title"] or not result["body"]:
        raise ValueError("请填写文章标题和正文。")
    return result


def generate_draft(api_key, model, topic, brief=""):
    if not api_key:
        raise ValueError("请填写 OpenAI API Key，或设置 OPENAI_API_KEY。")
    if not topic.strip() or not model.strip():
        raise ValueError("请填写文章选题和模型。")
    payload = {
        "model": model.strip(),
        "store": False,
        "instructions": (
            "你是微信公众号编辑。根据选题和写作要求撰写可供人工审阅的文章，"
            "默认用简体中文。标题控制在32字以内，摘要在120字以内，正文用纯文本和空行分段。"
            "不编造统计数据、引用、链接或人物经历。缺少支撑的时效性事实应留待编辑核实。"
        ),
        "input": json.dumps({"选题": topic.strip(), "写作要求": brief.strip()}, ensure_ascii=False),
        "max_output_tokens": 5000,
        "text": {"format": {
            "type": "json_schema",
            "name": "official_account_draft",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {name: {"type": "string"} for name in FIELDS},
                "required": list(FIELDS),
                "additionalProperties": False,
            },
        }},
    }
    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=(10, 120),
            allow_redirects=False,
        )
    except requests.Timeout:
        raise RuntimeError("OpenAI 请求超时，请稍后重试。") from None
    except requests.RequestException:
        raise RuntimeError("无法连接 OpenAI，请检查网络。") from None

    if response.status_code != 200:
        messages = {
            400: "OpenAI 拒绝了请求，请检查所选模型是否支持结构化输出。",
            401: "OpenAI 密钥验证失败，请检查 API Key。",
            403: "OpenAI 拒绝访问，请检查项目和模型权限。",
            404: "未找到所选模型，请检查模型名称和项目权限。",
            429: "OpenAI 请求受限，请检查 API 项目额度、账单或请求频率。",
        }
        raise RuntimeError(messages.get(response.status_code, f"OpenAI 请求失败（HTTP {response.status_code}）。"))

    try:
        result = response.json()
        if result.get("status") != "completed":
            raise RuntimeError("OpenAI 未完成生成，请缩短写作要求或重试。")
        parts = [part for item in result.get("output", []) if item.get("type") == "message"
                 for part in item.get("content", [])]
        if any(part.get("type") == "refusal" for part in parts):
            raise RuntimeError("该选题未生成文章，请调整写作要求。")
        content = "".join(part["text"] for part in parts if part.get("type") == "output_text")
        return validate_draft(json.loads(content))
    except (ValueError, AttributeError, KeyError, TypeError):
        raise RuntimeError("OpenAI 返回的文章格式无效，请重试。") from None
