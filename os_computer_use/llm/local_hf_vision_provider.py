from __future__ import annotations

import io
import os
from typing import Any, Dict, List


class LocalHFVisionProvider:
    def __init__(
        self,
        model_dir: str,
        *,
        cpu_only: bool = True,
        longest_edge: int = 512,
        max_new_tokens: int = 192,
    ):
        self.model_dir = model_dir
        self.cpu_only = cpu_only
        self.longest_edge = int(longest_edge)
        self.max_new_tokens = int(max_new_tokens)
        self._processor = None
        self._model = None
        self._torch = None
        self._device = None

    def _ensure_loaded(self) -> None:
        if self._processor is not None and self._model is not None:
            return
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

        import torch
        from transformers import AutoModelForVision2Seq
        from transformers import AutoProcessor

        use_cuda = torch.cuda.is_available() and not self.cpu_only
        device = "cuda" if use_cuda else "cpu"
        dtype = torch.bfloat16 if use_cuda else torch.float32
        attn_impl = "flash_attention_2" if use_cuda else "eager"

        self._processor = AutoProcessor.from_pretrained(
            self.model_dir,
            trust_remote_code=True,
            size={"longest_edge": self.longest_edge},
        )
        self._model = AutoModelForVision2Seq.from_pretrained(
            self.model_dir,
            torch_dtype=dtype,
            _attn_implementation=attn_impl,
            trust_remote_code=True,
        ).to(device)
        self._torch = torch
        self._device = device

    def call(self, messages: List[Dict[str, Any]]) -> str:
        self._ensure_loaded()

        from PIL import Image

        prompt_parts: List[str] = []
        image = None

        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, bytes):
                        image = Image.open(io.BytesIO(block)).convert("RGB")
                    else:
                        prompt_parts.append(str(block))
            else:
                prompt_parts.append(str(content or ""))

        if image is None:
            raise ValueError("LocalHFVisionProvider requires at least one image bytes block.")

        prompt_text = "\n".join(part for part in prompt_parts if part).strip()
        chat_messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image"},
                    {"type": "text", "text": prompt_text},
                ],
            }
        ]

        prompt = self._processor.apply_chat_template(
            chat_messages,
            add_generation_prompt=True,
        )
        inputs = self._processor(
            text=prompt,
            images=[image],
            return_tensors="pt",
        )
        inputs = {key: value.to(self._device) for key, value in inputs.items()}

        with self._torch.inference_mode():
            generated_ids = self._model.generate(
                **inputs,
                max_new_tokens=self.max_new_tokens,
                do_sample=False,
            )

        text = self._processor.batch_decode(
            generated_ids,
            skip_special_tokens=True,
        )[0]
        return self._postprocess_output(str(text))

    @staticmethod
    def _postprocess_output(text: str) -> str:
        cleaned = str(text or "").strip()
        if "Assistant:" in cleaned:
            cleaned = cleaned.split("Assistant:", 1)[-1].strip()
        if "<|im_start|>assistant" in cleaned:
            cleaned = cleaned.split("<|im_start|>assistant", 1)[-1].strip()
        return cleaned
