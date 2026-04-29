import os
import re
import shutil
from typing import Any, Dict, List


class FileToolError(RuntimeError):
    pass


class FileTool:
    @staticmethod
    def list_files(directory: str, extensions: List[str] = None) -> List[str]:
        directory = os.path.expanduser(directory)
        if not os.path.exists(directory):
            raise FileToolError(f"Directory does not exist: {directory}")
        files = os.listdir(directory)
        if extensions:
            files = [name for name in files if any(name.endswith(ext) for ext in extensions)]
        return [os.path.join(directory, name) for name in files]

    @staticmethod
    def create_folder(path: str) -> str:
        path = os.path.expanduser(path)
        path = os.path.abspath(path)
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def copy_file(src: str, dst_dir: str, new_name: str = None) -> str:
        src = os.path.expanduser(src)
        dst_dir = os.path.expanduser(dst_dir)
        if not os.path.exists(src):
            raise FileToolError(f"Source file does not exist: {src}")
        os.makedirs(dst_dir, exist_ok=True)

        filename = new_name if new_name else os.path.basename(src)
        dst_path = os.path.join(dst_dir, filename)
        if os.path.isdir(src):
            if os.path.exists(dst_path):
                raise FileToolError(f"Destination already exists: {dst_path}")
            shutil.copytree(src, dst_path)
        else:
            shutil.copy2(src, dst_path)
        return dst_path

    @staticmethod
    def move_file(src: str, dst_dir: str, new_name: str = None) -> str:
        src = os.path.expanduser(src)
        dst_dir = os.path.expanduser(dst_dir)
        if not os.path.exists(src):
            raise FileToolError(f"Source file does not exist: {src}")
        os.makedirs(dst_dir, exist_ok=True)

        filename = new_name if new_name else os.path.basename(src)
        dst_path = os.path.join(dst_dir, filename)
        shutil.move(src, dst_path)
        return dst_path

    @staticmethod
    def rename_file(src: str, new_name: str) -> str:
        src = os.path.expanduser(src)
        if not os.path.exists(src):
            raise FileToolError(f"Source file does not exist: {src}")
        directory = os.path.dirname(src)
        dst = os.path.join(directory, new_name)
        os.rename(src, dst)
        return dst

    @staticmethod
    def delete_path(path: str) -> str:
        path = os.path.expanduser(path)
        path = os.path.abspath(path)
        if not os.path.exists(path):
            raise FileToolError(f"Path does not exist: {path}")
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        return path

    @staticmethod
    def parse_pdf(file_path: str) -> str:
        file_path = os.path.expanduser(file_path)
        if not os.path.exists(file_path):
            raise FileToolError(f"PDF file does not exist: {file_path}")

        try:
            import PyPDF2
        except ModuleNotFoundError as exc:
            raise FileToolError(
                "PyPDF2 is required for PDF parsing but is not installed in the current environment."
            ) from exc

        text_parts: List[str] = []
        try:
            with open(file_path, "rb") as handle:
                reader = PyPDF2.PdfReader(handle)
                for page in reader.pages:
                    text_parts.append(page.extract_text() or "")
        except Exception as exc:
            raise FileToolError(f"Failed to parse PDF {file_path}: {exc}") from exc

        text = "\n".join(text_parts).strip()
        if not text:
            raise FileToolError(f"Parsed PDF is empty: {file_path}")
        return text

    @staticmethod
    def extract_contract_elements(text: str) -> Dict[str, Any]:
        if not text or not text.strip():
            raise FileToolError("Cannot extract contract elements from empty text.")

        patterns = {
            "contract_name": [r"合同名称[:：]\s*(.+)", r"项目名称[:：]\s*(.+)"],
            "party_a": [r"甲方[:：]\s*(.+)"],
            "party_b": [r"乙方[:：]\s*(.+)"],
            "amount": [
                r"合同金额[:：]\s*([0-9]+(?:\.[0-9]+)?)",
                r"金额[:：]\s*([0-9]+(?:\.[0-9]+)?)",
            ],
            "penalty_clause": [r"(违约金[^。\n]*)"],
        }

        extracted: Dict[str, Any] = {}
        missing: List[str] = []
        for field, field_patterns in patterns.items():
            value = None
            for pattern in field_patterns:
                match = re.search(pattern, text)
                if match:
                    value = match.group(1).strip()
                    break
            if value is None:
                missing.append(field)
            else:
                extracted[field] = float(value) if field == "amount" else value

        if missing:
            raise FileToolError(
                f"Failed to extract required contract fields: {', '.join(missing)}"
            )

        return extracted
