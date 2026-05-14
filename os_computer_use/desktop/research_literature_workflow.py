from __future__ import annotations

import json
import os
import re
import zipfile
from urllib.parse import quote
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape


DEFAULT_HEADERS: Tuple[str, ...] = ("方向", "标题", "作者", "发表年份")
DEFAULT_DIRECTION_LIMIT = 4
DEFAULT_PAPERS_PER_DIRECTION = 5
BAIDU_SCHOLAR_HOME_URL = "https://xueshu.baidu.com/"
BAIDU_SCHOLAR_SEARCH_URL_TEMPLATE = "https://xueshu.baidu.com/ndscholar/browse/search?wd={query}"
SCHOLAR_SEARCH_TEXTAREA_SELECTOR = "textarea.atomic-textarea-box.search-input"
SCHOLAR_SEARCH_TEXTAREA_FALLBACKS: Tuple[str, ...] = (
    "textarea.search-input",
    "div.atomic-textarea.search-input textarea",
)
SCHOLAR_LITERATURE_MODE_SELECTORS: Tuple[str, ...] = (
    "text=文献搜索",
    "a:has-text('文献搜索')",
    "button:has-text('文献搜索')",
    "[role='tab']:has-text('文献搜索')",
)
SCHOLAR_SUBMIT_METHOD = "direct_search_url"
BAIDU_SCHOLAR_SELECTOR_PROBE_JS = r"""
(selectors) => {
  const toText = (node) => String(node?.innerText || node?.textContent || '').replace(/\s+/g, ' ').trim();
  const visible = (node) => {
    if (!node) return false;
    const r = node.getBoundingClientRect();
    return !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length) && r.width > 10 && r.height > 10;
  };
  return selectors.map((sel) => {
    const nodes = Array.from(document.querySelectorAll(sel)).slice(0, 5);
    return {
      selector: sel,
      count: document.querySelectorAll(sel).length,
      matches: nodes.map((n, i) => {
        const r = n.getBoundingClientRect();
        return {
          index: i,
          tag: String(n.tagName || ''),
          id: String(n.id || ''),
          className: String(n.className || ''),
          text: toText(n).slice(0, 120),
          visible: visible(n),
          top: Math.round(r.top),
          left: Math.round(r.left),
          width: Math.round(r.width),
          height: Math.round(r.height),
        };
      }),
    };
  });
}
"""
# 与 scripts/vm_dom_probe_baidu_scholar.py 中 SNAPSHOT_JS 保持一致（页面 DOM 抽取逻辑）
BAIDU_SCHOLAR_PROBE_SNAPSHOT_JS = r"""
() => {
  const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
  const noiseRe = /智能助手|AI高级搜索|AI学术搜索|选题推荐|文献总结|以上内容由AI生成|仅供参考|DeepSeek-R1|输入内容或链接|选择文献|按相关性/i;
  const toText = (node) => String(node?.innerText || node?.textContent || '').replace(/\s+/g, ' ').trim();
  const toLines = (node) => String(node?.innerText || node?.textContent || '')
      .split(/\r?\n+/)
      .map((line) => String(line || '').replace(/\s+/g, ' ').trim())
      .filter(Boolean);
  const visible = (node) => {
    if (!node) return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 40 && rect.height > 10 && rect.bottom > 80;
  };
  const items = [];
  const seen = new Set();
  const personLikeRe = /^(?:[\u4e00-\u9fff]{2,4}|[A-Za-z][A-Za-z .'\-]{1,40})(?:\s*[，,、· ]\s*(?:[\u4e00-\u9fff]{2,4}|[A-Za-z][A-Za-z .'\-]{1,40})){0,9}$/;
  const authorLineLike = (line) => {
    if (!line || noiseRe.test(line)) return false;
    if (/^(期刊|学位|会议|专利|图书|报纸)$/.test(line)) return false;
    if (/^《.+》$/.test(line)) return false;
    if (/来源|被引量|摘要|关键词|doi|收藏|引用|论文图谱|AI问答/i.test(line)) return false;
    if (/(方法|研究|模型|系统|接口|训练|优化|增强|分类|诊疗|数据|网络|设备|装置)/.test(line)) return false;
    if (personLikeRe.test(line)) return true;
    return /^[\u4e00-\u9fff]{2,4}[，,、]?$/.test(line);
  };
  const extractAuthors = (lines, title) => {
    const titleIndex = Math.max(0, lines.findIndex((line) => line === title));
    const trailingLines = titleIndex >= 0 ? lines.slice(titleIndex + 1) : lines.slice(1);
    const collected = [];
    let started = false;
    for (const line of trailingLines) {
      if (!line) continue;
      if (/^(期刊|学位|会议|专利|图书|报纸)$/.test(line) && !started) continue;
      if (/^[-–—]+$/.test(line) || /^《.+》$/.test(line) || /来源|被引量|\b(?:19|20)\d{2}\b/i.test(line)) {
        if (started) break;
        continue;
      }
      if (authorLineLike(line)) {
        started = true;
        collected.push(line);
        continue;
      }
      if (started) break;
    }
    const merged = collected
      .map((item) => item.replace(/[，,、\s]+$/g, '').trim())
      .filter(Boolean)
      .join('， ')
      .replace(/[，,、]\s*[，,、]\s*/g, '， ')
      .replace(/\s+/g, ' ')
      .replace(/[，,、]\s*\.\.\.$/, '')
      .replace(/[，,、]\s*…+$/, '')
      .trim();
    return merged.replace(/[，,、\s]+$/g, '').trim();
  };
  const pushBlock = (root, selector) => {
    if (!root || !visible(root)) return;
    const rootRect = root.getBoundingClientRect();
    if (rootRect.left > viewportWidth * 0.72 || rootRect.width < viewportWidth * 0.22 || rootRect.top < 100) return;
    const rawLines = toLines(root);
    const blockText = rawLines.join('\n');
    if (!blockText || noiseRe.test(blockText)) return;
    if (!(/来源|被引量|\b(?:19|20)\d{2}\b/i.test(blockText) || (/收藏/.test(blockText) && /引用/.test(blockText)))) return;
    const titleNode = root.querySelector('h3 a, h3, a[title], a, [class*="title"]');
    const title = toText(titleNode);
    if (!title || title.length < 6 || title.length > 220 || noiseRe.test(title)) return;
    if (/^[A-Za-z0-9._-]{1,24}$/.test(title)) return;
    if (seen.has(title)) return;
    seen.add(title);
    const lines = rawLines.map((line) => line.replace(/\s+/g, ' ').trim()).filter(Boolean);
    const titleIndex = Math.max(0, lines.findIndex((line) => line === title));
    const trailingLines = titleIndex >= 0 ? lines.slice(titleIndex + 1) : lines.slice(1);
    const authorsLine = extractAuthors(lines, title);
    const yearLine = trailingLines.find((line) => /\b(?:19|20)\d{2}\b/.test(line)) || '';
    const meta = trailingLines.filter((line) => /来源|被引量|\b(?:19|20)\d{2}\b/i.test(line)).join(' ; ');
    const abstract = trailingLines.find((line) => {
      if (!line || line === authorsLine) return false;
      return !/收藏|引用|论文图谱|AI问答|来源|被引量/i.test(line);
    }) || '';
    items.push({
      selector,
      title,
      authors: authorsLine.slice(0, 200),
      year: (yearLine.match(/\b(?:19|20)\d{2}\b/) || [])[0] || '',
      meta: meta.slice(0, 400),
      abstract: abstract.slice(0, 400),
      text: blockText.slice(0, 1200),
      top: Math.round(rootRect.top),
      left: Math.round(rootRect.left),
    });
  };
  const cardSelectors = ['[class*="result"]', '[class*="Result"]', 'article', 'section', 'li', 'div'];
  for (const selector of cardSelectors) {
    const roots = Array.from(document.querySelectorAll(selector));
    for (const root of roots.slice(0, 120)) {
      pushBlock(root, selector);
    }
    if (items.length >= 8) break;
  }
  return {
    url: String(location.href || ''),
    title: String(document.title || ''),
    items: items.slice(0, 12),
    bodyPreview: String(document.body?.innerText || '').slice(0, 1600),
  };
}
"""
_NOISE_TEXT_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"智能助手"),
    re.compile(r"AI高级搜索", re.I),
    re.compile(r"AI学术搜索", re.I),
    re.compile(r"选题推荐"),
    re.compile(r"文献总结"),
    re.compile(r"以上内容由AI生成"),
    re.compile(r"仅供参考"),
    re.compile(r"DeepSeek-R1", re.I),
    re.compile(r"输入内容或链接"),
    re.compile(r"选择文献"),
    re.compile(r"按相关性"),
    re.compile(r"搜索完成，但没有提取到足够可靠的结果"),
    re.compile(r"搜索完成，但当前没有可读取的页面"),
    re.compile(r"window\.__NUXT__\s*=", re.I),
)
_TITLE_METADATA_SPLIT_RE = re.compile(r"[；;。]|来源[:：]|摘要[:：]|关键词[:：]|被引量[:：]")
_AUTHOR_STOPWORDS_RE = re.compile(
    r"(标题|作者|摘要|关键词|被引量|来源|DOI|doi|收藏|引用|论文图谱|AI问答|专利号|公开号|公开日|申请人|申请号)"
)
_AUTHOR_TECHNICAL_TERMS_RE = re.compile(
    r"(方法|研究|模型|系统|装置|接口|数据|网络|算法|诊疗|图谱|设备|训练|优化|分类|增强|微调|学习|分析|模态|知识库|神经|通信)"
)

_BAIDU_SCHOLAR_SITE_RE = re.compile(r"\bsite:(?:www\.)?xueshu\.baidu\.com\b", re.I)
_SPREADSHEET_PATH_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"((?:/|~/|[A-Za-z]:\\)[^\s,\uFF0C\u3002\uFF1B;\"']+\.(?:xlsx|xls|et))", re.I),
    re.compile(r"([^\s,\uFF0C\u3002\uFF1B;\"']+\.(?:xlsx|xls|et))", re.I),
)


class ResearchLiteratureWorkflowError(RuntimeError):
    pass


class ResearchLiteratureWorkflow:
    def __init__(self, desktop: Any, provider: Any = None, artifact_root: str = "") -> None:
        self.desktop = desktop
        self.provider = provider
        self.artifact_root = Path(artifact_root or ".").resolve()

    async def collect_literature(self, instruction: str, file_path: str = "") -> Dict[str, Any]:
        raw_instruction = str(instruction or "").strip()
        if not raw_instruction:
            raise ResearchLiteratureWorkflowError("文献收集任务缺少指令内容。")

        topic = extract_research_topic(raw_instruction)
        if not topic:
            raise ResearchLiteratureWorkflowError("未能从指令中提取检索主题。")
        self._debug_log("文献主题：{}".format(topic))

        explicit_output = file_path or extract_spreadsheet_target(raw_instruction)
        if not explicit_output:
            raise ResearchLiteratureWorkflowError("请在指令中明确给出 xlsx 文件路径。")
        output_path = resolve_output_path(explicit_output)
        self._debug_log("目标表格：{}".format(output_path))
        directions = self._plan_directions(topic, raw_instruction)
        if not directions:
            raise ResearchLiteratureWorkflowError("未能规划出有效的检索方向。")
        self._debug_log(
            "已规划 {} 个检索方向：{}".format(
                len(directions),
                " | ".join("{}=>{}".format(item.get("direction", ""), item.get("query", "")) for item in directions),
            )
        )

        artifact_dir = self.artifact_root / "research_literature_demo"
        artifact_dir.mkdir(parents=True, exist_ok=True)

        collected_rows: List[Dict[str, str]] = []
        search_records: List[Dict[str, Any]] = []
        seen_titles = set()

        for direction in directions:
            direction_name = str(direction.get("direction", "") or "").strip()
            query = normalize_search_query(direction.get("query", "") or topic)
            engine = str(direction.get("engine", "") or "baidu_scholar").strip() or "baidu_scholar"
            self._debug_log("开始检索方向「{}」：{}".format(direction_name or topic, query))

            probe = getattr(self.desktop, "probe_baidu_scholar", None)
            if callable(probe):
                probe_report = await probe(query)
                snapshot = normalize_baidu_scholar_probe_snapshot((probe_report or {}).get("snapshot", {}))
                search_output = snapshot
            else:
                search_output = await self.desktop.baidu_scholar_search(query)
                snapshot = normalize_baidu_scholar_probe_snapshot(await self._capture_current_page_snapshot())
            scholar_results = extract_scholar_results(snapshot, search_output)
            self._debug_log("方向「{}」结果区命中 {} 个候选块。".format(direction_name or topic, len(scholar_results)))
            page_text = sanitize_scholar_page_text(
                str((snapshot or {}).get("scholarMainText", "") or (snapshot or {}).get("text", "") or "")
            )
            visible_text = self._extract_visible_text(search_output)
            papers = self._extract_papers_from_text(
                topic=topic,
                direction_name=direction_name,
                query=query,
                page_text=page_text,
                visible_text=visible_text,
                scholar_results=scholar_results,
            )

            normalized_papers: List[Dict[str, str]] = []
            for item in papers[:DEFAULT_PAPERS_PER_DIRECTION]:
                normalized = sanitize_paper_candidate(item)
                title = normalized.get("title", "")
                authors = normalized.get("authors", "")
                year = normalized.get("year", "")
                if not title or title in seen_titles:
                    continue
                seen_titles.add(title)
                row = {
                    "方向": direction_name or topic,
                    "标题": title,
                    "作者": authors,
                    "发表年份": year,
                }
                normalized_papers.append(row)
                collected_rows.append(row)
            self._debug_log(
                "方向「{}」抽取到 {} 篇候选论文。".format(direction_name or topic, len(normalized_papers))
            )

            search_records.append(
                {
                    "direction": direction_name,
                    "query": query,
                    "engine": engine,
                    "visible_text": visible_text[:4000],
                    "page_text": page_text[:4000],
                    "scholar_results": scholar_results[:8],
                    "paper_count": len(normalized_papers),
                    "papers": normalized_papers,
                }
            )

        if not collected_rows:
            raise ResearchLiteratureWorkflowError("已完成检索，但未解析出可写入表格的论文条目。")
        self._debug_log("总计抽取 {} 篇论文，准备交给 spreadsheet.write_cell 写入。".format(len(collected_rows)))

        payload = {
            "topic": topic,
            "file_path": str(output_path),
            "direction_count": len(directions),
            "paper_count": len(collected_rows),
            "headers": list(DEFAULT_HEADERS),
            "directions": directions,
            "records": search_records,
            "rows": collected_rows,
            "hardcoded_elements": hardcoded_elements_summary(),
        }
        manifest_path = artifact_dir / "literature_collection.json"
        manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["manifest_path"] = str(manifest_path)
        self._debug_log("文献结构化结果已保存：{}".format(manifest_path))
        return payload

    async def _capture_current_page_snapshot(self) -> Dict[str, Any]:
        capture = getattr(self.desktop, "_capture_search_page_snapshot", None)
        if capture is None:
            return {}
        try:
            snapshot = await capture()
            return snapshot if isinstance(snapshot, dict) else {}
        except Exception:
            return {}

    def _plan_directions(self, topic: str, instruction: str) -> List[Dict[str, str]]:
        planned = self._plan_directions_with_model(topic, instruction)
        if planned:
            return planned[:DEFAULT_DIRECTION_LIMIT]
        return heuristic_direction_plan(topic)[:DEFAULT_DIRECTION_LIMIT]

    def _plan_directions_with_model(self, topic: str, instruction: str) -> List[Dict[str, str]]:
        if self.provider is None:
            return []
        prompt = (
            "你是学术文献检索助手。\n"
            "给定一个模糊研究主题，请规划 3 到 4 个互不重复的检索方向。\n"
            "返回严格 JSON 数组，每个元素包含 direction、query、engine。\n"
            "direction 用简体中文描述方向；query 给出适合百度学术检索的中文或中英混合关键词；"
            "engine 固定写 baidu_scholar。\n"
            "不要输出 markdown，不要解释，不要编造与主题无关的方向。\n\n"
            f"原始指令：{instruction}\n"
            f"研究主题：{topic}\n"
        )
        try:
            response = self.provider.call([{"role": "user", "content": prompt}])
        except Exception:
            return []
        self._debug_log("方向规划模型输出：{}".format(str(response or "").strip()[:600]))
        parsed = extract_json_array(response)
        normalized: List[Dict[str, str]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            direction = str(item.get("direction", "") or "").strip()
            query = normalize_search_query(item.get("query", ""))
            if not direction or not query:
                continue
            normalized.append(
                {
                    "direction": direction,
                    "query": query,
                    "engine": "baidu_scholar",
                }
            )
        return normalized

    def _extract_papers_from_text(
        self,
        *,
        topic: str,
        direction_name: str,
        query: str,
        page_text: str,
        visible_text: str,
        scholar_results: List[Dict[str, str]],
    ) -> List[Dict[str, str]]:
        dom_papers = extract_papers_from_scholar_results(scholar_results)
        if dom_papers:
            return dom_papers[:DEFAULT_PAPERS_PER_DIRECTION]
        heuristic_papers = heuristic_extract_papers(page_text or visible_text)
        if heuristic_papers:
            return heuristic_papers[:DEFAULT_PAPERS_PER_DIRECTION]
        return []

    def _extract_papers_with_model(
        self,
        *,
        topic: str,
        direction_name: str,
        query: str,
        page_text: str,
        visible_text: str,
        scholar_results: List[Dict[str, str]],
    ) -> List[Dict[str, str]]:
        if self.provider is None:
            return []
        prompt_text = format_scholar_results_for_prompt(scholar_results)
        page_section = prompt_text or page_text[:6000]
        prompt = (
            "你要从学术搜索结果页面文本中抽取论文信息。\n"
            "返回严格 JSON 数组，每个元素必须包含 title、authors、year。\n"
            "只保留页面中明确出现的论文，不要编造；year 只保留四位年份；"
            "authors 不确定时可以留空字符串。\n"
            "最多返回 5 篇。\n"
            "必须忽略右侧智能助手、AI 问答、选题推荐、文献总结、AI 高级搜索、"
            "“以上内容由AI生成，仅供参考”等非论文内容。\n"
            "不要输出 markdown，不要解释。\n\n"
            f"主题：{topic}\n"
            f"方向：{direction_name}\n"
            f"检索词：{query}\n\n"
            f"候选结果：\n{page_section}\n\n"
            f"提炼文本：\n{visible_text[:4000]}\n"
        )
        try:
            response = self.provider.call([{"role": "user", "content": prompt}])
        except Exception:
            return []
        self._debug_log(
            "论文抽取模型输出（方向：{}）：{}".format(direction_name or topic, str(response or "").strip()[:600])
        )
        parsed = extract_json_array(response)
        normalized: List[Dict[str, str]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            normalized_item = sanitize_paper_candidate(item)
            if not normalized_item:
                continue
            normalized.append(normalized_item)
        return normalized

    @staticmethod
    def _extract_visible_text(search_output: Any) -> str:
        if isinstance(search_output, dict):
            if search_output.get("scholarMainText"):
                return sanitize_scholar_page_text(search_output.get("scholarMainText", ""))
            return sanitize_scholar_page_text(
                str(search_output.get("raw_text", "") or search_output.get("text", "") or "").strip()
            )
        return sanitize_scholar_page_text(search_output)

    def _debug_log(self, message: str) -> None:
        text = str(message or "").strip()
        if not text:
            return
        print("[OCU][research] {}".format(text))
        emit_progress = getattr(self.desktop, "_emit_progress", None)
        if callable(emit_progress):
            try:
                emit_progress("task", status="research_progress", summary=text)
            except Exception:
                pass


def baidu_scholar_site_in_query(query: str) -> Tuple[bool, str]:
    """若检索词含 site:xueshu.baidu.com，返回 (True, 去掉 site 后的关键词)，否则 (False, 原串)。"""
    q = str(query or "").strip()
    if not q or not _BAIDU_SCHOLAR_SITE_RE.search(q):
        return False, q
    cleaned = _BAIDU_SCHOLAR_SITE_RE.sub(" ", q)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return True, cleaned or q


def build_baidu_scholar_search_url(query: str) -> str:
    text = str(query or "").strip()
    return BAIDU_SCHOLAR_SEARCH_URL_TEMPLATE.format(query=quote(text))


def normalize_baidu_scholar_probe_snapshot(snapshot: Any) -> Dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {
            "url": "",
            "title": "",
            "items": [],
            "bodyPreview": "",
            "scholarResults": [],
            "scholarMainText": "",
            "text": "",
            "html": "",
        }
    items = list(snapshot.get("items", []) or snapshot.get("scholarResults", []) or [])
    body_preview = str(snapshot.get("bodyPreview", "") or snapshot.get("scholarMainText", "") or snapshot.get("text", "") or "")
    normalized = dict(snapshot)
    normalized["items"] = items
    normalized["bodyPreview"] = body_preview
    normalized["scholarResults"] = items
    normalized["scholarMainText"] = str(snapshot.get("scholarMainText", "") or body_preview)
    normalized["text"] = str(snapshot.get("text", "") or body_preview)
    normalized["html"] = str(snapshot.get("html", "") or "")
    return normalized


def is_baidu_scholar_verification_snapshot(snapshot: Any) -> bool:
    normalized = normalize_baidu_scholar_probe_snapshot(snapshot)
    current_url = str(normalized.get("url", "") or "")
    current_title = str(normalized.get("title", "") or "")
    body = str(normalized.get("bodyPreview", "") or "")
    blob = "\n".join([current_url, current_title, body]).lower()
    tokens = [
        "百度安全验证",
        "请完成安全验证",
        "校验失败，请再试一次",
        "captcha",
        "verify",
    ]
    if any(token.lower() in blob for token in tokens):
        return True
    return "wappass.baidu.com" in current_url.lower()


def is_research_literature_table_scenario(instruction: str) -> bool:
    """是否为「学术/论文检索 + 汇总到表格」类任务。"""
    text = str(instruction or "").strip()
    if not text:
        return False
    lowered = text.lower()
    lit = any(
        k in text or k in lowered
        for k in (
            "论文",
            "学术",
            "文献",
            "scholar",
            "谷歌学术",
            "google 学术",
            "google scholar",
            "百度学术",
            "xueshu.baidu",
            "arxiv",
        )
    )
    collect = any(k in text for k in ("搜索", "检索", "查找", "收集", "搜集", "资料", "调研", "查询"))
    tab = any(
        k in lowered or k in text
        for k in ("excel", "表格", "wps", "单元格", "写入", ".xlsx", ".et", "xlsx", "电子表")
    )
    return bool(lit and collect and tab)


def extract_spreadsheet_target(instruction: str) -> str:
    text = str(instruction or "").strip()
    if not text:
        return ""
    for pattern in _SPREADSHEET_PATH_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1).strip().rstrip(" ,.;:'\"")
    return ""


def extract_research_topic(instruction: str) -> str:
    text = str(instruction or "").strip()
    if not text:
        return ""
    normalized = text.replace("Google Scholar", "Google 学术").replace("google scholar", "google 学术")
    patterns = [
        r"(?:搜索|检索|查找|搜集|收集)(?:和|与)?(.+?)(?:有关|相关)的论文",
        r"(?:搜索|检索|查找|搜集|收集)(.+?)论文",
        r"(?:关于|围绕)(.+?)(?:的论文|论文)",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            topic = clean_topic_text(match.group(1))
            if topic:
                return topic
    return clean_topic_text(normalized)


def clean_topic_text(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"（.*?）|\(.*?\)", " ", value)
    value = re.sub(
        r"(帮我|请|在|上|如果|虚拟机|里面|上不去|可以|用|然后|再|把|各方向|找到的内容|写入|当前目录|文件夹|总结|标题|作者|发表年份)",
        " ",
        value,
    )
    value = re.sub(r"(google 学术|谷歌学术|百度学术|excel|wps|xlsx|et|site:xueshu\.baidu\.com)", " ", value, flags=re.I)
    value = re.sub(r"^[\"'“”‘’`]+", " ", value)
    value = re.sub(r"^[和与]\s*", " ", value)
    value = re.sub(r"[\"'“”‘’`]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" ，,。；;：:")
    return value


def heuristic_direction_plan(topic: str) -> List[Dict[str, str]]:
    base = topic or "研究主题"
    suffixes = [
        ("基础方法", f"{base} 基础方法"),
        ("参数高效微调", f"{base} PEFT LoRA"),
        ("指令微调", f"{base} 指令微调 instruction tuning"),
        ("训练与对齐", f"{base} 偏好对齐 RLHF DPO"),
    ]
    planned: List[Dict[str, str]] = []
    seen_queries = set()
    for direction, query in suffixes:
        query = normalize_search_query(query)
        if query in seen_queries:
            continue
        seen_queries.add(query)
        planned.append(
            {
                "direction": direction,
                "query": query,
                "engine": "baidu_scholar",
            }
        )
    return planned


def normalize_year(value: Any) -> str:
    match = re.search(r"(19|20)\d{2}", str(value or ""))
    return match.group(0) if match else ""


def normalize_search_query(value: Any) -> str:
    query = clean_topic_text(str(value or ""))
    query = re.sub(r"^[\"'“”‘’`]+", "", query)
    query = re.sub(r"^[和与]\s*", "", query)
    query = re.sub(r"\s+", " ", query).strip(" ，,。；;：:")
    return query


def contains_noise_text(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _NOISE_TEXT_PATTERNS)


def sanitize_scholar_page_text(value: Any) -> str:
    payload = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    if not payload.strip():
        return ""
    clean_lines: List[str] = []
    seen = set()
    for raw_line in payload.split("\n"):
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line or contains_noise_text(line):
            continue
        if line in seen:
            continue
        seen.add(line)
        clean_lines.append(line)
    return "\n".join(clean_lines)


def is_probable_paper_title(value: Any) -> bool:
    title = re.sub(r"\s+", " ", str(value or "")).strip(" ：:;；，,。")
    if len(title) < 6 or len(title) > 220:
        return False
    if re.match(r"^(作者|来源)[:：]?", title):
        return False
    if contains_noise_text(title):
        return False
    if re.search(r"window\.__NUXT__\s*=", title, re.I):
        return False
    if re.match(r"^\d+\s*(北大核心期刊|中国科技核心期刊|CSCD\s*索引|CSSCI\s*索引|免费下载|登录查看|计算机科学与技术|生物医学工程|控制科学与工程)\b", title, re.I):
        return False
    if re.match(r"^(北大核心期刊|中国科技核心期刊|CSCD\s*索引|CSSCI\s*索引|免费下载|登录查看)$", title, re.I):
        return False
    if re.match(r"^(领域|核心|获取方式|类型|时间|确认|期刊|学位|会议)\b", title):
        return False
    if re.fullmatch(r"[A-Za-z0-9._-]{1,24}", title):
        return False
    if title in {"登录", "注册", "收藏", "引用", "批量引用", "论文图谱", "AI问答"}:
        return False
    return bool(re.search(r"[一-鿿A-Za-z]", title))


def sanitize_authors(value: Any) -> str:
    return normalize_author_text(value)


def sanitize_paper_candidate(item: Any) -> Dict[str, str]:
    if not isinstance(item, dict):
        return {}
    title = re.sub(r"\s+", " ", str(item.get("title", "") or "")).strip()
    title = _TITLE_METADATA_SPLIT_RE.split(title, maxsplit=1)[0].strip()
    if not is_probable_paper_title(title):
        return {}
    authors = sanitize_authors(item.get("authors", ""))
    year = normalize_year(item.get("year", ""))
    return {"title": title, "authors": authors, "year": year}


def merge_paper_candidates(*groups: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    merged: List[Dict[str, str]] = []
    seen = set()
    for group in groups:
        for item in list(group or []):
            normalized = sanitize_paper_candidate(item)
            title = normalized.get("title", "")
            if not title or title in seen:
                continue
            seen.add(title)
            merged.append(normalized)
            if len(merged) >= DEFAULT_PAPERS_PER_DIRECTION:
                return merged
    return merged


def extract_scholar_results(snapshot: Any, search_output: Any) -> List[Dict[str, str]]:
    candidates: List[Any] = []
    if isinstance(snapshot, dict):
        candidates.extend(list(snapshot.get("scholarResults", []) or []))
    if isinstance(search_output, dict):
        candidates.extend(list(search_output.get("scholarResults", []) or []))

    normalized: List[Dict[str, str]] = []
    seen = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        title = re.sub(r"\s+", " ", str(item.get("title", "") or "")).strip()
        if not is_probable_paper_title(title) or title in seen:
            continue
        seen.add(title)
        normalized.append(
            {
                "title": title,
                "authors": sanitize_scholar_page_text(item.get("authors", "")),
                "year": normalize_year(item.get("year", "")),
                "meta": sanitize_scholar_page_text(item.get("meta", "")),
                "abstract": sanitize_scholar_page_text(item.get("abstract", "")),
                "text": sanitize_scholar_page_text(item.get("text", "")),
            }
        )
    return normalized


def format_scholar_results_for_prompt(results: Sequence[Dict[str, str]]) -> str:
    blocks: List[str] = []
    for index, item in enumerate(list(results or [])[:8], start=1):
        title = str(item.get("title", "") or "").strip()
        if not title:
            continue
        block_lines = [f"结果{index}", f"标题：{title}"]
        if item.get("meta"):
            block_lines.append("元信息：{}".format(item["meta"]))
        if item.get("abstract"):
            block_lines.append("摘要：{}".format(item["abstract"]))
        elif item.get("text"):
            block_lines.append("块文本：{}".format(str(item["text"])[:500]))
        blocks.append("\n".join(block_lines))
    return "\n\n".join(blocks)


def extract_papers_from_scholar_results(results: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    papers: List[Dict[str, str]] = []
    seen = set()
    for item in list(results or []):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "") or "").strip()
        if not is_probable_paper_title(title) or title in seen:
            continue
        seen.add(title)
        blob = "\n".join(
            [
                str(item.get("meta", "") or ""),
                str(item.get("abstract", "") or ""),
                str(item.get("text", "") or ""),
            ]
        )
        papers.append(
            {
                "title": title,
                "authors": sanitize_authors(item.get("authors", "")) or infer_authors_from_result_blob(blob, title=title),
                "year": normalize_year(item.get("year", "")) or normalize_year(blob),
            }
        )
        if len(papers) >= DEFAULT_PAPERS_PER_DIRECTION:
            break
    return papers


def infer_authors_from_result_blob(blob: Any, title: str = "") -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(blob or "").splitlines()]
    lines = [line for line in lines if line and not contains_noise_text(line)]
    return extract_authors_from_lines(lines, title=title)


def heuristic_extract_papers(text: str) -> List[Dict[str, str]]:
    payload = sanitize_scholar_page_text(text)
    if not payload:
        return []
    if "共" in payload and "相关结果" in payload and any(
        token in payload for token in ["北大核心期刊", "CSCD 索引", "CSSCI索引", "中国科技核心期刊", "免费下载", "登录查看"]
    ):
        return []
    lines = [line.strip() for line in payload.splitlines() if line.strip()]
    candidates: List[Dict[str, str]] = []
    seen = set()
    for index, line in enumerate(lines):
        title = line
        if not is_probable_paper_title(title):
            continue
        normalized_title = re.sub(r"\s+", " ", title).strip()
        year = normalize_year(line)
        authors = ""
        if not year and index + 1 < len(lines):
            year = normalize_year(lines[index + 1])
        if index + 1 < len(lines):
            authors = extract_authors_from_lines(lines[index + 1 : index + 8], title=normalized_title)
        if normalized_title in seen:
            continue
        seen.add(normalized_title)
        candidates.append({"title": normalized_title, "authors": authors, "year": year})
        if len(candidates) >= DEFAULT_PAPERS_PER_DIRECTION:
            break
    return candidates


def normalize_author_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" ：:;；，,。")
    if not text or contains_noise_text(text):
        return ""
    text = re.sub(r"^(作者|来源)[:：]?\s*", "", text)
    text = re.sub(r"[，,、;； ]作者", " ", text)
    text = re.sub(r"(被引量|摘要|关键词|DOI|doi)[:：].*$", "", text, flags=re.I)
    text = re.sub(r"^(专利|作者|发明人)[:：]?\s*", "", text)
    text = re.sub(r"(19|20)\d{2}年?", "", text)
    text = re.split(r"[;；。]|《|（|\(|\[|【", text, maxsplit=1)[0].strip()
    text = re.sub(r"\s+", " ", text).strip(" ：:;；，,。")
    return text


def _is_author_label_line(text: str) -> bool:
    return bool(re.fullmatch(r"(期刊|学位|会议|专利|图书|报纸)", text))


def _is_author_stop_line(text: str) -> bool:
    return bool(
        re.fullmatch(r"[-–—]+", text)
        or re.fullmatch(r"《.+》", text)
        or re.search(r"来源|被引量|\b(?:19|20)\d{2}\b", text)
    )


def extract_author_segment(line: Any, title: str = "") -> str:
    text = re.sub(r"\s+", " ", str(line or "")).strip()
    if not text or contains_noise_text(text):
        return ""
    title_text = re.sub(r"\s+", " ", str(title or "")).strip()
    if title_text and text.startswith(title_text):
        text = text[len(title_text):].strip(" ：:;；，,。-—")
    if "来源" in text:
        text = text.split("来源", 1)[0].strip()
    if "专利" in text:
        text = text.split("专利", 1)[1].strip()
    if re.match(r"^(作者|发明人)[:：]", text):
        text = re.sub(r"^(作者|发明人)[:：]\s*", "", text)
    text = normalize_author_text(text)
    if not text:
        return ""
    if _AUTHOR_STOPWORDS_RE.search(text):
        return ""
    if text == title_text:
        return ""
    if title_text and title_text in text:
        text = text.replace(title_text, " ").strip(" ：:;；，,。-—")
        text = normalize_author_text(text)
    return text if is_probable_author_text(text) else ""


def extract_authors_from_lines(lines: Sequence[str], title: str = "") -> str:
    prepared = [re.sub(r"\s+", " ", str(line or "")).strip() for line in list(lines or [])]
    prepared = [line for line in prepared if line and not contains_noise_text(line)]
    if not prepared:
        return ""
    title_text = re.sub(r"\s+", " ", str(title or "")).strip()
    start_index = 0
    if title_text:
        for idx, line in enumerate(prepared):
            if line == title_text:
                start_index = idx + 1
                break
    trailing_lines = prepared[start_index:]
    collected: List[str] = []
    started = False
    for line in trailing_lines:
        if _is_author_label_line(line) and not started:
            continue
        if _is_author_stop_line(line):
            if started:
                break
            continue
        candidate = extract_author_segment(line, title=title_text)
        if candidate:
            started = True
            collected.append(candidate)
            continue
        if started:
            break
    merged = "， ".join(item.strip(" ，,、") for item in collected if item.strip(" ，,、"))
    merged = re.sub(r"[，,、]\s*[，,、]\s*", "， ", merged)
    merged = re.sub(r"\s+", " ", merged).strip()
    merged = re.sub(r"[，,、]\s*\.\.\.$", "", merged)
    merged = re.sub(r"[，,、]\s*…+$", "", merged)
    merged = re.sub(r"[，,、\s]+$", "", merged).strip()
    return merged if is_probable_author_text(merged) else ""


def is_probable_author_text(value: Any) -> bool:
    text = normalize_author_text(value)
    if not text or len(text) > 80:
        return False
    if _AUTHOR_STOPWORDS_RE.search(text):
        return False
    if _AUTHOR_TECHNICAL_TERMS_RE.search(text):
        return False
    if re.fullmatch(r"[\u4e00-\u9fff]{2,4}", text):
        return True
    chinese_name_list = re.fullmatch(r"[\u4e00-\u9fff]{2,4}(?:\s*[，,、· ]\s*[\u4e00-\u9fff]{2,4}){0,9}", text)
    if chinese_name_list:
        return True
    english_name_list = re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{1,40}(?:\s*[，,;；]\s*[A-Za-z][A-Za-z .'\-]{1,40}){0,9}", text)
    return bool(english_name_list)


def resolve_output_path(path_text: str) -> Path:
    target = str(path_text or "").strip()
    if not target:
        raise ValueError("缺少输出表格路径。")
    target = os.path.expanduser(target)
    return Path(target).resolve()


def write_literature_xlsx(file_path: Path, headers: Sequence[str], rows: Sequence[Dict[str, str]]) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    shared_strings: List[str] = []
    shared_index: Dict[str, int] = {}

    def string_id(value: str) -> int:
        text = str(value or "")
        if text in shared_index:
            return shared_index[text]
        shared_index[text] = len(shared_strings)
        shared_strings.append(text)
        return shared_index[text]

    all_rows: List[List[str]] = [list(headers)]
    for row in rows:
        all_rows.append([str(row.get(header, "") or "") for header in headers])

    sheet_rows: List[str] = []
    for row_index, values in enumerate(all_rows, start=1):
        cells: List[str] = []
        for col_index, value in enumerate(values, start=1):
            cell_ref = "{}{}".format(column_name(col_index), row_index)
            sid = string_id(value)
            cells.append(f'<c r="{cell_ref}" t="s"><v>{sid}</v></c>')
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    shared_items = "".join(f"<si><t>{escape(item)}</t></si>" for item in shared_strings)
    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{len(shared_strings)}" uniqueCount="{len(shared_strings)}">{shared_items}</sst>'
    )
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{column_name(len(headers))}{len(all_rows)}"/>'
        f'<sheetData>{"".join(sheet_rows)}</sheetData>'
        "</worksheet>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="文献汇总" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
        'Target="sharedStrings.xml"/>'
        "</Relationships>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        "</Types>"
    )

    with zipfile.ZipFile(file_path, "w", zipfile.ZIP_DEFLATED) as workbook:
        workbook.writestr("[Content_Types].xml", content_types)
        workbook.writestr("_rels/.rels", root_rels)
        workbook.writestr("xl/workbook.xml", workbook_xml)
        workbook.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        workbook.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        workbook.writestr("xl/sharedStrings.xml", shared_xml)


def column_name(index: int) -> str:
    if index <= 0:
        raise ValueError("列序号必须大于 0。")
    chars: List[str] = []
    value = index
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        chars.append(chr(65 + remainder))
    return "".join(reversed(chars))


def extract_json_array(text: Any) -> List[Any]:
    payload = str(text or "").strip()
    if not payload:
        return []
    try:
        parsed = json.loads(payload)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        pass
    match = re.search(r"\[.*\]", payload, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def hardcoded_elements_summary() -> Dict[str, Any]:
    return {
        "search_engine": "baidu_scholar",
        "default_headers": list(DEFAULT_HEADERS),
        "direction_limit": DEFAULT_DIRECTION_LIMIT,
        "papers_per_direction": DEFAULT_PAPERS_PER_DIRECTION,
        "baidu_scholar_home_url": BAIDU_SCHOLAR_HOME_URL,
        "search_textarea_selector": SCHOLAR_SEARCH_TEXTAREA_SELECTOR,
        "search_textarea_fallbacks": list(SCHOLAR_SEARCH_TEXTAREA_FALLBACKS),
        "submit_method": SCHOLAR_SUBMIT_METHOD,
    }


def build_task_spec(instruction: str, previous_results: Optional[Any]) -> Optional[Dict[str, Any]]:
    if not is_research_literature_table_scenario(instruction):
        return None
    collected_output: Optional[Dict[str, Any]] = None
    if isinstance(previous_results, list):
        for item in previous_results:
            if not isinstance(item, dict):
                continue
            if str(item.get("kind", "") or "") != "research.collect_literature":
                continue
            output = item.get("output")
            if not isinstance(output, dict):
                continue
            manual_takeover = bool(output.get("manual_takeover_required"))
            rows = output.get("rows", [])
            if not manual_takeover and isinstance(rows, list) and rows:
                collected_output = output
    arguments: Dict[str, Any] = {}
    explicit_target = extract_spreadsheet_target(instruction)
    if not explicit_target:
        raise ValueError("research.collect_literature 需要用户明确提供 xlsx 文件路径。")
    if collected_output is not None:
        payload = build_spreadsheet_write_task_spec(explicit_target, collected_output)
        return prune_completed_research_write_operations(payload, previous_results)
    arguments["file_path"] = explicit_target
    return {
        "summary": "规划方向并检索文献后写入 Excel",
        "success_criteria": ["已生成包含标题、作者、发表年份的文献表格"],
        "metadata": {"scenario": "research_literature"},
        "operations": [
            {
                "id": "collect_research_literature",
                "kind": "research.collect_literature",
                "description": "执行固定文献检索工作流并写入 Excel",
                "arguments": arguments,
                "depends_on": [],
                "risky": False,
            }
        ],
    }


def planning_prompt_addon() -> str:
    """追加到 Planner 主提示后的规则片段。"""
    lines: List[str] = [
        "\n",
        "20. 【学术资料检索写入表格】当本任务属于「从学术/论文站点检索并汇总到 Excel 或 WPS 表格」时，遵守下列约束：\n",
        "20a. 若命中固定文献工作流场景，优先使用 research.collect_literature，一次性完成方向规划、学术检索、结果解析与 xlsx 写入。\n",
        "20b. 该固定工作流当前写死使用百度学术入口，搜索框选择器与首页 URL 由桌面层常量维护。\n",
        "20c. 用户必须明确给出 .xlsx/.et 路径，并传给 research.collect_literature 的 file_path；不要编造默认输出路径。\n",
        "20d. 不要在本场景下改用 meeting.extract_actions 或 meeting.send_assignments。\n",
    ]
    return "".join(lines)


def build_spreadsheet_write_task_spec(file_path: str, collected_output: Dict[str, Any]) -> Dict[str, Any]:
    rows = list(collected_output.get("rows", []) or [])
    operations: List[Dict[str, Any]] = [
        {
            "id": "open_research_spreadsheet",
            "kind": "spreadsheet.open",
            "description": "打开文献汇总表格",
            "arguments": {"file_path": file_path},
            "depends_on": [],
            "risky": False,
        }
    ]
    previous_id = "open_research_spreadsheet"
    for column_index, header in enumerate(DEFAULT_HEADERS, start=1):
        op_id = "write_header_{}".format(column_name(column_index).lower())
        operations.append(
            {
                "id": op_id,
                "kind": "spreadsheet.write_cell",
                "description": "写入文献表头",
                "arguments": {
                    "file_path": file_path,
                    "cell": "{}1".format(column_name(column_index)),
                    "text": str(header),
                },
                "depends_on": [previous_id],
                "risky": False,
            }
        )
        previous_id = op_id
    for row_index, row in enumerate(rows, start=2):
        for column_index, header in enumerate(DEFAULT_HEADERS, start=1):
            op_id = "write_r{}_{}".format(row_index, column_name(column_index).lower())
            operations.append(
                {
                    "id": op_id,
                    "kind": "spreadsheet.write_cell",
                    "description": "写入文献行",
                    "arguments": {
                        "file_path": file_path,
                        "cell": "{}{}".format(column_name(column_index), row_index),
                        "text": str(row.get(header, "") or ""),
                    },
                    "depends_on": [previous_id],
                    "risky": False,
                }
            )
            previous_id = op_id
    return {
        "summary": "将检索到的文献逐格写入 Excel",
        "success_criteria": ["已通过 spreadsheet.write_cell 将文献结果写入表格"],
        "metadata": {"scenario": "research_literature", "phase": "spreadsheet_write"},
        "operations": operations,
    }


def prune_completed_research_write_operations(
    payload: Dict[str, Any],
    previous_results: Optional[Any],
) -> Dict[str, Any]:
    operations = list(payload.get("operations", []) or [])
    if not operations or not isinstance(previous_results, list):
        return payload

    completed_signatures = set()
    for item in previous_results:
        if not isinstance(item, dict):
            continue
        completed_signatures.add(
            operation_signature(
                str(item.get("kind", "") or ""),
                dict(item.get("arguments", {}) or {}),
            )
        )
    completed_signatures.discard("")
    if not completed_signatures:
        return payload

    remaining: List[Dict[str, Any]] = []
    for operation in operations:
        signature = operation_signature(
            str(operation.get("kind", "") or ""),
            dict(operation.get("arguments", {}) or {}),
        )
        if signature in completed_signatures:
            continue
        remaining.append(operation)

    if not remaining:
        return {
            "summary": "文献检索与汇总已完成",
            "success_criteria": ["已通过 spreadsheet.write_cell 将文献结果写入表格"],
            "metadata": {"task_completed": True, "scenario": "research_literature", "phase": "spreadsheet_write"},
            "operations": [],
        }

    # 剪掉已完成节点后，原 depends_on 可能指向已移除的 id 或只剩空列表；若多个 write_cell
    # 同时变成 depends_on=[]，调度器会并行执行它们，易触发 Excel/COM 挂起。按 remaining 的
    # 原始顺序重建单链：每项仅依赖上一项（首项无依赖），与「逐格写入」语义一致。
    for index, operation in enumerate(remaining):
        if index == 0:
            operation["depends_on"] = []
        else:
            prev_id = str(remaining[index - 1].get("id", "") or "")
            operation["depends_on"] = [prev_id] if prev_id else []

    payload = dict(payload)
    payload["operations"] = remaining
    return payload


def operation_signature(kind: str, arguments: Dict[str, Any]) -> str:
    return "{}|{}".format(str(kind or ""), json.dumps(dict(arguments or {}), ensure_ascii=False, sort_keys=True))


def example_demo_prompt() -> str:
    """供演示或文档引用的示例自然语言指令。"""
    return (
        "帮我在 Google 学术上（如果在虚拟机里上不去可以用百度学术）搜索和大模型微调有关的论文，"
        "先按不同方向规划检索，再把各方向找到的内容写入 /home/kylin/桌面/工作簿1.xlsx，"
        "总结每篇的标题、作者、发表年份。"
    )


def vm_run_hints() -> str:
    """虚拟机与 SSH 运行提示。"""
    return (
        "在麒麟虚拟机中通过共享目录开发时，项目路径常为 /mnt/hgfs/<共享名>。"
        "可先通过 ssh 登录，再进入项目目录后执行 python3 相关测试或调试脚本。"
    )
