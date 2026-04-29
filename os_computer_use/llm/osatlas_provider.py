try:
    from gradio_client import Client, handle_file
except ImportError:
    Client = None
    handle_file = None

from os_computer_use.desktop.grounding import extract_bbox_midpoint
from os_computer_use.logging import logger
import os


OSATLAS_HUGGINGFACE_SOURCE = "maxiw/OS-ATLAS"
OSATLAS_HUGGINGFACE_MODEL = "OS-Copilot/OS-Atlas-Base-7B"
OSATLAS_HUGGINGFACE_API = "/run_example"

HF_TOKEN = os.getenv("HF_TOKEN")


class OSAtlasProvider:
    """
    OS-Atlas 提供商，用于调用 OS-Atlas 模型进行 UI 元素定位。
    """

    def __init__(self):
        if Client is None:
            print("警告: 未安装 gradio_client，OSAtlasProvider 将无法工作。")
        self.client = Client(OSATLAS_HUGGINGFACE_SOURCE, hf_token=HF_TOKEN) if Client else None

    def call(self, prompt, image_data):
        result = self.client.predict(
            image=handle_file(image_data),
            text_input=prompt + "\nReturn the response in the form of a bbox",
            model_id=OSATLAS_HUGGINGFACE_MODEL,
            api_name=OSATLAS_HUGGINGFACE_API,
        )
        position = extract_bbox_midpoint(result[1])
        image_url = result[2]
        logger.log(f"bbox {image_url}", "gray")
        return position
