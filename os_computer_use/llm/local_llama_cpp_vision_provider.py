from __future__ import annotations

import base64
import io
import json
from typing import Any, Dict, List, Optional

import requests


class LocalLlamaCppVisionProvider:
    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8080/v1",
        model: Optional[str] = None,
        max_new_tokens: int = 192,
        timeout: int = 180,
    ):
        self.base_url = str(base_url).rstrip("/")
        self.model = model
        self.max_new_tokens = int(max_new_tokens)
        self.timeout = int(timeout)

    def call(self, messages: List[Dict[str, Any]]) -> str:
        prompt_text, image_blocks = self._extract_prompt_and_images(messages)
        if not image_blocks:
            raise ValueError(
                "LocalLlamaCppVisionProvider requires at least one image bytes block."
            )

        content: List[Dict[str, Any]] = []
        if prompt_text:
            content.append({"type": "text", "text": prompt_text})
        for image_bytes in image_blocks:
            content.append(self._image_block(image_bytes))

        payload: Dict[str, Any] = {
            "messages": [
                {
                    "role": "user",
                    "content": content,
                }
            ],
            "max_tokens": self.max_new_tokens,
            "temperature": 0,
        }
        if self.model:
            payload["model"] = self.model

        last_error: Optional[str] = None
        for endpoint in self._candidate_endpoints():
            try:
                response = requests.post(
                    endpoint,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=self.timeout,
                )
                response.raise_for_status()
                body = response.json()
                return self._extract_text(body)
            except requests.HTTPError as exc:
                last_error = self._format_http_error(exc)
                if exc.response is not None and exc.response.status_code != 404:
                    raise RuntimeError(last_error) from exc
            except requests.RequestException as exc:
                last_error = "Failed to call llama.cpp vision server: {}".format(exc)
                raise RuntimeError(last_error) from exc
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                last_error = "Malformed llama.cpp response: {}".format(exc)
                raise RuntimeError(last_error) from exc

        raise RuntimeError(
            last_error
            or "Could not find a working llama.cpp chat/completions endpoint."
        )

    def _candidate_endpoints(self) -> List[str]:
        if self.base_url.endswith("/chat/completions"):
            return [self.base_url]
        if self.base_url.endswith("/v1"):
            return [
                self.base_url + "/chat/completions",
                self.base_url[:-3] + "/chat/completions",
            ]
        return [
            self.base_url + "/v1/chat/completions",
            self.base_url + "/chat/completions",
        ]

    @staticmethod
    def _extract_prompt_and_images(
        messages: List[Dict[str, Any]],
    ) -> (str, List[bytes]):
        prompt_parts: List[str] = []
        image_blocks: List[bytes] = []

        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, bytes):
                        image_blocks.append(block)
                    elif isinstance(block, dict):
                        block_type = str(block.get("type", "") or "")
                        if block_type == "text":
                            prompt_parts.append(str(block.get("text", "") or ""))
                        elif block_type == "image_url":
                            image_url = block.get("image_url") or {}
                            maybe_url = image_url.get("url")
                            if isinstance(maybe_url, str) and maybe_url.startswith("data:image/"):
                                image_blocks.append(
                                    LocalLlamaCppVisionProvider._decode_data_url(maybe_url)
                                )
                    else:
                        prompt_parts.append(str(block))
            elif content is not None:
                prompt_parts.append(str(content))

        prompt_text = "\n".join(part for part in prompt_parts if part).strip()
        return prompt_text, image_blocks

    @staticmethod
    def _decode_data_url(data_url: str) -> bytes:
        _, encoded = data_url.split(",", 1)
        return base64.b64decode(encoded)

    @staticmethod
    def _image_block(image_bytes: bytes) -> Dict[str, Any]:
        image_type = "png"
        try:
            from PIL import Image

            with Image.open(io.BytesIO(image_bytes)) as img:
                image_type = str(img.format or "PNG").lower()
        except Exception:
            pass

        encoded = base64.b64encode(image_bytes).decode("utf-8")
        return {
            "type": "image_url",
            "image_url": {"url": "data:image/{};base64,{}".format(image_type, encoded)},
        }

    @staticmethod
    def _extract_text(body: Dict[str, Any]) -> str:
        choice = body["choices"][0]
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            text_parts: List[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(str(block.get("text", "") or ""))
            return "\n".join(part for part in text_parts if part).strip()
        return str(content or "").strip()

    @staticmethod
    def _format_http_error(exc: requests.HTTPError) -> str:
        response = exc.response
        if response is None:
            return "llama.cpp HTTP error: {}".format(exc)
        details = response.text.strip()
        if details:
            return "llama.cpp server error {}: {}".format(response.status_code, details)
        return "llama.cpp server error {}".format(response.status_code)
