"""Run on the client; obtain the platform Key from your own application settings."""
import os

import httpx
from openai import OpenAI

with OpenAI(
    api_key=os.environ['AI_GATEWAY_API_KEY'],
    base_url=os.environ.get('AI_GATEWAY_BASE_URL', 'http://127.0.0.1:8094/v1'),
    max_retries=0,
    http_client=httpx.Client(trust_env=False),
) as client:
    result = client.chat.completions.create(
        model=os.environ['AI_GATEWAY_MODEL'],
        messages=[{'role': 'user', 'content': '你好，请介绍你的能力。'}],
        max_completion_tokens=1024,
    )
    print(result.choices[0].message.content)
