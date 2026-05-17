from __future__ import annotations

import asyncio
import zipfile
import pytest

from os_computer_use.desktop.research_literature_workflow import (
    ResearchLiteratureWorkflow,
    build_task_spec,
    extract_authors_from_lines,
    extract_author_segment,
    extract_papers_from_scholar_results,
    extract_scholar_results,
    extract_spreadsheet_target,
    extract_research_topic,
    normalize_search_query,
    is_probable_author_text,
    sanitize_paper_candidate,
    heuristic_extract_papers,
    write_literature_xlsx,
)


class FakeDesktop:
    def __init__(self) -> None:
        self.queries = []

    async def baidu_scholar_search(self, query: str):
        self.queries.append(query)
        return {
            "text": "大模型微调基础方法研究\n作者甲, 作者乙\n2024\n参数高效微调训练机制分析\n作者丙\n2023",
            "raw_text": "大模型微调基础方法研究\n作者甲, 作者乙\n2024\n参数高效微调训练机制分析\n作者丙\n2023",
        }

    async def _capture_search_page_snapshot(self):
        return {"text": "大模型微调基础方法研究\n作者甲, 作者乙\n2024\n参数高效微调训练机制分析\n作者丙\n2023"}


def test_extract_spreadsheet_target_reads_windows_path() -> None:
    instruction = r"把结果写到 E:\computer-use\open-computer-use\output\literature.xlsx"
    assert extract_spreadsheet_target(instruction).endswith(r"output\literature.xlsx")


def test_build_task_spec_returns_fixed_operation() -> None:
    payload = build_task_spec(
        "帮我在百度学术搜索大模型微调论文并写入 E:\\computer-use\\open-computer-use\\output\\literature.xlsx",
        previous_results=None,
    )
    assert payload is not None
    assert payload["operations"][0]["kind"] == "research.collect_literature"


def test_build_task_spec_requires_explicit_xlsx_path() -> None:
    with pytest.raises(ValueError):
        build_task_spec("帮我在百度学术搜索大模型微调论文并写入表格", previous_results=None)


def test_extract_research_topic_strips_leading_conjunction_and_quotes() -> None:
    topic = extract_research_topic('帮我在百度学术上搜索和"大模型微调"有关的论文，并写入 E:\\out.xlsx')
    assert topic == "大模型微调"
    assert normalize_search_query('"和大模型微调 PEFT LoRA') == "大模型微调 PEFT LoRA"


def test_build_task_spec_does_not_mark_manual_takeover_as_completed() -> None:
    payload = build_task_spec(
        "帮我在百度学术搜索大模型微调论文并写入 E:\\computer-use\\open-computer-use\\output\\literature.xlsx",
        previous_results=[
            {
                "kind": "research.collect_literature",
                "output": {
                    "manual_takeover_required": True,
                    "reason": "search_verification_required",
                },
            }
        ],
    )
    assert payload is not None
    assert payload["metadata"].get("task_completed") is not True
    assert payload["operations"][0]["kind"] == "research.collect_literature"


def test_build_task_spec_switches_to_spreadsheet_write_after_collection() -> None:
    payload = build_task_spec(
        "帮我在百度学术搜索大模型微调论文并写入 E:\\computer-use\\open-computer-use\\output\\literature.xlsx",
        previous_results=[
            {
                "kind": "research.collect_literature",
                "output": {
                    "file_path": r"E:\computer-use\open-computer-use\output\literature.xlsx",
                    "paper_count": 1,
                    "rows": [
                        {"方向": "基础方法", "标题": "论文一", "作者": "作者甲", "发表年份": "2024"},
                    ],
                },
            }
        ],
    )
    assert payload is not None
    assert payload["operations"][0]["kind"] == "spreadsheet.open"
    assert payload["operations"][1]["kind"] == "spreadsheet.write_cell"
    assert payload["operations"][1]["depends_on"] == ["open_research_spreadsheet"]
    assert payload["operations"][2]["depends_on"] == [payload["operations"][1]["id"]]


def test_build_task_spec_marks_completed_after_all_research_writes() -> None:
    instruction = "帮我在百度学术搜索大模型微调论文并写入 E:\\computer-use\\open-computer-use\\output\\literature.xlsx"
    initial = build_task_spec(
        instruction,
        previous_results=[
            {
                "kind": "research.collect_literature",
                "output": {
                    "file_path": r"E:\computer-use\open-computer-use\output\literature.xlsx",
                    "paper_count": 1,
                    "rows": [
                        {"方向": "基础方法", "标题": "论文一", "作者": "作者甲", "发表年份": "2024"},
                    ],
                },
            }
        ],
    )
    previous_results = [
        {
            "kind": op["kind"],
            "arguments": dict(op.get("arguments", {}) or {}),
        }
        for op in initial["operations"]
    ]
    previous_results.insert(
        0,
        {
            "kind": "research.collect_literature",
            "output": {
                "file_path": r"E:\computer-use\open-computer-use\output\literature.xlsx",
                "paper_count": 1,
                "rows": [
                    {"方向": "基础方法", "标题": "论文一", "作者": "作者甲", "发表年份": "2024"},
                ],
            },
        },
    )
    payload = build_task_spec(instruction, previous_results=previous_results)
    assert payload is not None
    assert payload["metadata"].get("task_completed") is True
    assert payload["operations"] == []


def test_write_literature_xlsx_creates_valid_archive(tmp_path) -> None:
    file_path = tmp_path / "literature.xlsx"
    write_literature_xlsx(
        file_path,
        ["方向", "标题", "作者", "发表年份"],
        [{"方向": "参数高效微调", "标题": "论文一", "作者": "作者甲", "发表年份": "2024"}],
    )
    assert file_path.exists()
    with zipfile.ZipFile(file_path, "r") as workbook:
        names = set(workbook.namelist())
    assert "xl/workbook.xml" in names
    assert "xl/worksheets/sheet1.xml" in names
    assert "xl/sharedStrings.xml" in names


def test_collect_literature_writes_manifest_and_rows(tmp_path) -> None:
    workflow = ResearchLiteratureWorkflow(desktop=FakeDesktop(), provider=None, artifact_root=str(tmp_path))
    payload = asyncio.run(
        workflow.collect_literature(
            "帮我在百度学术搜索和大模型微调有关的论文，并写入 E:\\computer-use\\open-computer-use\\output\\literature.xlsx"
        )
    )
    assert payload["paper_count"] > 0
    assert payload["direction_count"] > 0
    assert payload["file_path"].endswith("literature.xlsx")
    assert payload["rows"]
    assert "manifest_path" in payload


def test_sanitize_paper_candidate_filters_noise_title() -> None:
    assert sanitize_paper_candidate({"title": "DeepSeek-R1", "authors": "以上内容由AI生成，仅供参考", "year": ""}) == {}
    assert sanitize_paper_candidate({"title": "以上内容由AI生成，仅供参考", "authors": "", "year": ""}) == {}


def test_extract_scholar_results_ignores_ai_sidebar_entries() -> None:
    snapshot = {
        "scholarResults": [
            {
                "title": "基于思维链增强的分类分级大模型微调方法研究",
                "meta": "徐允彪，邓莎，李亚荣 来源：通信技术 2025年",
                "abstract": "提出基于领域思维链的大模型分类分级微调方法。",
                "text": "基于思维链增强的分类分级大模型微调方法研究",
            },
            {
                "title": "DeepSeek-R1",
                "meta": "以上内容由AI生成，仅供参考",
                "abstract": "AI高级搜索",
                "text": "你好！我是你的智能助手",
            },
        ]
    }
    results = extract_scholar_results(snapshot, {})
    assert len(results) == 1
    assert results[0]["title"] == "基于思维链增强的分类分级大模型微调方法研究"


def test_extract_papers_from_scholar_results_prefers_clean_result_blocks() -> None:
    papers = extract_papers_from_scholar_results(
        [
            {
                "title": "基于思维链增强的分类分级大模型微调方法研究",
                "authors": "徐允彪，邓莎，李亚荣",
                "year": "2025",
                "meta": "徐允彪，邓莎，李亚荣 来源：通信技术 被引量：0 2025年",
                "abstract": "面向多模态数据与动态场景。",
                "text": "基于思维链增强的分类分级大模型微调方法研究",
            },
            {
                "title": "以上内容由AI生成，仅供参考",
                "meta": "",
                "abstract": "",
                "text": "",
            },
        ]
    )
    assert len(papers) == 1
    assert papers[0]["title"] == "基于思维链增强的分类分级大模型微调方法研究"
    assert papers[0]["authors"] == "徐允彪，邓莎，李亚荣"
    assert papers[0]["year"] == "2025"


def test_heuristic_extract_papers_skips_error_fallback_text() -> None:
    papers = heuristic_extract_papers("搜索完成，但没有提取到足够可靠的结果")
    assert papers == []


def test_extract_papers_from_scholar_results_prefers_structured_author_fields() -> None:
    papers = extract_papers_from_scholar_results(
        [
            {
                "title": "刺激辅助的脑电信号特征增强方法与混合式脑机接口",
                "authors": "陈岩，李青",
                "year": "2023",
                "meta": "来源：某期刊 被引量：0",
                "abstract": "",
                "text": "刺激辅助的脑电信号特征增强方法与混合式脑机接口\n陈岩，李青\n2023",
            }
        ]
    )
    assert len(papers) == 1
    assert papers[0]["authors"] == "陈岩，李青"
    assert papers[0]["year"] == "2023"


def test_extract_author_segment_strips_title_prefix_and_keeps_single_name() -> None:
    title = "基于思维链增强的分类分级大模型微调方法研究"
    line = "基于思维链增强的分类分级大模型微调方法研究 陈岩"
    assert extract_author_segment(line, title=title) == "陈岩"


def test_extract_author_segment_strips_patent_marker_and_keeps_name_list() -> None:
    title = "一种处理多模态混合数据并增强模型生成效果的方法"
    line = "一种处理多模态混合数据并增强模型生成效果的方法 专利 周汉宾，张京，王凯"
    assert extract_author_segment(line, title=title) == "周汉宾，张京，王凯"


def test_is_probable_author_text_rejects_technical_phrase() -> None:
    assert is_probable_author_text("刺激辅助的脑电信号特征增强方法与混合式脑机接口") is False
    assert is_probable_author_text("徐允彪，邓莎，李亚荣") is True


def test_extract_authors_from_lines_skips_journal_label_and_stops_before_source() -> None:
    lines = [
        "小样本场景下基于大模型微调的秸秆未离田地块识别方法研究",
        "期刊",
        "吴迪，",
        "刘喜，",
        "白松",
        "，...",
        "-",
        "《测绘与空间地理信息》",
        "- 2025年",
        "来源：",
    ]
    assert extract_authors_from_lines(lines, title="小样本场景下基于大模型微调的秸秆未离田地块识别方法研究") == "吴迪， 刘喜， 白松"


def test_infer_authors_from_result_blob_handles_abstract_then_multiline_authors() -> None:
    blob = "\n".join(
        [
            "个性化人机信息交互中的大模型微调技术研究",
            "随着大模型在自然语言处理领域的广泛应用,如何通过微调技术实现个性化人机交互成为研究热点.",
            "期刊",
            "苏歆海，",
            "钱铭钢，",
            "刘成",
            "-",
            "《科技视界》",
            "- 2025年",
            "来源：万方 / 掌桥科研 / 知网",
        ]
    )
    from os_computer_use.desktop.research_literature_workflow import infer_authors_from_result_blob

    assert infer_authors_from_result_blob(blob, title="个性化人机信息交互中的大模型微调技术研究") == "苏歆海， 钱铭钢， 刘成"
