import os
from dotenv import load_dotenv
from os_computer_use.llm.llm_provider import (
    OpenAIBaseProvider,
    MistralBaseProvider,
    OpenRouterProvider as SharedOpenRouterProvider,
)
from os_computer_use.llm.local_hf_vision_provider import LocalHFVisionProvider
from os_computer_use.llm.local_llama_cpp_vision_provider import LocalLlamaCppVisionProvider
from os_computer_use.llm.osatlas_provider import OSAtlasProvider
from os_computer_use.llm.showui_provider import ShowUIProvider

# Load environment variables from .env file
load_dotenv()

# LLM providers use the OpenAI specification and require a base URL:


class LlamaProvider(OpenAIBaseProvider):
    base_url = "https://api.llama-api.com"
    api_key = os.getenv("LLAMA_API_KEY")
    aliases = {"llama-3.2": "llama-3.2-90b-vision", "llama-3.3": "llama-3.3-70b"}


class OpenRouterProvider(SharedOpenRouterProvider):
    aliases = {
        "llama-3.2": "meta-llama/llama-3.2-90b-vision-instruct",
        "qwen-2.5-vl": "qwen/qwen2.5-vl-72b-instruct:free",
    }


class FireworksProvider(OpenAIBaseProvider):
    base_url = "https://api.fireworks.ai/inference/v1"
    api_key = os.getenv("FIREWORKS_API_KEY")
    aliases = {
        "llama-3.2": "accounts/fireworks/models/llama-v3p2-90b-vision-instruct",
        "llama-3.3": "accounts/fireworks/models/llama-v3p3-70b-instruct",
    }


class DeepSeekProvider(OpenAIBaseProvider):
    base_url = "https://api.deepseek.com"
    api_key = os.getenv("DEEPSEEK_API_KEY")


class OpenAIProvider(OpenAIBaseProvider):
    base_url = "https://api.openai.com/v1"
    api_key = os.getenv("OPENAI_API_KEY")


class GeminiProvider(OpenAIBaseProvider):
    base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    api_key = os.getenv("GEMINI_API_KEY")


class GroqProvider(OpenAIBaseProvider):
    base_url = "https://api.groq.com/openai/v1"
    api_key = os.getenv("GROQ_API_KEY")
    aliases = {
        "llama-3.2": "llama-3.2-90b-vision-preview",
        "llama-3.3": "llama-3.3-70b-versatile",
    }


class MistralProvider(MistralBaseProvider):
    base_url = "https://api.mistral.ai/v1"
    api_key = os.getenv("MISTRAL_API_KEY")
    aliases = {
        "mistral": "mistral-large-latest",
        "pixtral": "pixtral-large-latest",
    }


class MoonshotProvider(OpenAIBaseProvider):
    base_url = "https://api.moonshot.cn/v1"
    api_key = os.getenv("MOONSHOT_API_KEY")
    aliases = {
        "moonshot-v1": "moonshot-v1-128k",
        "moonshot-v1-vision": "moonshot-v1-128k-vision-preview",
    }
