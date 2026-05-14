import io
import json
import re
import base64
import requests
import os
import time
from typing import Any, List, Optional

try:
    from llama_cpp import Llama
except ImportError:
    Llama = None

try:
    from e2b import Sandbox as SandboxBase
except ImportError:
    class SandboxBase:
        def __init__(self, *args, **kwargs):
            pass
        def kill(self):
            pass
        def set_timeout(self, timeout):
            pass

def Message(content, role="assistant"):
    return {"role": role, "content": content}

def Text(text):
    return {"type": "text", "text": text}

def parse_json(s):
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        print(f"解码工具调用参数的 JSON 时出错: {s}")
        return None

class LLMProvider:
    """
    LLM 提供商基础类，支持本地和远程模型调用。
    """
    base_url = None
    api_key = None
    aliases = {}

    def __init__(self, model):
        self.model = self.aliases.get(model, model)
        self.local_llm = None
        self.requests_trust_env = True
        
        if os.path.exists(self.model) and Llama:
            print(f"正在加载本地模型: {self.model}")
            try:
                self.local_llm = Llama(model_path=self.model, n_ctx=4096, n_gpu_layers=-1, verbose=False)
                print(f"✅ 本地模型加载成功 (n_ctx=4096)")
            except Exception as e:
                print(f"本地加载失败: {e}")
                try:
                    self.local_llm = Llama(model_path=self.model, n_ctx=4096, n_gpu_layers=0, verbose=False)
                    print(f"✅ 本地模型加载成功 (CPU模式, n_ctx=4096)")
                except Exception as e2:
                    print(f"本地加载失败(CPU): {e2}")
        
        if not self.local_llm:
            print(f"正在使用 {self.__class__.__name__} 模型: {self.model}")

    def create_function_schema(self, definitions):
        functions = []
        for name, details in definitions.items():
            properties = {}
            required = []
            for param_name, param_desc in details["params"].items():
                properties[param_name] = {"type": "string", "description": param_desc}
                required.append(param_name)
            function_def = self.create_function_def(name, details, properties, required)
            functions.append(function_def)
        return functions

    def create_tool_call(self, name, parameters):
        return {
            "type": "function",
            "name": name,
            "parameters": parameters,
        }

    def wrap_block(self, block):
        if isinstance(block, bytes):
            return self.create_image_block(block)
        else:
            return Text(block)

    def transform_message(self, message):
        content = message["content"]
        if isinstance(content, list):
            wrapped_content = [self.wrap_block(block) for block in content]
            return {**message, "content": wrapped_content}
        else:
            return message

    def completion(self, messages, **kwargs):
        if self.local_llm:
            return self.local_completion(messages, **kwargs)
        
        new_messages = [self.transform_message(message) for message in messages]
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": self.model,
            "messages": new_messages,
            **{k: v for k, v in kwargs.items() if v is not None}
        }
        payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        # 增加超时时间到 120s 以应对复杂模型的推理时间
        timeout = kwargs.get("timeout", 120)

        try:
            headers["Content-Type"] = "application/json; charset=utf-8"
            session = requests.Session()
            session.trust_env = bool(getattr(self, "requests_trust_env", True))
            request_url = f"{self.base_url}/chat/completions"
            print(
                "[LLM] provider={} model={} url={} timeout={} trust_env={}".format(
                    self.__class__.__name__,
                    self.model,
                    request_url,
                    timeout,
                    session.trust_env,
                )
            )
            response = session.post(
                request_url,
                headers=headers,
                data=payload_bytes,
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"API 请求失败: {e}")
            if hasattr(e, 'response') and e.response is not None:
                status_code = getattr(e.response, "status_code", "unknown")
                response_text = str(getattr(e.response, "text", "") or "").strip()
                if len(response_text) > 800:
                    response_text = response_text[:800] + "..."
                print(f"状态码: {status_code}")
                print(f"详情: {response_text}")
            raise Exception(f"调用模型失败: {str(e)}")

    def local_completion(self, messages, **kwargs):
        # 针对本地 Llama 模型的简单 Chat Completion 模拟
        prompt = ""
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            prompt += f"<|im_start|>{role}\n{content}<|im_end|>\n"
        prompt += "<|im_start|>assistant\n"
        
        response = self.local_llm(
            prompt,
            max_tokens=kwargs.get("max_tokens", 512),
            stop=["<|im_end|>"],
            echo=False
        )
        
        text = response["choices"][0]["text"]
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": text
                }
            }]
        }

class OpenAIBaseProvider(LLMProvider):
    def create_function_def(self, name, details, properties, required):
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": details["description"],
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def create_image_block(self, image_data: bytes):
        from PIL import Image
        image_type = "png"
        try:
            with Image.open(io.BytesIO(image_data)) as img:
                image_type = img.format.lower()
        except Exception as e:
            print(f"检测图片类型时出错: {e}")

        encoded = base64.b64encode(image_data).decode("utf-8")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:image/{image_type};base64,{encoded}"},
        }

    def call(self, messages, functions=None):
        tools = self.create_function_schema(functions) if functions else None
        response = self.completion(messages, tools=tools)
        
        if "error" in response:
            raise Exception(f"调用模型时出错: {response['error']}")
            
        choice = response["choices"][0]
        message = choice["message"]
        content = message.get("content")

        if functions:
            tool_calls = message.get("tool_calls", [])
            combined_tool_calls = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                args = parse_json(fn.get("arguments", "{}"))
                if args is not None:
                    combined_tool_calls.append(self.create_tool_call(fn.get("name"), args))

            if content and not combined_tool_calls:
                tool_call_matches = re.search(r"\{.*\}", content)
                if tool_call_matches:
                    tool_call = parse_json(tool_call_matches.group(0))
                    parameters = tool_call.get("parameters", tool_call.get("arguments"))
                    if tool_call.get("name") and parameters:
                        combined_tool_calls.append(self.create_tool_call(tool_call.get("name"), parameters))
                        return None, combined_tool_calls

            return content, combined_tool_calls
        else:
            return content

class AnthropicBaseProvider(LLMProvider):
    # 如果后续需要支持国产的类 Anthropic 接口，可以在此实现
    # 目前国产 API 大多兼容 OpenAI 格式
    pass

class OpenRouterProvider(OpenAIBaseProvider):
    base_url = "https://openrouter.ai/api/v1"
    
    def __init__(self, model, api_key=None, enable_reasoning=True):
        super().__init__(model)
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY")
        self.enable_reasoning = bool(enable_reasoning)
        if not self.local_llm and not self.api_key:
            raise ValueError(
                "OPENROUTER_API_KEY is required when using OpenRouterProvider without a local model."
            )
        trust_env = str(os.getenv("OPENROUTER_TRUST_ENV", "false") or "false").strip().lower()
        self.requests_trust_env = trust_env in {"1", "true", "yes", "on"}

    def completion(self, messages, **kwargs):
        if self.local_llm:
            return self.local_completion(messages, **kwargs)
        last_error = None
        for reasoning_enabled in self._reasoning_attempts():
            for endpoint_index, base_url in enumerate(self._candidate_base_urls()):
                previous_base_url = self.base_url
                self.base_url = base_url
                attempt_kwargs = dict(kwargs)
                extra_body = dict(attempt_kwargs.get("extra_body") or {})
                if reasoning_enabled:
                    extra_body["reasoning"] = {"enabled": True}
                else:
                    extra_body.pop("reasoning", None)
                if extra_body:
                    attempt_kwargs["extra_body"] = extra_body
                else:
                    attempt_kwargs.pop("extra_body", None)
                self._log_openrouter_attempt(
                    phase="start",
                    model=self.model,
                    base_url=base_url,
                    reasoning_enabled=reasoning_enabled,
                    timeout=attempt_kwargs.get("timeout", 120),
                )
                try:
                    return self._completion_with_retries(
                        messages,
                        model_name=self.model,
                        base_url=base_url,
                        reasoning_enabled=reasoning_enabled,
                        **attempt_kwargs,
                    )
                except Exception as exc:
                    last_error = exc
                    self._log_openrouter_attempt(
                        phase="failed",
                        model=self.model,
                        base_url=base_url,
                        reasoning_enabled=reasoning_enabled,
                        error=str(exc),
                    )
                    if endpoint_index < len(self._candidate_base_urls()) - 1:
                        time.sleep(1.0 + endpoint_index)
                finally:
                    self.base_url = previous_base_url
        raise last_error or Exception("OpenRouter request failed.")

    def _completion_with_retries(self, messages, model_name=None, base_url=None, reasoning_enabled=None, **kwargs):
        last_error = None
        for attempt in range(3):
            try:
                self._log_openrouter_attempt(
                    phase="request",
                    model=model_name or self.model,
                    base_url=base_url or self.base_url,
                    reasoning_enabled=bool(reasoning_enabled),
                    retry=attempt + 1,
                )
                return super().completion(messages, **kwargs)
            except Exception as exc:
                last_error = exc
                message = str(exc)
                should_retry = any(
                    token in message
                    for token in [
                        "500 Server Error",
                        "502 Server Error",
                        "503 Server Error",
                        "504 Server Error",
                        "429 Client Error",
                        "Failed to establish a new connection",
                        "Network is unreachable",
                        "网络不可达",
                        "Read timed out",
                        "ConnectTimeout",
                    ]
                )
                if not should_retry or attempt >= 2:
                    raise
                time.sleep(1.5 * (attempt + 1))
        raise last_error or Exception("OpenRouter request failed.")

    def transform_message(self, message):
        # Preserve reasoning_details if present
        transformed = super().transform_message(message)
        if "reasoning_details" in message:
            transformed["reasoning_details"] = message["reasoning_details"]
        return transformed

    def _candidate_base_urls(self):
        custom = str(os.getenv("OPENROUTER_BASE_URL", "") or "").strip()
        candidates = []
        if custom:
            candidates.append(custom.rstrip("/"))
        candidates.extend(
            [
                "https://openrouter.ai/api/v1",
                "https://www.openrouter.ai/api/v1",
            ]
        )
        unique = []
        for item in candidates:
            if item and item not in unique:
                unique.append(item)
        return unique

    def _reasoning_attempts(self) -> List[bool]:
        return [bool(self.enable_reasoning)]

    def _log_openrouter_attempt(
        self,
        *,
        phase: str,
        model: str,
        base_url: str,
        reasoning_enabled: bool,
        timeout: Optional[Any] = None,
        retry: Optional[int] = None,
        error: str = "",
    ) -> None:
        parts = [
            "[OpenRouter]",
            f"phase={phase}",
            f"model={model}",
            f"endpoint={base_url}",
            f"reasoning={'on' if reasoning_enabled else 'off'}",
        ]
        if timeout is not None:
            parts.append(f"timeout={timeout}")
        if retry is not None:
            parts.append(f"retry={retry}")
        if error:
            compact_error = str(error).replace("\n", " ").strip()
            if len(compact_error) > 400:
                compact_error = compact_error[:400] + "..."
            parts.append(f"error={compact_error}")
        print(" ".join(parts))

    def call(self, messages, functions=None):
        tools = self.create_function_schema(functions) if functions else None
        response = self.completion(messages, tools=tools)
        
        if "error" in response:
            raise Exception(f"调用 OpenRouter 模型时出错: {response['error']}")
            
        choice = response["choices"][0]
        message = choice["message"]
        content = message.get("content")
        
        # Support reasoning_details in the response
        reasoning_details = message.get("reasoning_details")
        
        if functions:
            tool_calls = message.get("tool_calls", [])
            combined_tool_calls = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                args = parse_json(fn.get("arguments", "{}"))
                if args is not None:
                    combined_tool_calls.append(self.create_tool_call(fn.get("name"), args))

            if content and not combined_tool_calls:
                tool_call_matches = re.search(r"\{.*\}", content)
                if tool_call_matches:
                    tool_call = parse_json(tool_call_matches.group(0))
                    parameters = tool_call.get("parameters", tool_call.get("arguments"))
                    if tool_call.get("name") and parameters:
                        combined_tool_calls.append(self.create_tool_call(tool_call.get("name"), parameters))
                        return None, combined_tool_calls

            return content, combined_tool_calls
        else:
            # If there's reasoning, we might want to log it or handle it, 
            # but for now we follow the project's 'call' return pattern.
            if reasoning_details:
                print(f"Reasoning: {reasoning_details}")
            return content

class MistralBaseProvider(OpenAIBaseProvider):
    def call(self, messages, functions=None):
        # 兼容处理助手消息前缀
        if messages and messages[-1].get("role") == "assistant":
            prefix = messages.pop()["content"]
            if messages and messages[-1].get("role") == "user":
                messages[-1]["content"] = prefix + "\n" + messages[-1].get("content", "")
            else:
                messages.append({"role": "user", "content": prefix})
        return super().call(messages, functions)
