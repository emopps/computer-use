import io
import json
import re
import base64
import requests
import os

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
            response = session.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                data=payload_bytes,
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"API 请求失败: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"详情: {e.response.text}")
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
    
    def __init__(self, model, api_key=None):
        super().__init__(model)
        # 支持传入不同的 API Key，方便不同模型使用不同的 Key
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY")
        if not self.local_llm and not self.api_key:
            raise ValueError(
                "OPENROUTER_API_KEY is required when using OpenRouterProvider without a local model."
            )
        trust_env = str(os.getenv("OPENROUTER_TRUST_ENV", "false") or "false").strip().lower()
        self.requests_trust_env = trust_env in {"1", "true", "yes", "on"}

    def completion(self, messages, **kwargs):
        # 本地模型不走远程API
        if self.local_llm:
            return self.local_completion(messages, **kwargs)
        # OpenRouter reasoning specific: extra_body={"reasoning": {"enabled": True}}
        if "extra_body" not in kwargs:
            kwargs["extra_body"] = {"reasoning": {"enabled": True}}
        return super().completion(messages, **kwargs)

    def transform_message(self, message):
        # Preserve reasoning_details if present
        transformed = super().transform_message(message)
        if "reasoning_details" in message:
            transformed["reasoning_details"] = message["reasoning_details"]
        return transformed

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
