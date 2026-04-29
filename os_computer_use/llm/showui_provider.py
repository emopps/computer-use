import ast
from datetime import datetime
from PIL import Image, ImageDraw
try:
    from gradio_client import Client, handle_file
except ImportError:
    Client = None
    handle_file = None

from os_computer_use.llm.llm_provider import OpenAIBaseProvider
import os
try:
    import numpy as np
except ImportError:
    np = None

SHOWUI_HUGGINGFACE_SOURCE = "showlab/ShowUI"
SHOWUI_HUGGINGFACE_MODEL = "showlab/ShowUI-2B"
SHOWUI_HUGGINGFACE_API = "/on_submit"

class ShowUIProvider(OpenAIBaseProvider):
    """
    The ShowUI provider is used to make calls to ShowUI.
    """

    def __init__(self):
        if Client is None:
            print("警告: 未安装 gradio_client，ShowUIProvider 将无法工作。")
        self.client = Client(SHOWUI_HUGGINGFACE_SOURCE) if Client else None

    def extract_norm_point(self, response, image_url):
        if isinstance(image_url, str):
            image = Image.open(image_url)
        else:
            if np is None:
                raise ImportError("未安装 numpy，无法处理数组类型的图片数据")
            image = Image.fromarray(np.uint8(image_url))
        
        point = ast.literal_eval(response)
        if len(point) == 2:
            x, y = point[0] * image.width, point[1] * image.height
            return x, y
        else:
            return None

    def call(self, prompt, image_data):
        result = self.client.predict(
            image=handle_file(image_data),
            query=prompt,
            iterations=1,
            is_example_image="False",
            api_name=SHOWUI_HUGGINGFACE_API,
        )
        pred = result[1]
        img_url = result[0][0]['image']
        result = self.extract_norm_point(pred, img_url)
        return result

if __name__ == "__main__":
    showuiprovider = ShowUIProvider()
    img_url = "/home/qinghong/example/demo/chrome.png"
    query = "search box"
    result = showuiprovider.call(query, img_url)
    print(result)
