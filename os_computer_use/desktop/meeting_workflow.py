from __future__ import annotations

import json
import os
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List


@dataclass
class MailDeliveryConfig:
    mode: str
    host: str
    port: int
    username: str
    password: str
    sender: str
    use_ssl: bool
    use_starttls: bool


class MeetingWorkflowError(RuntimeError):
    pass


class MeetingWorkflow:
    def __init__(self, provider: Any = None, artifact_root: str = "") -> None:
        self.provider = provider
        self.artifact_root = Path(artifact_root or ".").resolve()

    def extract_actions(self, instruction: str, meeting_page: Dict[str, Any]) -> Dict[str, Any]:
        raw_text = str(instruction or "").strip()
        page = dict(meeting_page or {})
        body_text = str(page.get("body_text", "") or "").strip()
        summary_text = str(page.get("summary_text", "") or "").strip()
        transcript_text = str(page.get("transcript_text", "") or "").strip()
        if not body_text:
            raise MeetingWorkflowError("Failed to read the real meeting page content from browser.")

        email_map = self._extract_email_map(raw_text)
        source_url = self._clean_source_url(str(page.get("url", "") or self._extract_field(raw_text, "转写文件")).strip())
        meeting_title = str(page.get("meeting_title", "") or self._extract_field(raw_text, "转写")).strip()
        meeting_date = str(page.get("meeting_date", "") or self._extract_field(raw_text, "日期")).strip()
        speaker_turns = self._parse_speaker_turns(transcript_text)
        if not speaker_turns:
            raise MeetingWorkflowError("Failed to extract speaker turns from the transcript panel.")
        grounded_transcript = self._format_speaker_turns(speaker_turns)

        parsed = self._extract_with_model(
            meeting_title=meeting_title,
            meeting_date=meeting_date,
            source_url=source_url,
            summary_text=summary_text,
            transcript_text=transcript_text,
            grounded_transcript=grounded_transcript,
            email_map=email_map,
        )
        assignments = self._ensure_assignments_cover_email_map(
            self._normalize_assignments(parsed.get("assignments", []), email_map),
            email_map,
        )
        if not assignments:
            raise MeetingWorkflowError("No meeting recipients or actionable assignments were extracted from the transcript.")

        overall_summary = str(parsed.get("overall_summary", "") or summary_text or "").strip()
        artifact_dir = self.artifact_root / "meeting_demo"
        drafts_dir = artifact_dir / "email_drafts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        drafts_dir.mkdir(parents=True, exist_ok=True)

        markdown = self._build_minutes_markdown(
            meeting_title=meeting_title,
            meeting_date=meeting_date,
            source_url=source_url,
            overall_summary=overall_summary,
            assignments=assignments,
        )
        summary_path = artifact_dir / "meeting_minutes.md"
        summary_path.write_text(markdown, encoding="utf-8")

        payload = {
            "meeting_title": meeting_title,
            "meeting_date": meeting_date,
            "source_url": source_url,
            "overall_summary": overall_summary,
            "assignments": assignments,
            "email_map": email_map,
            "page_title": str(page.get("page_title", "") or ""),
            "summary_text": summary_text[:12000],
            "transcript_text": transcript_text[:32000],
            "grounded_transcript": grounded_transcript[:32000],
            "body_text_excerpt": body_text[:12000],
            "artifact_dir": str(artifact_dir),
            "summary_path": str(summary_path),
        }
        assignments_path = artifact_dir / "task_assignments.json"
        assignments_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["assignments_path"] = str(assignments_path)

        draft_files: List[str] = []
        for index, assignment in enumerate(assignments, start=1):
            subject = self._build_mail_subject(meeting_title, assignment["owner"], has_tasks=bool(assignment.get("tasks")))
            body = self._build_mail_body(
                meeting_title=meeting_title,
                meeting_date=meeting_date,
                source_url=source_url,
                overall_summary=overall_summary,
                assignment=assignment,
            )
            safe_owner = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", assignment["owner"]).strip("_") or f"user_{index}"
            draft_path = drafts_dir / f"{index:02d}_{safe_owner}.txt"
            draft_path.write_text(f"To: {assignment.get('email', '')}\nSubject: {subject}\n\n{body}", encoding="utf-8")
            draft_files.append(str(draft_path))
            assignment["mail_subject"] = subject
            assignment["mail_body"] = body
            assignment["draft_path"] = str(draft_path)

        payload["draft_files"] = draft_files
        assignments_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    def send_assignments(self, extracted: Dict[str, Any]) -> Dict[str, Any]:
        artifact_dir = Path(str(extracted.get("artifact_dir", "") or self.artifact_root / "meeting_demo"))
        artifact_dir.mkdir(parents=True, exist_ok=True)
        preview_dir = artifact_dir / "mail_preview"
        preview_dir.mkdir(parents=True, exist_ok=True)
        assignments = self._ensure_assignments_cover_email_map(
            self._normalize_assignments(extracted.get("assignments", []), extracted.get("email_map", {})),
            extracted.get("email_map", {}),
        )
        if not assignments:
            raise MeetingWorkflowError("No meeting assignments available for email delivery.")

        config = self._load_mail_config()
        delivery_results: List[Dict[str, Any]] = []
        for index, assignment in enumerate(assignments, start=1):
            owner = assignment["owner"]
            email = str(assignment.get("email", "") or "").strip()
            if not email:
                raise MeetingWorkflowError(
                    "Meeting assignment email is missing for {}. Please supplement mappings like 张三=zhangsan@example.com.".format(owner)
                )
            subject = str(
                assignment.get("mail_subject", "")
                or self._build_mail_subject(
                    extracted.get("meeting_title", ""),
                    owner,
                    has_tasks=bool(assignment.get("tasks")),
                )
            )
            body = str(
                assignment.get("mail_body", "")
                or self._build_mail_body(
                    meeting_title=str(extracted.get("meeting_title", "") or ""),
                    meeting_date=str(extracted.get("meeting_date", "") or ""),
                    source_url=self._clean_source_url(str(extracted.get("source_url", "") or "")),
                    overall_summary=str(extracted.get("overall_summary", "") or ""),
                    assignment=assignment,
                )
            )
            message = EmailMessage()
            message["To"] = email
            message["From"] = config.sender or config.username or "noreply@example.com"
            message["Subject"] = subject
            message.set_content(body)

            safe_owner = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", owner).strip("_") or f"user_{index}"
            eml_path = preview_dir / f"{index:02d}_{safe_owner}.eml"
            eml_path.write_bytes(message.as_bytes())
            if config.mode == "smtp":
                self._send_via_smtp(config, message)
            delivery_results.append(
                {
                    "owner": owner,
                    "email": email,
                    "subject": subject,
                    "preview_path": str(eml_path),
                    "status": "sent" if config.mode == "smtp" else "previewed",
                }
            )
        manifest = {"delivery_mode": config.mode, "meeting_title": extracted.get("meeting_title", ""), "results": delivery_results}
        manifest_path = artifact_dir / "mail_delivery.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "delivery_mode": config.mode,
            "sent_count": sum(1 for item in delivery_results if item["status"] == "sent"),
            "preview_count": sum(1 for item in delivery_results if item["status"] == "previewed"),
            "results": delivery_results,
            "manifest_path": str(manifest_path),
        }

    def _extract_with_model(
        self,
        *,
        meeting_title: str,
        meeting_date: str,
        source_url: str,
        summary_text: str,
        transcript_text: str,
        grounded_transcript: str,
        email_map: Dict[str, str],
    ) -> Dict[str, Any]:
        if self.provider is None:
            raise MeetingWorkflowError("Meeting extraction requires a configured reasoning model.")
        prompt = (
            "You are extracting owner-specific action items from a real Tencent Meeting transcript page.\n"
            "Use the transcript as the primary source of truth and use the meeting summary only as secondary context.\n"
            "Do not invent tasks not grounded in the transcript speaker turns.\n"
            "Return strict JSON only with keys meeting_title, meeting_date, source_url, overall_summary, assignments.\n"
            "Each assignment must include owner, email, tasks, deadline, notes.\n"
            "Only include explicit or strongly implied action items from the transcript. "
            "If transcript and summary conflict, always prefer the transcript. "
            "If the transcript has no explicit owner for a task, you may use the meeting summary as secondary evidence only when the summary explicitly names the owner and the follow-up action clearly belongs to that owner's own presentation or discussion.\n"
            "Write overall_summary, tasks, deadline, and notes entirely in Simplified Chinese.\n"
            "Translate English transcript content into natural Simplified Chinese, but preserve person names, URLs, paper titles, and technical acronyms exactly when needed.\n"
            "Keep each owner's tasks strictly separated. Never place one person's task under another person's assignment.\n"
            "If a task clearly refers to a known mapped owner by name, assign it to that owner instead of the current speaker.\n"
            "When the provided email mappings mention multiple owners, keep them as separate assignment entries whenever the transcript contains tasks for them.\n"
            "If the Known email mappings dictionary is non-empty, treat it as the authoritative recipient allowlist for outbound assignment emails: "
            "only include assignment owners who appear in that mapping as recipients for mail delivery; "
            "do not add extra owners solely because they appear in the transcript unless they are also in the mapping.\n\n"
            f"Known email mappings:\n{json.dumps(email_map, ensure_ascii=False)}\n\n"
            f"Meeting title: {meeting_title}\n"
            f"Meeting date: {meeting_date}\n"
            f"Meeting URL: {source_url}\n\n"
            f"Structured speaker turns:\n{grounded_transcript[:32000]}\n\n"
            f"Raw transcript section:\n{transcript_text[:32000]}\n\n"
            f"Summary section:\n{summary_text[:12000]}"
        )
        try:
            response = self.provider.call([{"role": "user", "content": prompt}])
            parsed = self._extract_json_object(response)
            if isinstance(parsed, dict):
                parsed.setdefault("meeting_title", meeting_title)
                parsed.setdefault("meeting_date", meeting_date)
                parsed.setdefault("source_url", source_url)
                parsed["source_url"] = self._clean_source_url(str(parsed.get("source_url", "") or source_url))
                return parsed
        except Exception as exc:
            raise MeetingWorkflowError("Failed to extract meeting assignments from transcript: {}".format(exc)) from exc
        raise MeetingWorkflowError("Model did not return a valid meeting assignment JSON payload.")

    def _load_mail_config(self) -> MailDeliveryConfig:
        explicit_mode = str(os.getenv("OCU_ASSIGNMENT_MAIL_MODE", "") or "").strip().lower()
        host = str(os.getenv("OCU_SMTP_HOST", "") or "").strip()
        mode = explicit_mode or ("smtp" if host else "preview")
        port = int(str(os.getenv("OCU_SMTP_PORT", "465") or "465"))
        return MailDeliveryConfig(
            mode=mode if mode in {"smtp", "preview"} else "preview",
            host=host,
            port=port,
            username=str(os.getenv("OCU_SMTP_USERNAME", "") or "").strip(),
            password=str(os.getenv("OCU_SMTP_PASSWORD", "") or "").strip(),
            sender=str(os.getenv("OCU_SMTP_FROM", "") or "").strip(),
            use_ssl=str(os.getenv("OCU_SMTP_SSL", "true") or "true").strip().lower() in {"1", "true", "yes", "on"},
            use_starttls=str(os.getenv("OCU_SMTP_STARTTLS", "false") or "false").strip().lower() in {"1", "true", "yes", "on"},
        )

    def _send_via_smtp(self, config: MailDeliveryConfig, message: EmailMessage) -> None:
        if not config.host or not config.username or not config.password:
            raise MeetingWorkflowError("SMTP delivery requires OCU_SMTP_HOST / OCU_SMTP_USERNAME / OCU_SMTP_PASSWORD.")
        if config.use_ssl:
            with smtplib.SMTP_SSL(config.host, config.port, timeout=30) as server:
                server.login(config.username, config.password)
                server.send_message(message)
            return
        with smtplib.SMTP(config.host, config.port, timeout=30) as server:
            if config.use_starttls:
                server.starttls()
            server.login(config.username, config.password)
            server.send_message(message)

    def _normalize_assignments(self, assignments: Any, email_map: Dict[str, str]) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        seen = set()
        for item in assignments or []:
            if not isinstance(item, dict):
                continue
            owner = str(item.get("owner", "") or "").strip()
            if not owner:
                continue
            tasks = []
            raw_tasks = item.get("tasks", []) or []
            if isinstance(raw_tasks, str):
                raw_tasks = [raw_tasks]
            for task in raw_tasks:
                text = str(task or "").strip().lstrip("-").strip()
                if text and text not in tasks:
                    tasks.append(text)
            deadline = str(item.get("deadline", "") or "").strip()
            notes = str(item.get("notes", "") or "").strip()
            email = email_map.get(owner, "") or str(item.get("email", "") or "").strip()
            if not tasks and not deadline and not notes and not email:
                continue
            if owner in seen:
                existing = next(entry for entry in normalized if entry["owner"] == owner)
                for task in tasks:
                    if task not in existing["tasks"]:
                        existing["tasks"].append(task)
                if not existing.get("email"):
                    existing["email"] = email
                if not existing.get("deadline"):
                    existing["deadline"] = deadline
                if not existing.get("notes"):
                    existing["notes"] = notes
                continue
            seen.add(owner)
            normalized.append(
                {
                    "owner": owner,
                    "email": email,
                    "tasks": tasks,
                    "deadline": deadline,
                    "notes": notes,
                }
            )
        return normalized

    @staticmethod
    def _ensure_assignments_cover_email_map(assignments: List[Dict[str, Any]], email_map: Dict[str, str]) -> List[Dict[str, Any]]:
        normalized = list(assignments or [])
        seen = {str(item.get("owner", "") or "").strip() for item in normalized}
        for owner, email in (email_map or {}).items():
            clean_owner = str(owner or "").strip()
            clean_email = str(email or "").strip()
            if not clean_owner or clean_owner in seen:
                continue
            normalized.append(
                {
                    "owner": clean_owner,
                    "email": clean_email,
                    "tasks": [],
                    "deadline": "",
                    "notes": "",
                }
            )
            seen.add(clean_owner)
        return normalized

    @staticmethod
    def _clean_source_url(text: str) -> str:
        payload = str(text or "").strip()
        if not payload:
            return ""
        match = re.search(r"https?://[^\s\u3000,\uFF0C\u3002\uFF1B;\"'<>]+", payload, flags=re.IGNORECASE)
        if not match:
            return payload
        return match.group(0).rstrip("，。；;\"'》〉】）)")

    def _extract_email_map(self, text: str) -> Dict[str, str]:
        mappings: Dict[str, str] = {}
        line_patterns = [
            re.compile(r"([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9_\-·]{0,20})\s*[=:：]\s*([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})"),
            re.compile(r"([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9_\-·]{0,20})\s*<([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>"),
        ]
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            for pattern in line_patterns:
                for name, email in pattern.findall(line):
                    mappings[str(name).strip()] = str(email).strip()
        return mappings

    def _build_minutes_markdown(self, *, meeting_title: str, meeting_date: str, source_url: str, overall_summary: str, assignments: List[Dict[str, Any]]) -> str:
        lines = [f"# {meeting_title or '会议纪要任务分配'}", "", f"- 日期：{meeting_date or '未提供'}", f"- 转写链接：{source_url or '未提供'}", ""]
        if overall_summary:
            lines.extend(["## 总结", "", overall_summary, ""])
        lines.extend(["## 任务分配", ""])
        for item in assignments:
            lines.append(f"### {item['owner']}")
            for task in item["tasks"]:
                lines.append(f"- {task}")
            if item.get("deadline"):
                lines.append(f"- 截止时间：{item['deadline']}")
            if item.get("notes"):
                lines.append(f"- 备注：{item['notes']}")
            if item.get("email"):
                lines.append(f"- 邮箱：{item['email']}")
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    def _build_mail_subject(self, meeting_title: str, owner: str, has_tasks: bool = True) -> str:
        title = meeting_title or "会议纪要"
        prefix = "[任务分配]" if has_tasks else "[会议纪要]"
        return f"{prefix} {title} - {owner}"

    def _build_mail_body(self, *, meeting_title: str, meeting_date: str, source_url: str, overall_summary: str, assignment: Dict[str, Any]) -> str:
        tasks = [str(task or "").strip() for task in (assignment.get("tasks", []) or []) if str(task or "").strip()]
        if tasks:
            lines = [f"{assignment['owner']}，你好：", "", f"以下是会议《{meeting_title or '会议纪要'}》中分配给你的事项：", ""]
        else:
            lines = [
                f"{assignment['owner']}，你好：",
                "",
                f"以下是会议《{meeting_title or '会议纪要'}》纪要：",
                "",
                "本次会议未提取到分配给你的明确待办事项，现将会议纪要发送给你供参考。",
                "",
            ]
        for index, task in enumerate(tasks, start=1):
            lines.append(f"{index}. {task}")
        if assignment.get("deadline"):
            lines.extend(["", f"截止时间：{assignment['deadline']}"])
        if assignment.get("notes"):
            lines.extend(["", f"备注：{assignment['notes']}"])
        if overall_summary:
            lines.extend(["", "会议摘要：", overall_summary])
        if meeting_date or source_url:
            lines.append("")
            if meeting_date:
                lines.append(f"会议时间：{meeting_date}")
            if source_url:
                lines.append(f"转写链接：{source_url}")
        if tasks:
            lines.extend(["", "请按上述事项推进，并及时同步进展。", "", "系统自动生成"])
        else:
            lines.extend(["", "如有需要，可基于本纪要继续补充后续行动。", "", "系统自动生成"])
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _extract_json_object(text: Any) -> Dict[str, Any]:
        payload = str(text or "").strip()
        if not payload:
            return {}
        try:
            parsed = json.loads(payload)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            pass
        match = re.search(r"\{.*\}", payload, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}

    @staticmethod
    def _extract_field(text: str, field_name: str) -> str:
        pattern = re.compile(rf"{re.escape(field_name)}\s*[:：]\s*(.+)")
        for line in text.splitlines():
            match = pattern.search(line)
            if match:
                return match.group(1).strip()
        return ""

    def _parse_speaker_turns(self, transcript_text: str) -> List[Dict[str, str]]:
        text = re.sub(r"\s+", " ", str(transcript_text or "")).strip()
        if not text:
            return []
        pattern = re.compile(
            r"(?P<speaker>[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9_\-·]{0,20})\s*"
            r"(?P<time>\d{1,2}:\d{2})\s+"
            r"(?P<content>.*?)(?=(?:[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9_\-·]{0,20}\s*\d{1,2}:\d{2}\s+)|$)"
        )
        turns: List[Dict[str, str]] = []
        for match in pattern.finditer(text):
            speaker = str(match.group("speaker") or "").strip()
            time_text = str(match.group("time") or "").strip()
            content = str(match.group("content") or "").strip()
            if not speaker or not content:
                continue
            turns.append({"speaker": speaker, "time": time_text, "content": content[:1600]})
        return turns

    @staticmethod
    def _format_speaker_turns(turns: List[Dict[str, str]]) -> str:
        lines: List[str] = []
        for item in turns[:120]:
            speaker = str(item.get("speaker", "") or "").strip()
            time_text = str(item.get("time", "") or "").strip()
            content = str(item.get("content", "") or "").strip()
            if not speaker or not content:
                continue
            lines.append(f"{speaker} {time_text}: {content}")
        return "\n".join(lines)
