# 抄袭检测 Web 界面 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 kbweb 里做出一个判定导向的查重界面——提交稿件、看进度、得到总重复率与全文高亮、逐处追到来源出处。

**Architecture:** Flask blueprint + Jinja SSR，经 `KbClient` 走 kbsvc REST。详情页按 `status` 分支，进度与报告共用一个 URL，无脚本下靠 `<meta http-equiv="refresh">` 自然过渡。SSE 只是渐进增强，Flask 侧代理并截流。

**Tech Stack:** Flask 3.1、Jinja2、httpx、原生 JS（无构建）、pytest + respx、Playwright（e2e）

**规格：** `docs/superpowers/specs/2026-08-08-plagiarism-frontend-design.md`

## Global Constraints

以下约束对每个 Task 都生效，不再逐条重复：

- **无脚本必须可用。** 任何交互都要有无 JS 路径。`tests/conftest.py` 的 `config` fixture 是 `params=[False, True]` 参数化的，所以**每个视图测试自动跑 js 与 nojs 两遍**——测试写一遍即覆盖两个维度。
- **kbweb 不持有存储。** 不缓存后端数据，不落盘，不进 session。
- **朱砂 `--seal` 是语义色**，只用于命中高亮与引用标记，不作装饰。提交按钮、进度条、重复率数字使用既有墨色/靛青/赭石语义色，不得使用 `btn--seal` 或 `--seal`。
- **模板输出默认转义。** 高亮走分段拼接（`coverage_segments` 返回的片段逐个输出），**不得用 `|safe` 拼原始 HTML**。
- **API Key 只在服务端**，不出现在任何渲染结果里。
- 现有代码风格：ruff，`line-length = 100`，`select = ["E", "F", "I", "UP", "B", "SIM"]`，`from __future__ import annotations` 打头。
- 后端常量：`plag_max_input_chars = 500_000`、`plag_max_active_checks_per_key = 2`、`plag_preview_chars = 300`、`plag_sse_max_seconds = 900`；既有 chunks API 的 `limit` 上限是 **200**。
- **以后端真实错误码为准。** 本功能涉及 `feature_disabled`、`plagiarism_concurrency_limit`、`plagiarism_input_too_large`、`plagiarism_corpus_not_ready`、`plagiarism_corpus_empty`、`plagiarism_check_not_found`、`report_visibility_changed`、`idempotency_conflict`，以及框架层可能返回的 `validation_error`。文档 chunks 自身的 `not_found` 是有效契约，但不得拿它替代 plagiarism check 的 `plagiarism_check_not_found`。
- 测试命令一律在 `knowledge-web/` 下跑。

## 文件结构

**新建：**

| 文件 | 职责 |
|---|---|
| `kbweb/views/plagiarism.py` | 六条路由，只做请求/响应与模板选择 |
| `kbweb/report.py` | 报告装配：来源编号、查询侧区间、来源正文摘取。无 Flask 依赖，纯数据变换 |
| `kbweb/templates/plagiarism.html` | 提交页 + 历史列表 |
| `kbweb/templates/check.html` | 详情页外壳，按状态选 partial |
| `kbweb/templates/partials/_check_progress.html` | 进度态 |
| `kbweb/templates/partials/_check_report.html` | 报告态 |
| `kbweb/static/js/check.js` | EventSource + 点高亮展开对照卡 |
| `tests/test_report.py` | `kbweb/report.py` 的纯函数测试 |
| `tests/test_plagiarism.py` | 六条路由的视图测试 |

路由与装配分开的理由：视图只剩「取数据、选模板」，装配逻辑（编号、区间、摘取）是纯函数，可以脱离 Flask 单测。两者都能保持在 200 行以内。

**修改：**

| 文件 | 改动 |
|---|---|
| `kbweb/client.py` | 拆出 `_send`；新增 8 个 plagiarism 方法 |
| `kbweb/filters.py` | 新增 `coverage_segments` |
| `kbweb/__init__.py:102-109` | 注册 blueprint |
| `kbweb/views/library.py` | 为文档详情生成服务端 UUID 幂等 token |
| `kbweb/templates/base.html:31-40` | 导航增加「查重」 |
| `kbweb/templates/document.html` | 「维护」面板增加查重按钮 |
| `kbweb/static/css/app.css` | 追加样式 |
| `tests/test_nojs.py:38` | `PAGES` 增加 `/plagiarism/` |
| `tests/conftest.py` | 新增 plagiarism 相关 payload fixture |
| `tests/e2e/stub_backend.py` | 扩展注入式 `FakeKbClient` 的 plagiarism 方法与假 SSE（不是挂 HTTP 路由） |
| `deploy/Dockerfile:29` | `waitress-serve` 增加 `--threads=16` |

`tests/e2e/stub_backend.py` 实际是注入进 kbweb 的 `FakeKbClient`，不是 HTTP 应用；Task 9 必须扩展这个类的方法面，不能在文件里挂假路由。

### Task 0: 后端前置契约（阻塞 Task 6 与 Task 7 的版本一致性）

前端动工前先在 `knowledge-service/` 建一个独立提交，完成并验证以下契约：

1. `CheckReport` 与 `ReportOut` 增加 `query_text`，`ReportOut.of()` 完整映射。
2. 文本检测继续从 `PlagCheck.query_text` 返回原文；文档检测在 runner 按冻结的 `source_version_id` 解析出正文后，也把这份**检测快照**写入同一字段。报告读取不重复解析对象存储。新检查若冻结版本已不存在，runner 必须明确失败，不能静默完成为与 `query_chars` 不一致的空串；历史检查若尚未保存快照，则按既有兼容策略如实返回空串，不做报告读取时的隐式回填。
3. `CheckRunner._persist()` 写 `PlagCheckSource.version` 时保存真实 `DocumentVersion.version`，不得继续写常量 `0`。来源摘录随后用报告中的版本读取，避免文档更新后旧偏移切到新正文。
4. 保持现有 ACL 语义：若在 `get_report` 之前已有来源被撤权，后端返回 `report_visibility_changed` 409，前端显示专门说明，不渲染可能泄露来源信息的部分报告。只有“报告已成功取得、随后 chunks 请求失败”的竞态才按单卡降级。
5. 新建 `knowledge-service/docs/adr/0006-return-owner-query-text-in-plagiarism-report.md`，记录为何只向检测创建者回显查询侧原文，以及文档模式如何按冻结版本还原。

**Files:**
- Modify: `knowledge-service/src/kbsvc/plagiarism/types.py`
- Modify: `knowledge-service/src/kbsvc/plagiarism/models.py`（更新 `query_text` 注释以反映文档检测快照）
- Modify: `knowledge-service/src/kbsvc/plagiarism/service.py`
- Modify: `knowledge-service/src/kbsvc/plagiarism/runner.py`
- Modify: `knowledge-service/src/kbsvc/api/plagiarism_schemas.py`
- Create: `knowledge-service/docs/adr/0006-return-owner-query-text-in-plagiarism-report.md`
- Test: `knowledge-service/tests/plagiarism/test_api.py`、对应 runner/service 测试

**门禁测试：** 文本模式与文档模式的报告都返回与 `query_chars` 一致的 `query_text`；来源文档产生新版本后，旧报告仍携带旧来源版本号；`report_visibility_changed` 仍返回 409。

**Task 0 收尾决策：** `query_text` 列和来源版本列均已存在，因此本任务只修正写入/读取契约，不新增列、不重建索引、不执行全表回填。旧记录的空 `query_text` 是兼容行为；从本次修订起新完成的检查必须保存检测快照。

Task 1–5、8 可先行；Task 6 依赖 `query_text`，Task 7 的正确摘录依赖真实来源版本号。

---

### Task 1: 纯函数——`coverage_segments` 与 `source_excerpt`

本次唯一的算法性代码。先做，因为后面所有模板都依赖它，且它完全不需要 Flask。

**Files:**
- Modify: `kbweb/filters.py`
- Create: `kbweb/report.py`
- Test: `tests/test_filters.py`（追加）、`tests/test_report.py`（新建）

**Interfaces:**
- Produces:
  - `coverage_segments(text: str, spans: list[tuple[int, int, int]]) -> list[tuple[str, frozenset[int]]]`，注册为 Jinja global
  - `source_excerpt(chunks: list[dict], start: int, end: int) -> str`

- [x] **Step 1: 写 `coverage_segments` 的失败测试**

追加到 `tests/test_filters.py`：

```python
from kbweb.filters import coverage_segments


def owners_of(segments, fragment):
    return next(own for frag, own in segments if frag == fragment)


def test_coverage_marks_a_single_span():
    segments = coverage_segments("夫天地者万物之逆旅也", [(0, 3, 1)])
    assert segments == [("夫天地", frozenset({1})), ("者万物之逆旅也", frozenset())]


def test_coverage_keeps_overlap_instead_of_dropping_it():
    """highlight_segments drops the second span here; this must not."""
    segments = coverage_segments("零一二三四五", [(0, 4, 1), (2, 6, 2)])
    assert segments == [
        ("零一", frozenset({1})),
        ("二三", frozenset({1, 2})),
        ("四五", frozenset({2})),
    ]


def test_coverage_merges_two_sources_covering_the_same_span():
    segments = coverage_segments("零一二三", [(1, 3, 1), (1, 3, 2)])
    assert owners_of(segments, "一二") == frozenset({1, 2})


def test_coverage_handles_a_contained_span():
    segments = coverage_segments("零一二三四五", [(0, 6, 1), (2, 4, 2)])
    assert segments == [
        ("零一", frozenset({1})),
        ("二三", frozenset({1, 2})),
        ("四五", frozenset({1})),
    ]


def test_coverage_joins_runs_with_identical_owners():
    """Adjacent spans from the same source emit one run, not two."""
    segments = coverage_segments("零一二三", [(0, 2, 1), (2, 4, 1)])
    assert segments == [("零一二三", frozenset({1}))]


def test_coverage_leaves_a_gap_between_non_adjacent_spans():
    segments = coverage_segments("零一二三四五", [(0, 2, 1), (4, 6, 2)])
    assert segments == [
        ("零一", frozenset({1})),
        ("二三", frozenset()),
        ("四五", frozenset({2})),
    ]


def test_coverage_drops_out_of_range_spans_rather_than_trusting_them():
    assert coverage_segments("零一二", [(0, 99, 1)]) == [("零一二", frozenset())]
    assert coverage_segments("零一二", [(-1, 2, 1)]) == [("零一二", frozenset())]
    assert coverage_segments("零一二", [(2, 2, 1)]) == [("零一二", frozenset())]


def test_coverage_handles_empty_inputs():
    assert coverage_segments("", [(0, 1, 1)]) == []
    assert coverage_segments("零一二", []) == [("零一二", frozenset())]
```

- [x] **Step 2: 跑测试确认失败**

```bash
cd knowledge-web && python -m pytest tests/test_filters.py -v
```
预期：`ImportError: cannot import name 'coverage_segments'`

- [x] **Step 3: 实现 `coverage_segments`**

追加到 `kbweb/filters.py`（放在 `highlight_segments` 之后）：

```python
from collections import Counter, defaultdict


def coverage_segments(
    text: str, spans: list[tuple[int, int, int]]
) -> list[tuple[str, frozenset[int]]]:
    """Split text into (fragment, source ordinals) pairs.

    Deliberately *not* `highlight_segments`: that one drops a span overlapping
    the previous hit, which is right for search snippets and wrong here. One
    passage matching several sources is exactly what a plagiarism report exists
    to show, so overlaps are kept and merged instead.

    Ordinals are 1-based and match the numbering of the source list.
    """
    if not text:
        return []
    valid = [
        (start, end, ordinal)
        for start, end, ordinal in spans or []
        if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text)
    ]
    if not valid:
        return [(text, frozenset())]

    # A real event sweep: do not rescan every span at every boundary. Reports
    # may contain thousands of passages, so O(spans * boundaries) is not an
    # acceptable rendering path for the 500k-character input ceiling.
    events: dict[int, Counter[int]] = defaultdict(Counter)
    for start, end, ordinal in valid:
        events[start][ordinal] += 1
        events[end][ordinal] -= 1

    edges = sorted({0, len(text), *events})
    active: Counter[int] = Counter()
    sliced: list[tuple[str, frozenset[int]]] = []
    for left, right in zip(edges, edges[1:]):
        for ordinal, delta in events[left].items():
            active[ordinal] += delta
            if active[ordinal] <= 0:
                del active[ordinal]
        sliced.append((text[left:right], frozenset(active)))

    # Runs with identical owners become one <mark> rather than one per boundary.
    merged: list[tuple[list[str], frozenset[int]]] = []
    for fragment, owners in sliced:
        if merged and merged[-1][1] == owners:
            merged[-1][0].append(fragment)
        else:
            merged.append(([fragment], owners))
    return [("".join(fragments), owners) for fragments, owners in merged]
```

再加一条同一来源自重叠的测试，防止用 `set.remove()` 提前清掉仍活跃的区间：

```python
def test_coverage_counts_overlapping_spans_from_the_same_source():
    assert coverage_segments("零一二三四五", [(0, 6, 1), (2, 4, 1)]) == [
        ("零一二三四五", frozenset({1}))
    ]
```

复杂度目标：排序 O(n log n)，扫描 O(n)，正文切片/拼接 O(len(text))；不得保留“每个切片再次遍历全部 spans”或循环中反复拼接长字符串的二次复杂度实现。

并在 `register()` 的 `app.jinja_env.globals` 里加上它：

```python
    app.jinja_env.globals["highlight_segments"] = highlight_segments
    app.jinja_env.globals["coverage_segments"] = coverage_segments
```

- [x] **Step 4: 跑测试确认通过**

```bash
cd knowledge-web && python -m pytest tests/test_filters.py -v
```
预期：全部 PASS

- [x] **Step 5: 写 `source_excerpt` 的失败测试**

新建 `tests/test_report.py`：

```python
"""Report assembly. Pure data shaping, no Flask involved."""

from __future__ import annotations

from kbweb.report import source_excerpt


def chunk(ordinal: int, text: str, char_start: int) -> dict:
    return {
        "ordinal": ordinal,
        "text": text,
        "char_start": char_start,
        "char_end": char_start + len(text),
    }


def test_excerpt_slices_within_a_single_chunk():
    chunks = [chunk(0, "零一二三四五六七八九", 0)]
    assert source_excerpt(chunks, 2, 5) == "二三四"


def test_excerpt_stitches_across_chunk_boundaries():
    chunks = [chunk(0, "零一二三四", 0), chunk(1, "五六七八九", 5)]
    assert source_excerpt(chunks, 3, 7) == "三四五六"


def test_excerpt_ignores_chunks_outside_the_range():
    chunks = [chunk(0, "零一二", 0), chunk(1, "三四五", 3), chunk(2, "六七八", 6)]
    assert source_excerpt(chunks, 3, 6) == "三四五"


def test_excerpt_survives_offsets_that_do_not_match_chunk_text_length():
    """Offsets come from the document, chunk text may have been normalised.

    A mismatch must clamp, never raise and never read past the fragment.
    """
    odd = {"ordinal": 0, "text": "零一二", "char_start": 0, "char_end": 999}
    assert source_excerpt([odd], 0, 999) == "零一二"


def test_excerpt_returns_empty_when_nothing_covers_the_range():
    assert source_excerpt([chunk(0, "零一二", 0)], 50, 60) == ""
    assert source_excerpt([], 0, 10) == ""
```

- [x] **Step 6: 跑测试确认失败**

```bash
cd knowledge-web && python -m pytest tests/test_report.py -v
```
预期：`ModuleNotFoundError: No module named 'kbweb.report'`

- [x] **Step 7: 实现 `source_excerpt`**

新建 `kbweb/report.py`：

```python
"""Report assembly for plagiarism checks.

Pure data shaping between what kbsvc returns and what the templates need.
Kept out of the view so the numbering, span and excerpt logic can be tested
without a request context.
"""

from __future__ import annotations


def source_excerpt(chunks: list[dict], start: int, end: int) -> str:
    """Stitch the source text covering `[start, end)` out of chunks.

    Offsets are in *document* coordinates while `text` is per chunk, so every
    slice is clamped to the fragment actually in hand: a chunk whose declared
    `char_end` disagrees with `len(text)` (normalisation, re-parse) must clamp
    rather than raise. A gap between chunks simply contributes nothing.
    """
    parts: list[str] = []
    for chunk in chunks:
        chunk_start = chunk.get("char_start") or 0
        text = chunk.get("text") or ""
        chunk_end = chunk_start + len(text)
        if chunk_end <= start or chunk_start >= end:
            continue
        left = max(0, start - chunk_start)
        right = min(len(text), end - chunk_start)
        if left < right:
            parts.append(text[left:right])
    return "".join(parts)
```

- [x] **Step 8: 跑测试确认通过**

```bash
cd knowledge-web && python -m pytest tests/test_report.py tests/test_filters.py -v
```
预期：全部 PASS

- [x] **Step 9: 提交**

```bash
git add knowledge-web/kbweb/filters.py knowledge-web/kbweb/report.py \
        knowledge-web/tests/test_filters.py knowledge-web/tests/test_report.py
git commit -m "feat(web): 抄袭报告的区间覆盖与来源摘取

coverage_segments 与 highlight_segments 的区别在于重叠：后者遇重叠即
丢弃，对检索片段正确，对抄袭报告错误——一段文字同时命中多个来源
正是报告要表达的信息。用边界扫描线而非左右游标，游标表达不了重叠。"
```

---

### Task 2: `KbClient` 的 plagiarism 方法

**Files:**
- Modify: `kbweb/client.py`
- Test: `tests/test_plagiarism.py`（新建）

**Interfaces:**
- Consumes: 无
- Produces:
  - `KbClient.corpus_status() -> dict`
  - `KbClient.create_text_check(*, text: str, language: str = "auto", idempotency_key: str = "") -> dict`
  - `KbClient.create_document_check(document_id: str, *, idempotency_key: str = "") -> dict`
  - `KbClient.list_checks(*, limit: int = 50, offset: int = 0) -> list[dict]`
  - `KbClient.get_check(check_id: str) -> dict`
  - `KbClient.get_plag_report(check_id: str) -> dict`
  - `KbClient.delete_check(check_id: str) -> int`（返回 HTTP 状态码，204 或 202）
  - `KbClient.stream_progress(check_id: str, *, last_event_id: str = "")`（返回已统一处理鉴权、HTTP 错误与网络错误的流式上下文管理器）
  - 扩展既有 `KbClient.get_chunks(..., version: int | None = None)`；调用方 `limit` 不得超过后端上限 200

方法名用 `get_plag_report` 而非 `get_report`，避免与后续可能的其他 report 混淆；`delete_check` **返回状态码而非 body**，因为 204 与 202 的区分正是调用方要的信息。

- [x] **Step 1: 写失败测试**

新建 `tests/test_plagiarism.py`：

```python
"""Plagiarism client and views. Backend stubbed with respx throughout."""

from __future__ import annotations

import httpx
import respx

from kbweb.client import KbClient
from kbweb.config import Config

from .conftest import API_BASE


def make_client() -> KbClient:
    return KbClient(Config(api_base=API_BASE, api_key="kb_test_key", timeout=5))


@respx.mock
def test_text_check_forwards_the_idempotency_key():
    route = respx.post(f"{API_BASE}/v1/plagiarism/checks").mock(
        return_value=httpx.Response(202, json={"check_id": "chk-1", "status": "pending"})
    )
    api = make_client()
    api.create_text_check(text="夫天地者", idempotency_key="tok-abc")

    request = route.calls.last.request
    assert request.headers["Idempotency-Key"] == "tok-abc"
    assert request.headers["Authorization"] == "Bearer kb_test_key"
    api.close()


@respx.mock
def test_text_check_omits_the_header_when_no_key_is_given():
    route = respx.post(f"{API_BASE}/v1/plagiarism/checks").mock(
        return_value=httpx.Response(202, json={"check_id": "chk-1", "status": "pending"})
    )
    api = make_client()
    api.create_text_check(text="夫天地者")
    assert "Idempotency-Key" not in route.calls.last.request.headers
    api.close()


@respx.mock
def test_delete_returns_the_status_code_so_204_and_202_stay_distinguishable():
    respx.delete(f"{API_BASE}/v1/plagiarism/checks/chk-gone").mock(
        return_value=httpx.Response(204)
    )
    respx.delete(f"{API_BASE}/v1/plagiarism/checks/chk-busy").mock(
        return_value=httpx.Response(202, json={"outcome": "cancel_requested"})
    )
    api = make_client()
    assert api.delete_check("chk-gone") == 204
    assert api.delete_check("chk-busy") == 202
    api.close()


@respx.mock
def test_document_check_posts_to_the_document_route():
    route = respx.post(f"{API_BASE}/v1/plagiarism/checks/documents/doc-1111-2222").mock(
        return_value=httpx.Response(202, json={"check_id": "chk-2", "status": "pending"})
    )
    api = make_client()
    api.create_document_check("doc-1111-2222")
    assert route.called
    api.close()
```

- [x] **Step 2: 跑测试确认失败**

```bash
cd knowledge-web && python -m pytest tests/test_plagiarism.py -v
```
预期：`AttributeError: 'KbClient' object has no attribute 'create_text_check'`

- [x] **Step 3: 拆出 `_send`**

替换 `kbweb/client.py:36-47` 的 `_request`：

```python
    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Everything auth, timeout and error-shaped happens here."""
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            logger.warning("backend unreachable: %s %s (%s)", method, path, exc)
            raise BackendUnavailable(type(exc).__name__) from exc

        if response.status_code >= 400:
            raise BackendError(response.status_code, *_parse_error(response))
        return response

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._send(method, path, **kwargs)
        if not response.content:
            return None
        return response.json()
```

- [x] **Step 4: 加 plagiarism 方法**

先在模块顶部导入 `from contextlib import contextmanager`。追加 plagiarism 方法到 `kbweb/client.py`，放在 `# --- jobs & ops` 之前；同时扩展既有 `get_chunks`，仅在版本非空时发送参数：

```python
    def get_chunks(
        self,
        document_id: str,
        *,
        from_ordinal: int = 0,
        limit: int = 20,
        version: int | None = None,
    ) -> list[dict]:
        params = {"from_ordinal": from_ordinal, "limit": limit}
        if version is not None:
            params["version"] = version
        return self._request(
            "GET", f"/v1/documents/{document_id}/chunks", params=params
        ) or []
```

随后加入 plagiarism 方法：

```python
    # --- plagiarism -----------------------------------------------------

    def corpus_status(self) -> dict:
        return self._request("GET", "/v1/plagiarism/corpus/status")

    def create_text_check(
        self, *, text: str, language: str = "auto", idempotency_key: str = ""
    ) -> dict:
        return self._request(
            "POST",
            "/v1/plagiarism/checks",
            json={"text": text, "language": language},
            headers=_idempotency(idempotency_key),
        )

    def create_document_check(self, document_id: str, *, idempotency_key: str = "") -> dict:
        return self._request(
            "POST",
            f"/v1/plagiarism/checks/documents/{document_id}",
            headers=_idempotency(idempotency_key),
        )

    def list_checks(self, *, limit: int = 50, offset: int = 0) -> list[dict]:
        return (
            self._request(
                "GET", "/v1/plagiarism/checks", params={"limit": limit, "offset": offset}
            )
            or []
        )

    def get_check(self, check_id: str) -> dict:
        return self._request("GET", f"/v1/plagiarism/checks/{check_id}")

    def get_plag_report(self, check_id: str) -> dict:
        return self._request("GET", f"/v1/plagiarism/checks/{check_id}/report")

    def delete_check(self, check_id: str) -> int:
        """Returns the status code: 204 is gone, 202 is cancellation requested.

        The distinction is the whole answer here, so the body is not what the
        caller wants.
        """
        return self._send("DELETE", f"/v1/plagiarism/checks/{check_id}").status_code

    @contextmanager
    def stream_progress(self, check_id: str, *, last_event_id: str = ""):
        """Open an SSE stream with the same error contract as ordinary calls."""
        headers = {"Accept": "text/event-stream"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        try:
            with self._client.stream(
                "GET", f"/v1/plagiarism/checks/{check_id}/progress", headers=headers
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise BackendError(response.status_code, *_parse_error(response))
                if not response.headers.get("Content-Type", "").startswith("text/event-stream"):
                    response.read()
                    raise BackendUnavailable("upstream progress response is not SSE")
                yield response
        except httpx.HTTPError as exc:
            logger.warning("backend SSE unavailable for %s: %s", check_id, exc)
            raise BackendUnavailable(type(exc).__name__) from exc
```

并在模块末尾 `_parse_error` 之前加：

```python
def _idempotency(key: str) -> dict[str, str]:
    return {"Idempotency-Key": key} if key else {}
```

- [x] **Step 5: 跑测试确认通过**

补测试断言：来源版本会作为 `version` 查询参数发送；`stream_progress` 的上游 404/503 会抛 `BackendError`，网络失败会抛 `BackendUnavailable`，而不是伪装成成功 SSE。

```bash
cd knowledge-web && python -m pytest tests/test_plagiarism.py tests/test_views.py -v
```
预期：全部 PASS（`test_views.py` 一并跑，确认 `_send` 拆分没有破坏既有调用）

- [x] **Step 6: 提交**

```bash
git add knowledge-web/kbweb/client.py knowledge-web/tests/test_plagiarism.py
git commit -m "feat(web): KbClient 的抄袭检测方法

delete_check 返回状态码而不是 body：204 已删除与 202 已请求取消的
区分正是调用方要的全部信息。

顺带把 _request 拆成 _send + _request，流式与状态码两种用法都需要
拿到 response 本身。"
```

---

### Task 3: 提交页、历史列表与所有提交入口

**Files:**
- Create: `kbweb/views/plagiarism.py`、`kbweb/templates/plagiarism.html`
- Modify: `kbweb/__init__.py:102-109`、`kbweb/views/library.py`、`kbweb/templates/base.html:31-40`、`kbweb/templates/document.html`、`tests/conftest.py`、`tests/test_nojs.py:38`、`kbweb/static/css/app.css`
- Test: `tests/test_plagiarism.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `corpus_status`、`create_text_check`、`create_document_check`、`list_checks`
- Produces:
  - blueprint `plagiarism.bp`，`url_prefix="/plagiarism"`
  - endpoint 名：`plagiarism.index`、`plagiarism.submit`、`plagiarism.submit_document`、`plagiarism.detail`、`plagiarism.delete`、`plagiarism.events`
  - 模板变量 `corpus`、`checks`、`form_token`

- [x] **Step 1: 加 fixture**

追加到 `tests/conftest.py` 末尾：

```python
@pytest.fixture
def corpus_ready_payload() -> dict:
    return {
        "total_documents": 201,
        "ready_documents": 201,
        "pending_documents": 0,
        "failed_documents": 0,
        "algorithm_config_hash": "cfg-abc123",
        "is_ready": True,
    }


@pytest.fixture
def corpus_pending_payload() -> dict:
    return {
        "total_documents": 1580,
        "ready_documents": 1203,
        "pending_documents": 377,
        "failed_documents": 0,
        "algorithm_config_hash": "cfg-abc123",
        "is_ready": False,
    }


@pytest.fixture
def checks_payload() -> list[dict]:
    return [
        {
            "check_id": "chk-done",
            "status": "completed",
            "created_at": "2026-08-08T10:00:00",
            "snapshot_at": "2026-08-08T10:00:00",
            "algorithm_config_hash": "cfg-abc123",
            "source_document_id": None,
            "query_chars": 1000,
            "matched_chars": 234,
        },
        {
            "check_id": "chk-live",
            "status": "running",
            "created_at": "2026-08-08T10:05:00",
            "snapshot_at": "2026-08-08T10:05:00",
            "algorithm_config_hash": "cfg-abc123",
            "source_document_id": "doc-1111-2222",
            "query_chars": 0,
            "matched_chars": 0,
        },
    ]
```

- [x] **Step 2: 写失败测试**

追加到 `tests/test_plagiarism.py`：

```python
def html(response) -> str:
    return response.data.decode("utf-8")


def stub_corpus(payload):
    respx.get(f"{API_BASE}/v1/plagiarism/corpus/status").mock(
        return_value=httpx.Response(200, json=payload)
    )


def stub_checks(payload):
    respx.get(f"{API_BASE}/v1/plagiarism/checks").mock(
        return_value=httpx.Response(200, json=payload)
    )


@respx.mock
def test_submit_page_renders_the_form_and_history(
    client, corpus_ready_payload, checks_payload
):
    stub_corpus(corpus_ready_payload)
    stub_checks(checks_payload)
    body = html(client.get("/plagiarism/"))
    assert "<textarea" in body
    assert 'method="post"' in body
    assert "chk-done" in body
    assert "chk-live" in body


@respx.mock
def test_submit_is_disabled_while_the_corpus_is_still_building(
    client, corpus_pending_payload, checks_payload
):
    stub_corpus(corpus_pending_payload)
    stub_checks(checks_payload)
    body = html(client.get("/plagiarism/"))
    assert "disabled" in body
    assert "1,203" in body and "1,580" in body


@respx.mock
def test_empty_corpus_says_to_import_first(client, checks_payload):
    stub_corpus(
        {
            "total_documents": 0,
            "ready_documents": 0,
            "pending_documents": 0,
            "failed_documents": 0,
            "algorithm_config_hash": "cfg-abc123",
            "is_ready": False,
        }
    )
    stub_checks(checks_payload)
    body = html(client.get("/plagiarism/"))
    assert "书库为空" in body


@respx.mock
def test_submitting_text_forwards_the_token_and_redirects_to_the_check(
    client, corpus_ready_payload, checks_payload
):
    stub_corpus(corpus_ready_payload)
    stub_checks(checks_payload)
    route = respx.post(f"{API_BASE}/v1/plagiarism/checks").mock(
        return_value=httpx.Response(202, json={"check_id": "chk-new", "status": "pending"})
    )

    response = client.post(
        "/plagiarism/", data={"text": "夫天地者，万物之逆旅也", "form_token": "tok-xyz"}
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/plagiarism/checks/chk-new")
    assert route.calls.last.request.headers["Idempotency-Key"] == "tok-xyz"


@respx.mock
def test_oversized_input_is_rejected_before_reaching_the_backend(
    client, corpus_ready_payload, checks_payload
):
    """Server-side, not just a JS counter - nojs must be rejected too."""
    stub_corpus(corpus_ready_payload)
    stub_checks(checks_payload)
    route = respx.post(f"{API_BASE}/v1/plagiarism/checks")

    response = client.post(
        "/plagiarism/", data={"text": "字" * 500_001, "form_token": "tok-xyz"}
    )

    assert not route.called
    assert response.status_code == 302


@respx.mock
def test_empty_submission_is_rejected_before_reaching_the_backend(
    client, corpus_ready_payload, checks_payload
):
    stub_corpus(corpus_ready_payload)
    stub_checks(checks_payload)
    route = respx.post(f"{API_BASE}/v1/plagiarism/checks")
    client.post("/plagiarism/", data={"text": "   ", "form_token": "tok-xyz"})
    assert not route.called


@respx.mock
def test_concurrency_limit_is_explained_rather_than_shown_as_a_raw_error(
    client, corpus_ready_payload, checks_payload
):
    stub_corpus(corpus_ready_payload)
    stub_checks(checks_payload)
    respx.post(f"{API_BASE}/v1/plagiarism/checks").mock(
        return_value=httpx.Response(
            429,
            json={
                "error": {
                    "code": "plagiarism_concurrency_limit",
                    "message": "too many checks already running",
                    "detail": {"active": 2, "limit": 2},
                }
            },
        )
    )
    response = client.post("/plagiarism/", data={"text": "夫天地者", "form_token": "t"})
    assert response.status_code == 302
    body = html(client.get("/plagiarism/", follow_redirects=True))
    assert "已有" in body


@respx.mock
def test_feature_disabled_gets_its_own_explanation(client, checks_payload):
    respx.get(f"{API_BASE}/v1/plagiarism/corpus/status").mock(
        return_value=httpx.Response(
            503,
            json={
                "error": {
                    "code": "feature_disabled",
                    "message": "plagiarism detection is disabled",
                    "detail": {},
                }
            },
        )
    )
    response = client.get("/plagiarism/")
    assert response.status_code == 200
    assert "当前部署未启用抄袭检测" in html(response)


@respx.mock
def test_document_page_offers_a_check_button(client, document_payload, chunks_payload):
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222").mock(
        return_value=httpx.Response(200, json=document_payload)
    )
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222/chunks").mock(
        return_value=httpx.Response(200, json=chunks_payload)
    )
    body = html(client.get("/library/doc-1111-2222"))
    assert "/plagiarism/documents/doc-1111-2222" in body


@respx.mock
def test_document_check_redirects_to_the_new_check(client):
    route = respx.post(f"{API_BASE}/v1/plagiarism/checks/documents/doc-1111-2222").mock(
        return_value=httpx.Response(202, json={"check_id": "chk-doc", "status": "pending"})
    )
    response = client.post("/plagiarism/documents/doc-1111-2222", data={"form_token": "t"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/plagiarism/checks/chk-doc")
    assert route.calls.last.request.headers["Idempotency-Key"] == "t"
```

- [x] **Step 3: 跑测试确认失败**

```bash
cd knowledge-web && python -m pytest tests/test_plagiarism.py -v -k "submit or corpus or document or concurrency or disabled or oversized or empty"
```
预期：404，因为路由还不存在

- [x] **Step 4: 写视图**

新建 `kbweb/views/plagiarism.py`：

```python
"""Plagiarism checks: submit, watch, read the report.

The detail route deliberately serves both progress and report from one URL.
That is what lets the no-script path work: a `<meta refresh>` pointed at this
URL turns into the report by itself once the check finishes, with no redirect
logic to get wrong.
"""

from __future__ import annotations

import uuid

from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..errors import BackendError
from ._common import client, settings

bp = Blueprint("plagiarism", __name__, url_prefix="/plagiarism")

MAX_INPUT_CHARS = 500_000  # mirrors kbsvc plag_max_input_chars
TERMINAL = {"completed", "completed_partial", "failed", "cancelled"}
REPORTABLE = {"completed", "completed_partial"}


@bp.get("/")
def index():
    api = client()
    corpus, corpus_error = _corpus_or_reason(api)
    checks = api.list_checks(limit=50) if corpus_error is None else []
    return render_template(
        "plagiarism.html",
        corpus=corpus,
        corpus_error=corpus_error,
        checks=checks,
        max_chars=MAX_INPUT_CHARS,
        # A fresh token per render: resubmitting the same form hits the same
        # idempotency key, so a double-click or a refresh cannot burn one of
        # the two concurrent-check slots.
        form_token=uuid.uuid4().hex,
    )


@bp.post("/")
def submit():
    text = (request.form.get("text") or "").strip()
    if not text:
        flash("请先粘贴要检测的文字", "error")
        return redirect(url_for("plagiarism.index"))
    if len(text) > MAX_INPUT_CHARS:
        flash(f"超出上限：{len(text):,} 字，最多 {MAX_INPUT_CHARS:,} 字", "error")
        return redirect(url_for("plagiarism.index"))

    try:
        created = client().create_text_check(
            text=text, idempotency_key=request.form.get("form_token") or ""
        )
    except BackendError as exc:
        flash(_explain(exc), "error")
        return redirect(url_for("plagiarism.index"))
    return redirect(url_for("plagiarism.detail", check_id=created["check_id"]))


@bp.post("/documents/<document_id>")
def submit_document(document_id: str):
    try:
        created = client().create_document_check(
            document_id, idempotency_key=request.form.get("form_token") or ""
        )
    except BackendError as exc:
        flash(_explain(exc), "error")
        return redirect(url_for("library.document", document_id=document_id))
    return redirect(url_for("plagiarism.detail", check_id=created["check_id"]))


def _corpus_or_reason(api) -> tuple[dict | None, str | None]:
    """The corpus call is also how we discover the feature is switched off."""
    try:
        return api.corpus_status(), None
    except BackendError as exc:
        if exc.code == "feature_disabled":
            return None, "当前部署未启用抄袭检测——该功能需要 PostgreSQL。"
        raise


def _explain(exc: BackendError) -> str:
    if exc.code == "plagiarism_concurrency_limit":
        limit = (exc.detail or {}).get("limit", "若干")
        return f"已有 {limit} 个检测在跑。等一个跑完，或到下方列表里取消一个。"
    if exc.code in {"plagiarism_input_too_large", "validation_error"}:
        return f"输入不合法：{exc.message}"
    if exc.code == "plagiarism_corpus_not_ready":
        return "语料仍在准备中，请稍后再试。"
    if exc.code == "plagiarism_corpus_empty":
        return "书库为空，请先导入典籍。"
    if exc.code == "idempotency_conflict":
        return "这张表单已用于另一份内容，请返回后重新提交。"
    return f"提交失败：{exc.message}"
```

为上述每个真实错误码补一个视图测试；至少断言状态码与完整专用文案，避免仅靠后端伪造的 message 让错误分支测试“碰巧通过”。

`library` blueprint 的文档详情 endpoint 已核实为 `library.document`，阅读页为 `library.read`。

- [x] **Step 5: 锁定 `BackendError` 与错误码契约**

```bash
cd knowledge-web && python -m pytest tests/test_plagiarism.py -v -k "error or disabled or corpus"
```
现有 `BackendError` 已核实具有 `status` / `code` / `message` / `detail`；测试直接锁定这些字段与真实后端错误码，不再把它们留作执行期占位检查。

- [x] **Step 6: 写模板**

新建 `kbweb/templates/plagiarism.html`：

```html
{% extends "base.html" %}
{% block title %}查重{% endblock %}

{% block content %}
<h1 class="section-title">稿件查重</h1>

{% if corpus_error %}
  <p class="flash flash--warn">{{ corpus_error }}</p>
{% else %}
  {% set ready = corpus.is_ready %}
  {% set empty = corpus.total_documents == 0 %}

  {% if empty %}
    <p class="flash">书库为空，先<a href="{{ url_for('ingest.index') }}">导入典籍</a>再来查重。</p>
  {% elif not ready %}
    <p class="flash">语料准备中 {{ "{:,}".format(corpus.ready_documents) }} / {{ "{:,}".format(corpus.total_documents) }}，稍后再试。</p>
  {% endif %}

  <form class="panel" method="post" action="{{ url_for('plagiarism.submit') }}">
    <input type="hidden" name="form_token" value="{{ form_token }}">
    <label class="field">
      <span class="field__label">待检文字</span>
      <textarea class="field__input" name="text" rows="14"
                maxlength="{{ max_chars }}"
                placeholder="粘贴要检测的稿件……"
                {% if not ready %}disabled{% endif %}></textarea>
    </label>
    <p class="field__hint">最多 {{ "{:,}".format(max_chars) }} 字。</p>
    <button class="btn" type="submit" {% if not ready %}disabled{% endif %}>开始查重</button>
  </form>
{% endif %}

<h2 class="section-title">检测记录</h2>
<div class="table-wrap">
  <table class="table">
    <thead>
      <tr><th>状态</th><th>检测</th><th>字数</th><th>重复</th><th>创建</th></tr>
    </thead>
    <tbody>
      {% for check in checks %}
        <tr>
          <td><span class="pill pill--{{ check.status | check_tone }}">{{ check.status }}</span></td>
          <td class="table__mono">
            <a href="{{ url_for('plagiarism.detail', check_id=check.check_id) }}">{{ check.check_id | short_id }}</a>
            {% if check.source_document_id %}<span class="table__note">文档</span>{% endif %}
          </td>
          <td class="table__mono">{{ "{:,}".format(check.query_chars) }}</td>
          <td class="table__mono">{{ "{:,}".format(check.matched_chars) }}</td>
          <td class="table__mono" title="{{ check.created_at }}">{{ check.created_at | timeago }}</td>
        </tr>
      {% else %}
        <tr><td colspan="5" class="table__mono">还没有检测记录</td></tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% endblock %}
```

- [x] **Step 7: 加 `check_tone` 过滤器**

追加到 `kbweb/filters.py`：

```python
_CHECK_TONE = {
    "completed": "ok",
    # Partial is not a success: the run stopped early and the number it
    # produced is a floor, not a verdict.
    "completed_partial": "warn",
    "failed": "warn",
    "cancelled": "warn",
    "pending": "",
    "running": "running",
    "cancel_requested": "running",
}


def check_tone(status: str) -> str:
    return _CHECK_TONE.get(status, "")
```

并注册进 `register()` 的 filters 字典：`"check_tone": check_tone,`

- [x] **Step 8: 注册 blueprint 与导航**

`kbweb/__init__.py:103-109` 改为：

```python
    from .views import api, ingest, jobs, library, plagiarism, search

    app.register_blueprint(search.bp)
    app.register_blueprint(library.bp)
    app.register_blueprint(ingest.bp)
    app.register_blueprint(plagiarism.bp)
    app.register_blueprint(jobs.bp)
    app.register_blueprint(api.bp)
```

`kbweb/templates/base.html`，在「导入」与「任务」之间插入：

```html
    <a class="masthead__link" href="{{ url_for('plagiarism.index') }}"
       {% if request.endpoint and request.endpoint.startswith('plagiarism.') %}aria-current="page"{% endif %}>查重</a>
```

`kbweb/views/library.py` 顶部导入 `uuid`，并把 `document()` 的模板调用改为：

```python
    return render_template(
        "document.html",
        document=doc,
        preview=preview,
        plagiarism_form_token=uuid.uuid4().hex,
    )
```

随后在 `kbweb/templates/document.html` 的「维护」面板里加一个表单（放在该 panel 内已有控件之后）：

```html
  <form method="post" action="{{ url_for('plagiarism.submit_document', document_id=document.id) }}"
        style="margin-top: var(--space-4)">
    <input type="hidden" name="form_token" value="{{ plagiarism_form_token }}">
    <button class="btn btn--sm" type="submit">查重</button>
  </form>
```

- [x] **Step 9: 把新页面纳入无脚本契约**

`tests/test_nojs.py:38` 改为：

```python
PAGES = ["/", "/library", "/ingest/", "/jobs/", "/plagiarism/"]
```

并在 `_stub_everything` 里补两条：

```python
    respx.get(f"{API_BASE}/v1/plagiarism/corpus/status").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_documents": 1,
                "ready_documents": 1,
                "pending_documents": 0,
                "failed_documents": 0,
                "algorithm_config_hash": "cfg",
                "is_ready": True,
            },
        )
    )
    respx.get(f"{API_BASE}/v1/plagiarism/checks").mock(return_value=httpx.Response(200, json=[]))
```

- [x] **Step 10: 加样式**

追加到 `kbweb/static/css/app.css`：

```css
/* --- 查重 ------------------------------------------------------------ */

.field { display: block; margin-bottom: var(--space-4); }
.field__label { display: block; margin-bottom: var(--space-2); font-family: var(--font-sans); font-size: 0.875rem; }
.field__input { width: 100%; font-family: var(--font-song); font-size: 1rem; line-height: 1.9; padding: var(--space-3); }
.field__input:disabled { opacity: 0.5; cursor: not-allowed; }
.field__hint { font-size: 0.8125rem; opacity: 0.7; margin-bottom: var(--space-4); }
.table__note { font-size: 0.75rem; opacity: 0.6; margin-left: var(--space-2); }
.pill--warn { background: var(--ochre-wash); color: var(--ochre); }
.flash--warn { border-left-color: var(--ochre); background: var(--ochre-wash); color: var(--ochre); }
```

- [x] **Step 11: 跑测试确认通过**

```bash
cd knowledge-web && python -m pytest tests/ -v
```
预期：全部 PASS，包括 js 与 nojs 两个维度

- [x] **Step 12: 提交**

```bash
git add knowledge-web/kbweb knowledge-web/tests
git commit -m "feat(web): 查重提交页、历史列表与提交入口

表单里的一次性 token 直接当 Idempotency-Key 用：双击或提交后刷新
会命中同一个 key，后端返回同一个 check，不会白占掉两个并发额度里
的一个。

语料未就绪时禁用提交而不是让用户交一个必然查不出东西的检测。
feature_disabled 单独解释，不走通用后端错误页；提交错误按真实 plagiarism
错误码分流，不让测试里的伪契约掩盖生产行为。"
```

---

### Task 4: 详情页进度态、无脚本轮询与取消

**Files:**
- Modify: `kbweb/views/plagiarism.py`、`kbweb/templates/base.html`
- Create: `kbweb/templates/check.html`、`kbweb/templates/partials/_check_progress.html`
- Test: `tests/test_plagiarism.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `get_check`、`delete_check`
- Produces: 模板变量 `check`、`live`；`check.html` 的 `{% block report %}` 供 Task 5 填充

- [x] **Step 1: 写失败测试**

追加到 `tests/test_plagiarism.py`：

```python
def stub_check(check_id: str, status: str, **extra):
    payload = {
        "check_id": check_id,
        "status": status,
        "created_at": "2026-08-08T10:00:00",
        "snapshot_at": "2026-08-08T10:00:00",
        "algorithm_config_hash": "cfg-abc123",
        "source_document_id": None,
        "query_chars": 1000,
        "matched_chars": 234,
    }
    payload.update(extra)
    respx.get(f"{API_BASE}/v1/plagiarism/checks/{check_id}").mock(
        return_value=httpx.Response(200, json=payload)
    )


@respx.mock
def test_running_check_refreshes_itself_only_without_scripts(client, config):
    stub_check("chk-live", "running")
    body = html(client.get("/plagiarism/checks/chk-live"))
    assert ('http-equiv="refresh"' in body) is config.nojs
    assert "检测中" in body


@respx.mock
def test_pending_check_follows_the_same_nojs_refresh_rule(client, config):
    stub_check("chk-wait", "pending")
    body = html(client.get("/plagiarism/checks/chk-wait"))
    assert ('http-equiv="refresh"' in body) is config.nojs


@respx.mock
def test_cancel_requested_is_live_and_follows_the_nojs_refresh_rule(client, config):
    stub_check("chk-stopping", "cancel_requested")
    body = html(client.get("/plagiarism/checks/chk-stopping"))
    assert ('http-equiv="refresh"' in body) is config.nojs
    assert "正在停止" in body


@respx.mock
def test_failed_check_stops_refreshing(client):
    """A terminal page that keeps reloading burns the backend forever."""
    stub_check("chk-bad", "failed")
    body = html(client.get("/plagiarism/checks/chk-bad"))
    assert 'http-equiv="refresh"' not in body
    assert "失败" in body


@respx.mock
def test_cancelled_check_stops_refreshing(client):
    stub_check("chk-stop", "cancelled")
    body = html(client.get("/plagiarism/checks/chk-stop"))
    assert 'http-equiv="refresh"' not in body
    assert "已取消" in body


@respx.mock
def test_cancel_is_a_real_form_post(client):
    stub_check("chk-live", "running")
    body = html(client.get("/plagiarism/checks/chk-live"))
    assert 'method="post"' in body
    assert "/plagiarism/checks/chk-live/delete" in body


@respx.mock
def test_deleting_a_finished_check_returns_to_the_list(client):
    respx.delete(f"{API_BASE}/v1/plagiarism/checks/chk-done").mock(
        return_value=httpx.Response(204)
    )
    response = client.post("/plagiarism/checks/chk-done/delete")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/plagiarism/")


@respx.mock
def test_cancelling_a_running_check_stays_on_the_check(client):
    respx.delete(f"{API_BASE}/v1/plagiarism/checks/chk-live").mock(
        return_value=httpx.Response(202, json={"outcome": "cancel_requested"})
    )
    response = client.post("/plagiarism/checks/chk-live/delete")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/plagiarism/checks/chk-live")


@respx.mock
def test_unknown_check_is_a_normal_404_page(client):
    respx.get(f"{API_BASE}/v1/plagiarism/checks/chk-nope").mock(
        return_value=httpx.Response(
            404,
            json={
                "error": {
                    "code": "plagiarism_check_not_found",
                    "message": "no such check",
                    "detail": {},
                }
            },
        )
    )
    assert client.get("/plagiarism/checks/chk-nope").status_code == 404
```

- [x] **Step 2: 跑测试确认失败**

```bash
cd knowledge-web && python -m pytest tests/test_plagiarism.py -v -k "check or cancel or delet"
```
预期：404，路由不存在

- [x] **Step 3: 加路由**

追加到 `kbweb/views/plagiarism.py`：

```python
@bp.get("/checks/<check_id>")
def detail(check_id: str):
    check = client().get_check(check_id)
    return render_template(
        "check.html",
        check=check,
        report=None,
        live=check["status"] not in TERMINAL,
        nojs_refresh_seconds=5,
    )


@bp.post("/checks/<check_id>/delete")
def delete(check_id: str):
    status = client().delete_check(check_id)
    if status == 202:
        flash("已请求取消，正在停止", "ok")
        return redirect(url_for("plagiarism.detail", check_id=check_id))
    flash("已删除", "ok")
    return redirect(url_for("plagiarism.index"))
```

- [x] **Step 4: 写模板**

先在 `kbweb/templates/base.html` 的 `</head>` 前增加合法的 head 扩展点：

```html
{% block head %}{% endblock %}
```

新建 `kbweb/templates/check.html`，把 refresh 放进 head block，不得输出在 `<main>` / `<body>` 中：

```html
{% extends "base.html" %}
{% block title %}检测 {{ check.check_id | short_id }}{% endblock %}

{% block head %}
  {% if live and nojs %}
    <meta http-equiv="refresh" content="{{ nojs_refresh_seconds }}">
  {% endif %}
{% endblock %}

{% block content %}
<h1 class="section-title">检测 <span class="table__mono">{{ check.check_id | short_id }}</span></h1>

{% if live %}
  {% include "partials/_check_progress.html" %}
{% elif check.status in ('completed', 'completed_partial') %}
  {% include "partials/_check_report.html" %}
{% elif check.status == 'failed' %}
  <p class="flash flash--warn">检测失败。可以回到<a href="{{ url_for('plagiarism.index') }}">查重页</a>重新提交。</p>
{% else %}
  <p class="flash">这次检测已取消。</p>
{% endif %}

<form method="post" action="{{ url_for('plagiarism.delete', check_id=check.check_id) }}"
      style="margin-top: var(--space-6)">
  <button class="btn btn--ghost btn--sm" type="submit">
    {% if live %}取消检测{% else %}删除记录{% endif %}
  </button>
</form>
{% endblock %}
```

新建 `kbweb/templates/partials/_check_progress.html`：

```html
<div class="panel progress" data-check-progress
     data-check-id="{{ check.check_id }}"
     data-events-url="{{ url_for('plagiarism.events', check_id=check.check_id) }}">
  <p class="progress__stage" data-progress-stage>
    {% if check.status == 'pending' %}排队中{% elif check.status == 'cancel_requested' %}正在停止{% else %}检测中{% endif %}
  </p>
  <div class="progress__track">
    <div class="progress__bar" data-progress-bar style="width: 0%"></div>
  </div>
  <p class="progress__hint">
    检测通常在一分钟内完成。
    {% if nojs %}本页每 {{ nojs_refresh_seconds }} 秒自动刷新。{% endif %}
  </p>
</div>
```

暂时留一个空的 `kbweb/templates/partials/_check_report.html`，Task 5 填充：

```html
<p class="flash">报告渲染尚未接入。</p>
```

- [x] **Step 5: `plagiarism.events` 端点占位**

Task 8 才实现真正的 SSE 代理，但 `url_for` 现在就要能解析。追加到 `kbweb/views/plagiarism.py`：

```python
@bp.get("/checks/<check_id>/events")
def events(check_id: str):
    """Filled in by the SSE task; the route exists now so `url_for` resolves."""
    return "", 204
```

- [x] **Step 6: 加样式**

追加到 `kbweb/static/css/app.css`：

```css
.progress__stage { font-family: var(--font-kai); font-size: 1.25rem; margin-bottom: var(--space-3); }
.progress__track { height: 4px; background: var(--ochre-wash); border-radius: 2px; overflow: hidden; }
.progress__bar { height: 100%; background: var(--ochre); transition: width var(--duration-normal) ease-out; }
.progress__hint { margin-top: var(--space-3); font-size: 0.8125rem; opacity: 0.7; }
```

- [x] **Step 7: 跑测试确认通过**

```bash
cd knowledge-web && python -m pytest tests/ -v
```
预期：全部 PASS

- [x] **Step 8: 提交**

```bash
git add knowledge-web/kbweb knowledge-web/tests
git commit -m "feat(web): 检测详情页的进度态与取消

进度与报告共用一个 URL，按 status 分支。meta refresh 只在非终态输出——
终态还刷就是让报告页永远重载后端。

取消与删除共用一个 POST 端点，按后端返回的 204/202 分流：一个是已经
没了，一个是请求已受理但 worker 还在跑。"
```

---

### Task 5: 报告页——判定条与来源清单

只用现有 `ReportOut` 字段，**不依赖后端 `query_text`**。全文高亮在 Task 6。

**Files:**
- Modify: `kbweb/views/plagiarism.py`、`kbweb/report.py`、`kbweb/templates/partials/_check_report.html`
- Test: `tests/test_report.py`、`tests/test_plagiarism.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `get_plag_report`
- Produces:
  - `report.numbered_sources(report: dict) -> list[dict]`（每个 source 增加 `ordinal` 键，1-based，按 `matched_chars` 降序）
  - `report.duplication_ratio(report: dict) -> float`（0–100 的百分数）
  - 模板变量 `sources`、`ratio`

- [x] **Step 1: 写装配函数的失败测试**

追加到 `tests/test_report.py`：

```python
from kbweb.report import duplication_ratio, numbered_sources


def a_report(**extra) -> dict:
    base = {
        "check_id": "chk-1",
        "status": "completed",
        "query_chars": 1000,
        "matched_chars": 234,
        "checked_chunks": 40,
        "total_chunks": 40,
        "coverage_reason": None,
        "is_complete": True,
        "sources": [],
        "unique_passages": [],
    }
    base.update(extra)
    return base


def test_sources_are_numbered_by_matched_chars_descending():
    report = a_report(
        sources=[
            {"document_id": "d-small", "matched_chars": 58, "passages": []},
            {"document_id": "d-big", "matched_chars": 142, "passages": []},
        ]
    )
    numbered = numbered_sources(report)
    assert [s["ordinal"] for s in numbered] == [1, 2]
    assert numbered[0]["document_id"] == "d-big"


def test_numbering_is_one_based_to_match_the_marker_glyphs():
    report = a_report(sources=[{"document_id": "d", "matched_chars": 1, "passages": []}])
    assert numbered_sources(report)[0]["ordinal"] == 1


def test_ratio_is_matched_over_query_chars():
    assert duplication_ratio(a_report(query_chars=1000, matched_chars=234)) == 23.4


def test_ratio_is_zero_when_nothing_was_submitted():
    """Never divide by zero just because a check failed before counting."""
    assert duplication_ratio(a_report(query_chars=0, matched_chars=0)) == 0.0
```

- [x] **Step 2: 跑测试确认失败**

```bash
cd knowledge-web && python -m pytest tests/test_report.py -v
```
预期：`ImportError: cannot import name 'numbered_sources'`

- [x] **Step 3: 实现装配函数**

追加到 `kbweb/report.py`：

```python
def numbered_sources(report: dict) -> list[dict]:
    """Sources in display order, each carrying its 1-based `ordinal`.

    The ordinal is the only thing tying a marker in the text to a row in the
    list, so ordering and numbering must be decided in one place - which is
    here, not in the template.
    """
    ordered = sorted(
        report.get("sources") or [],
        key=lambda source: source.get("matched_chars") or 0,
        reverse=True,
    )
    return [dict(source, ordinal=index) for index, source in enumerate(ordered, start=1)]


def duplication_ratio(report: dict) -> float:
    """Matched share of the submission, as a percentage.

    Callers must pair this with `is_complete`: when coverage stopped early the
    number is a floor, not a verdict.
    """
    query_chars = report.get("query_chars") or 0
    if query_chars <= 0:
        return 0.0
    return round((report.get("matched_chars") or 0) / query_chars * 100, 1)
```

- [x] **Step 4: 跑测试确认通过**

```bash
cd knowledge-web && python -m pytest tests/test_report.py -v
```
预期：全部 PASS

- [x] **Step 5: 写视图与模板的失败测试**

追加到 `tests/test_plagiarism.py`：

```python
def stub_report(check_id: str, **extra):
    payload = {
        "check_id": check_id,
        "status": "completed",
        "snapshot_at": "2026-08-08T10:00:00",
        "algorithm_config_hash": "cfg-abc123",
        "query_chars": 1000,
        "matched_chars": 234,
        "checked_chunks": 40,
        "total_chunks": 40,
        "coverage_reason": None,
        "is_complete": True,
        "sources": [
            {
                "document_id": "doc-1111-2222",
                "version_id": "ver-1",
                "version": 1,
                "content_hash": "abc",
                "title": "春夜宴从弟桃花园序",
                "matched_chars": 142,
                "score": 0.94,
                "passages": [
                    {
                        "query_start": 0,
                        "query_end": 11,
                        "source_start": 100,
                        "source_end": 111,
                        "score": 0.94,
                        "preview": "夫天地者，万物之逆旅也",
                    }
                ],
            }
        ],
        "unique_passages": [[0, 11]],
    }
    payload.update(extra)
    respx.get(f"{API_BASE}/v1/plagiarism/checks/{check_id}/report").mock(
        return_value=httpx.Response(200, json=payload)
    )


@respx.mock
def test_complete_report_states_the_ratio_plainly(client, chunks_payload):
    stub_check("chk-done", "completed")
    stub_report("chk-done")
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222/chunks").mock(
        return_value=httpx.Response(200, json=chunks_payload)
    )
    body = html(client.get("/plagiarism/checks/chk-done"))
    assert "23.4%" in body
    assert "已查完整篇" in body
    assert "≥" not in body
    assert "春夜宴从弟桃花园序" in body


@respx.mock
def test_partial_report_states_the_ratio_as_a_floor_and_warns(client, chunks_payload):
    """The whole point of COMPLETED_PARTIAL is that it is not a verdict."""
    stub_check("chk-part", "completed_partial")
    stub_report(
        "chk-part",
        status="completed_partial",
        checked_chunks=12,
        total_chunks=40,
        coverage_reason="time_cap",
        is_complete=False,
    )
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222/chunks").mock(
        return_value=httpx.Response(200, json=chunks_payload)
    )
    body = html(client.get("/plagiarism/checks/chk-part"))
    assert "≥" in body
    assert "未检查部分不代表没有重复" in body
    assert "12" in body and "40" in body


@respx.mock
def test_report_page_does_not_keep_refreshing(client, chunks_payload):
    stub_check("chk-done", "completed")
    stub_report("chk-done")
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222/chunks").mock(
        return_value=httpx.Response(200, json=chunks_payload)
    )
    assert 'http-equiv="refresh"' not in html(client.get("/plagiarism/checks/chk-done"))


@respx.mock
def test_visibility_change_gets_a_dedicated_409_page(client):
    stub_check("chk-hidden", "completed")
    respx.get(f"{API_BASE}/v1/plagiarism/checks/chk-hidden/report").mock(
        return_value=httpx.Response(
            409,
            json={
                "error": {
                    "code": "report_visibility_changed",
                    "message": "a source is no longer visible",
                    "detail": {"check_id": "chk-hidden"},
                }
            },
        )
    )
    response = client.get("/plagiarism/checks/chk-hidden")
    assert response.status_code == 409
    assert "来源访问权限已变化" in html(response)
```

- [x] **Step 6: 跑测试确认失败**

```bash
cd knowledge-web && python -m pytest tests/test_plagiarism.py -v -k "report"
```
预期：断言失败，报告仍是占位文案

- [x] **Step 7: 视图取报告**

改 `kbweb/views/plagiarism.py` 的 `detail`：

```python
from ..report import duplication_ratio, numbered_sources


@bp.get("/checks/<check_id>")
def detail(check_id: str):
    api = client()
    check = api.get_check(check_id)
    status = check["status"]

    report = None
    report_error = None
    if status in REPORTABLE:
        try:
            report = api.get_plag_report(check_id)
        except BackendError as exc:
            if exc.code != "report_visibility_changed":
                raise
            report_error = "来源访问权限已变化，出于安全原因无法显示这份报告。"
    sources = numbered_sources(report) if report else []

    rendered = render_template(
        "check.html",
        check=check,
        report=report,
        report_error=report_error,
        sources=sources,
        ratio=duplication_ratio(report) if report else 0.0,
        live=status not in TERMINAL,
        nojs_refresh_seconds=5,
    )
    return (rendered, 409) if report_error else rendered
```

同时把 `check.html` 的终态分支改为先判断 `report_error`，再 include 报告 partial：

```html
{% if live %}
  {% include "partials/_check_progress.html" %}
{% elif report_error %}
  <p class="flash flash--warn">{{ report_error }}</p>
{% elif check.status in ('completed', 'completed_partial') %}
  {% include "partials/_check_report.html" %}
{% elif check.status == 'failed' %}
  …
{% else %}
  …
{% endif %}
```

- [x] **Step 8: 写报告模板**

替换 `kbweb/templates/partials/_check_report.html`：

```html
{# The verdict bar. When coverage stopped early the number must read as a
   floor: a clean percentage on a judgement-oriented page is a conclusion,
   and kbsvc built COMPLETED_PARTIAL precisely so we would not draw one. #}
<div class="verdict{% if not report.is_complete %} verdict--partial{% endif %}">
  <p class="verdict__figure">
    {% if not report.is_complete %}<span class="verdict__floor">≥</span>{% endif %}{{ ratio }}%
  </p>
  <p class="verdict__label">
    {{ sources | length }} 个来源 ·
    {% if report.is_complete %}已查完整篇{% else %}覆盖不全{% endif %}
  </p>
</div>

{% if not report.is_complete %}
  <p class="flash flash--warn">
    仅检查了 {{ "{:,}".format(report.checked_chunks) }} / {{ "{:,}".format(report.total_chunks) }} 段（{% if report.coverage_reason == 'time_cap' %}时间预算用尽{% else %}检测被取消{% endif %}）。<strong>未检查部分不代表没有重复。</strong>
  </p>
{% endif %}

<h2 class="section-title">来源</h2>
<ol class="sources">
  {% for source in sources %}
    <li class="source" id="source-{{ source.ordinal }}">
      <p class="source__title">
        <span class="source__ordinal">{{ source.ordinal }}</span>
        <a href="{{ url_for('library.read', document_id=source.document_id) }}">{{ source.title }}</a>
      </p>
      <p class="source__meta">
        命中 {{ "{:,}".format(source.matched_chars) }} 字 · score {{ source.score | score(2) }} ·
        {{ source.passages | length }} 处
      </p>
    </li>
  {% else %}
    <li class="source"><p>没有找到重复来源。</p></li>
  {% endfor %}
</ol>
```

- [x] **Step 9: 加样式**

追加到 `kbweb/static/css/app.css`：

```css
.verdict { margin: var(--space-6) 0; }
.verdict__figure { font-family: var(--font-song); font-size: clamp(3rem, 1rem + 7vw, 6rem); line-height: 1; color: var(--indigo); }
.verdict__floor { opacity: 0.65; margin-right: 0.1em; }
.verdict--partial .verdict__figure { color: var(--ochre); }
.verdict__label { font-family: var(--font-kai); font-size: 1rem; opacity: 0.8; margin-top: var(--space-2); }
.sources { list-style: none; padding: 0; }
.source { padding: var(--space-4) 0; border-top: 1px solid var(--rule, currentColor); }
.source__ordinal { display: inline-block; min-width: 1.6em; font-family: var(--font-song); color: var(--seal); }
.source__title { font-size: 1.0625rem; }
.source__meta { font-size: 0.8125rem; opacity: 0.7; margin-top: var(--space-2); }
```

若 `--ink` / `--rule` 在 `tokens.css` 中不存在，跑
`grep -n "^  --" knowledge-web/kbweb/static/css/tokens.css` 取实际变量名替换。

- [x] **Step 10: 跑测试确认通过**

```bash
cd knowledge-web && python -m pytest tests/ -v
```
预期：全部 PASS

- [x] **Step 11: 提交**

```bash
git add knowledge-web/kbweb knowledge-web/tests
git commit -m "feat(web): 报告页的判定条与来源清单

覆盖不全时重复率写成下界（≥）并强制横幅。后端专门建了
COMPLETED_PARTIAL 这个状态，注释写着它绝不能被读成「没查到抄袭」；
判定导向的页面上一个干净的百分比就是一句结论，所以这个区分必须
出现在最显眼的位置。

来源编号在 report.numbered_sources 里一次定死——它是正文角标与右栏
清单之间唯一的纽带，不能让模板各排各的。"
```

---

### Task 6: 报告页——全文高亮与角标

**依赖“后端前置契约”的完整实现。** 不能只检查 schema 中出现字段名；门禁必须同时证明文本模式和文档模式都返回正确的 `query_text`。

**Files:**
- Modify: `kbweb/report.py`、`kbweb/templates/partials/_check_report.html`
- Test: `tests/test_report.py`、`tests/test_plagiarism.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `coverage_segments`、Task 5 的 `numbered_sources`
- Produces: `report.query_spans(sources: list[dict]) -> list[tuple[int, int, int]]`

- [x] **Step 1: 确认后端字段已上线**

```bash
cd knowledge-service && python -m pytest tests/plagiarism/test_api.py -v -k "report and query_text"
```
预期：文本模式、文档模式都 PASS，并且响应中的 `len(query_text) == query_chars`。仅 grep 到字段不算完成。

- [x] **Step 2: 写 `query_spans` 的失败测试**

追加到 `tests/test_report.py`：

```python
from kbweb.report import query_spans


def test_query_spans_carry_the_source_ordinal():
    sources = [
        {
            "ordinal": 1,
            "passages": [
                {"query_start": 0, "query_end": 11},
                {"query_start": 20, "query_end": 25},
            ],
        },
        {"ordinal": 2, "passages": [{"query_start": 5, "query_end": 15}]},
    ]
    assert sorted(query_spans(sources)) == [(0, 11, 1), (5, 15, 2), (20, 25, 1)]


def test_query_spans_tolerates_a_source_with_no_passages():
    assert query_spans([{"ordinal": 1, "passages": []}]) == []
    assert query_spans([{"ordinal": 1}]) == []
```

- [x] **Step 3: 跑测试确认失败**

```bash
cd knowledge-web && python -m pytest tests/test_report.py -v -k "query_spans"
```
预期：`ImportError: cannot import name 'query_spans'`

- [x] **Step 4: 实现 `query_spans`**

追加到 `kbweb/report.py`：

```python
def query_spans(sources: list[dict]) -> list[tuple[int, int, int]]:
    """Flatten numbered sources into `(start, end, ordinal)` for coverage_segments.

    Requires sources already carrying `ordinal` - see `numbered_sources`.
    """
    return [
        (passage["query_start"], passage["query_end"], source["ordinal"])
        for source in sources
        for passage in source.get("passages") or []
    ]
```

- [x] **Step 5: 写模板渲染的失败测试**

追加到 `tests/test_plagiarism.py`：

```python
@respx.mock
def test_report_highlights_the_submission_from_backend_offsets(client, chunks_payload):
    stub_check("chk-done", "completed")
    stub_report("chk-done", query_text="夫天地者，万物之逆旅也。古人秉烛夜游，良有以也。")
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222/chunks").mock(
        return_value=httpx.Response(200, json=chunks_payload)
    )
    body = html(client.get("/plagiarism/checks/chk-done"))
    assert "<mark" in body
    assert "夫天地者，万物之逆旅也" in body
    assert "古人秉烛夜游" in body


@respx.mock
def test_a_passage_matching_two_sources_carries_both_markers(client, chunks_payload):
    stub_check("chk-two", "completed")
    stub_report(
        "chk-two",
        query_text="零一二三四五",
        sources=[
            {
                "document_id": "doc-a",
                "version_id": "v", "version": 1, "content_hash": "h",
                "title": "甲书", "matched_chars": 4, "score": 0.9,
                "passages": [
                    {"query_start": 0, "query_end": 4, "source_start": 0,
                     "source_end": 4, "score": 0.9, "preview": "零一二三"}
                ],
            },
            {
                "document_id": "doc-b",
                "version_id": "v", "version": 1, "content_hash": "h",
                "title": "乙书", "matched_chars": 4, "score": 0.8,
                "passages": [
                    {"query_start": 2, "query_end": 6, "source_start": 0,
                     "source_end": 4, "score": 0.8, "preview": "二三四五"}
                ],
            },
        ],
    )
    for document_id in ("doc-a", "doc-b"):
        respx.get(f"{API_BASE}/v1/documents/{document_id}/chunks").mock(
            return_value=httpx.Response(200, json=chunks_payload)
        )
    body = html(client.get("/plagiarism/checks/chk-two"))
    # The overlapping run must name both sources, not just the first.
    assert "#source-1" in body and "#source-2" in body


@respx.mock
def test_submission_text_is_escaped_not_injected(client, chunks_payload):
    """Highlighting is fragment concatenation; it must never emit raw HTML."""
    stub_check("chk-xss", "completed")
    stub_report("chk-xss", query_text="<script>alert(1)</script>夫天地者", sources=[],
                unique_passages=[], matched_chars=0)
    body = html(client.get("/plagiarism/checks/chk-xss"))
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body
```

- [x] **Step 6: 跑测试确认失败**

```bash
cd knowledge-web && python -m pytest tests/test_plagiarism.py -v -k "highlight or markers or escaped"
```
预期：断言失败，正文尚未渲染

- [x] **Step 7: 视图传 spans**

改 `kbweb/views/plagiarism.py` 的 `detail`，在 `sources` 之后加：

```python
from ..report import duplication_ratio, numbered_sources, query_spans
```

并在 `render_template` 调用里加一个参数：

```python
        spans=query_spans(sources),
```

- [x] **Step 8: 在两栏报告布局里渲染正文**

在 `_check_report.html` 的 verdict/告警块之后增加 `.report-layout`：左侧放全文，右侧把 Task 5 已有的“来源”标题与 `<ol class="sources">` 整体移入 sticky aside。不得继续按上下顺序排列后却在 Self-Review 中声称是两栏。

```html
<div class="report-layout">
  <section class="report-layout__submission" aria-label="待检全文">
    {% if report.query_text %}
      <div class="submission">
        {% for fragment, owners in coverage_segments(report.query_text, spans) %}
          {%- if owners -%}
            <mark class="hit">{{ fragment }}<span class="hit__markers"
              >{% for ordinal in owners | sort %}<a class="hit__marker" href="#source-{{ ordinal }}">{{ ordinal }}</a>{% endfor %}</span
            ></mark>
          {%- else -%}
            {{ fragment }}
          {%- endif -%}
        {% endfor %}
      </div>
    {% else %}
      <p class="flash">原文已按保留策略清理，无法显示全文高亮。</p>
    {% endif %}
  </section>
  <aside class="report-layout__sources" aria-label="命中来源">
    {# 把 Task 5 的来源标题与 ol.sources 原样移到这里；Task 7 的 details 仍放在对应 li 内。 #}
  </aside>
</div>
```

`{{ fragment }}` 走 Jinja 默认转义，这就是「分段拼接而非 `|safe`」的落点。

- [x] **Step 9: 加样式**

追加到 `kbweb/static/css/app.css`：

```css
.submission {
  font-family: var(--font-song);
  font-size: 1.0625rem;
  line-height: 2.0;
  margin: 0;
  white-space: pre-wrap;
}
.report-layout { display: grid; grid-template-columns: minmax(0, 2fr) minmax(18rem, 1fr); gap: var(--space-7); align-items: start; margin-top: var(--space-6); }
.report-layout__sources { position: sticky; top: var(--space-4); max-height: calc(100vh - 2 * var(--space-4)); overflow: auto; }
.hit { background: var(--seal-wash); color: inherit; padding: 0.05em 0; }
.hit__markers { white-space: nowrap; }
.hit__marker {
  font-family: var(--font-sans);
  font-size: 0.6875rem;
  vertical-align: super;
  color: var(--seal);
  margin-left: 0.15em;
  text-decoration: none;
}
.hit__marker:hover, .hit__marker:focus { text-decoration: underline; }
@media (max-width: 56rem) {
  .report-layout { grid-template-columns: minmax(0, 1fr); }
  .report-layout__sources { position: static; max-height: none; overflow: visible; }
}
```

- [x] **Step 10: 跑测试确认通过**

```bash
cd knowledge-web && python -m pytest tests/ -v
```
预期：全部 PASS

- [x] **Step 11: 提交**

```bash
git add knowledge-web/kbweb knowledge-web/tests
git commit -m "feat(web): 稿件全文高亮与来源角标

重叠区间用单色朱砂加角标而不是每来源一色：朱砂在本项目里是语义色
（命中），给每个来源分配色相会把这条承诺换成装饰，而且来源过五个
就不够用，对色盲也不友好。角标序号与右栏清单同一套编号。

高亮是分段拼接，每个片段走 Jinja 默认转义，没有任何 |safe。"
```

---

### Task 7: 对照卡——来源正文与降级

**Files:**
- Modify: `kbweb/report.py`、`kbweb/views/plagiarism.py`、`kbweb/templates/partials/_check_report.html`
- Test: `tests/test_report.py`、`tests/test_plagiarism.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `source_excerpt`、Task 5 的 `numbered_sources`
- Produces: `report.attach_excerpts(api, sources, *, limit=8) -> list[dict]`，按报告冻结版本分页读取，为前 `limit` 个来源的每个 passage 增加 `source_text`；单页固定不超过 200，并设置分页总页数上限

- [x] **Step 1: 写失败测试**

追加到 `tests/test_plagiarism.py`：

```python
@respx.mock
def test_passage_cards_show_both_sides(client, chunks_payload):
    stub_check("chk-done", "completed")
    stub_report("chk-done", query_text="夫天地者，万物之逆旅也。")
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222/chunks").mock(
        return_value=httpx.Response(200, json=chunks_payload)
    )
    body = html(client.get("/plagiarism/checks/chk-done"))
    assert "<details" in body          # openable with no script at all
    assert "夫天地者，万物之逆旅也" in body   # the query-side preview
    assert "到书里看" in body


@respx.mock
def test_a_source_disappearing_after_report_fetch_degrades_its_card(client):
    """Covers the race after get_report succeeds; prior revocation is a report-level 409."""
    stub_check("chk-done", "completed")
    stub_report("chk-done", query_text="夫天地者，万物之逆旅也。")
    respx.get(f"{API_BASE}/v1/documents/doc-1111-2222/chunks").mock(
        return_value=httpx.Response(
            404, json={"error": {"code": "not_found", "message": "gone", "detail": {}}}
        )
    )
    response = client.get("/plagiarism/checks/chk-done")
    assert response.status_code == 200
    body = html(response)
    assert "来源已不可访问" in body
    assert "23.4%" in body      # the rest of the report still renders


@respx.mock
def test_only_the_top_sources_have_their_text_prefetched(client, chunks_payload):
    """Request count must stay bounded by the cap, not by the source count."""
    many = [
        {
            "document_id": f"doc-{index}",
            "version_id": "v", "version": 1, "content_hash": "h",
            "title": f"书 {index}", "matched_chars": 100 - index, "score": 0.5,
            "passages": [
                {"query_start": index, "query_end": index + 1, "source_start": 0,
                 "source_end": 3, "score": 0.5, "preview": "零"}
            ],
        }
        for index in range(12)
    ]
    stub_check("chk-many", "completed")
    stub_report("chk-many", query_text="零" * 20, sources=many)
    routes = {}
    for index in range(12):
        routes[index] = respx.get(f"{API_BASE}/v1/documents/doc-{index}/chunks").mock(
            return_value=httpx.Response(200, json=chunks_payload)
        )

    client.get("/plagiarism/checks/chk-many")

    called = sum(1 for route in routes.values() if route.called)
    assert called == 8


@respx.mock
def test_source_prefetch_uses_the_frozen_version_and_paginates_at_200():
    """The backend rejects limit > 200 and current-version text may not match old offsets."""
    from kbweb.report import attach_excerpts

    requests = []

    def source_chunk(ordinal: int, text: str, char_start: int) -> dict:
        return {
            "ordinal": ordinal,
            "text": text,
            "char_start": char_start,
            "char_end": char_start + len(text),
        }

    def page_for(request):
        requests.append(request)
        start = int(request.url.params["from_ordinal"])
        rows = (
            [source_chunk(index, "字", index) for index in range(200)]
            if start == 0
            else [source_chunk(200, "命", 200)]
        )
        return httpx.Response(200, json=rows)

    respx.get(f"{API_BASE}/v1/documents/doc-old/chunks").mock(side_effect=page_for)
    api = make_client()
    source = {
        "document_id": "doc-old",
        "version": 1,
        "ordinal": 1,
        "passages": [{"source_start": 200, "source_end": 201}],
    }

    attached = attach_excerpts(api, [source])

    assert attached[0]["passages"][0]["source_text"] == "命"
    assert len(requests) == 2
    assert all(request.url.params["limit"] == "200" for request in requests)
    assert all(request.url.params["version"] == "1" for request in requests)
    api.close()
```

另加回归测试：空 passages 不发请求；页数上限触发后标记 `prefetch_truncated`；空页与 ordinal 不前进能停止；chunks 返回 422/500 时 `attach_excerpts` 必须重新抛出，不能把调用参数错误或服务端故障伪装成“来源已不可访问”。只有 403/404 做权限/删除降级，`BackendUnavailable` 使用“暂时不可用”文案。

- [x] **Step 2: 跑测试确认失败**

```bash
cd knowledge-web && python -m pytest tests/test_plagiarism.py -v -k "cards or revoked or prefetch"
```
预期：断言失败

- [x] **Step 3: 实现 `attach_excerpts`**

追加到 `kbweb/report.py`：

```python
import logging

from .errors import BackendError, BackendUnavailable

logger = logging.getLogger(__name__)

MAX_PREFETCHED_SOURCES = 8
SOURCE_CHUNK_PAGE_SIZE = 200
SOURCE_MAX_PAGES = 20


def _source_chunks(api, source: dict, passages: list[dict]) -> tuple[list[dict], bool]:
    """Read the frozen version until all requested offsets are covered or capped."""
    if not passages:
        return [], True
    target_end = max((passage.get("source_end") or 0 for passage in passages), default=0)
    chunks: list[dict] = []
    from_ordinal = 0
    covered = target_end <= 0
    for _ in range(SOURCE_MAX_PAGES):
        page = api.get_chunks(
            source["document_id"],
            from_ordinal=from_ordinal,
            limit=SOURCE_CHUNK_PAGE_SIZE,
            version=source["version"],
        )
        if not page:
            break
        chunks.extend(page)
        covered = max((chunk.get("char_end") or 0 for chunk in page), default=0) >= target_end
        if covered or len(page) < SOURCE_CHUNK_PAGE_SIZE:
            break
        next_ordinal = max((chunk.get("ordinal") or 0 for chunk in page), default=-1) + 1
        if next_ordinal <= from_ordinal:
            break
        from_ordinal = next_ordinal
    return chunks, covered


def attach_excerpts(api, sources: list[dict], *, limit: int = MAX_PREFETCHED_SOURCES) -> list[dict]:
    """Give each passage of the top `limit` sources its source-side text.

    Bounded on purpose: never one request per passage. At most `limit` source
    documents are prefetched, each in pages of <= 200 chunks and with a hard
    page cap. The rest keep a link into the reader instead.

    A source that has become unreachable degrades to an empty excerpt. Access
    can be revoked between running a check and reading its report, and one
    deleted book must not take the whole report down with it.
    """
    attached: list[dict] = []
    for index, source in enumerate(sources):
        passages = source.get("passages") or []
        if index >= limit:
            attached.append(dict(source, prefetched=False))
            continue

        try:
            chunks, covered = _source_chunks(api, source, passages)
        except BackendError as exc:
            if exc.status not in {403, 404}:
                raise
            logger.info("source text unavailable for %s: %s", source["document_id"], exc)
            attached.append(dict(source, prefetched=True, fetch_error="来源已不可访问。"))
            continue
        except BackendUnavailable as exc:
            logger.info("source service unavailable for %s: %s", source["document_id"], exc)
            attached.append(dict(source, prefetched=True, fetch_error="来源服务暂时不可用。"))
            continue

        attached.append(
            dict(
                source,
                prefetched=True,
                prefetch_truncated=not covered,
                passages=[
                    dict(
                        passage,
                        source_text=source_excerpt(
                            chunks, passage["source_start"], passage["source_end"]
                        ),
                    )
                    for passage in passages
                ],
            )
        )
    return attached
```

- [x] **Step 4: 视图接上**

改 `kbweb/views/plagiarism.py` 的 `detail`：

```python
from ..report import attach_excerpts, duplication_ratio, numbered_sources, query_spans
```

`sources` 那行改为：

```python
    sources = attach_excerpts(api, numbered_sources(report)) if report else []
```

- [x] **Step 5: 模板加对照卡**

替换 `_check_report.html` 里 `<li class="source" ...>` 的内容，在 `source__meta` 之后加：

```html
      {% for passage in source.passages %}
        <details class="passage" id="passage-{{ source.ordinal }}-{{ loop.index }}">
          <summary class="passage__summary">第 {{ loop.index }} 处 · score {{ passage.score | score(2) }}</summary>
          <div class="passage__body">
            <p class="passage__label">你的文字</p>
            <p class="passage__text">{{ passage.preview }}</p>
            <p class="passage__label">来源</p>
            {% if source.fetch_error %}
              <p class="passage__missing">{{ source.fetch_error }}</p>
            {% elif passage.source_text %}
              <p class="passage__text">{{ passage.source_text }}</p>
            {% elif source.prefetch_truncated %}
              <p class="passage__missing">来源位置超出本页预取上限，请到书里查看。</p>
            {% else %}
              <p class="passage__missing">来源正文未预取。</p>
            {% endif %}
            <p>
              <a class="btn btn--ghost btn--sm"
                 href="{{ url_for('library.read', document_id=source.document_id) }}">到书里看 →</a>
            </p>
          </div>
        </details>
      {% endfor %}
```

- [x] **Step 6: 加样式**

追加到 `kbweb/static/css/app.css`：

```css
.passage { margin-top: var(--space-3); }
.passage__summary { cursor: pointer; font-family: var(--font-sans); font-size: 0.8125rem; opacity: 0.75; }
.passage__body { padding: var(--space-3) 0 var(--space-3) var(--space-4); border-left: 2px solid var(--paper-edge); margin-top: var(--space-3); }
.passage__label { font-family: var(--font-kai); font-size: 0.8125rem; opacity: 0.65; margin-top: var(--space-3); }
.passage__text { font-family: var(--font-song); line-height: 1.9; margin-top: var(--space-2); }
.passage__missing { font-size: 0.8125rem; opacity: 0.6; margin-top: var(--space-2); }
```

- [x] **Step 7: 跑测试确认通过**

```bash
cd knowledge-web && python -m pytest tests/ -v
```
预期：全部 PASS

- [x] **Step 8: 提交**

```bash
git add knowledge-web/kbweb knowledge-web/tests
git commit -m "feat(web): 命中对照卡与来源不可访问的降级

对照卡用 <details>，展开是纯 HTML 行为，无脚本下照样能开。

来源正文不按每处命中单独请求，而是对前 8 个不同来源按冻结版本、
每页最多 200 chunks 分页读取，并设置总页数上限。报告读取前已撤权
仍按后端 fail-closed 的 409 处理；仅报告取得后的 403/404 竞态降级单卡。"
```

---

### Task 8: SSE 代理、进度脚本与线程数

**Files:**
- Modify: `kbweb/views/plagiarism.py`、`kbweb/templates/check.html`、`deploy/Dockerfile:29`
- Create: `kbweb/static/js/check.js`
- Test: `tests/test_plagiarism.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `stream_progress`
- Produces: `/plagiarism/checks/<id>/events`，`text/event-stream`

- [x] **Step 1: 写失败测试**

追加到 `tests/test_plagiarism.py`：

```python
@respx.mock
def test_events_proxy_forwards_last_event_id_upstream(client):
    """Without this header the backend replays the whole stream on reconnect."""
    stub_check("chk-live", "running")
    route = respx.get(f"{API_BASE}/v1/plagiarism/checks/chk-live/progress").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=b"id: 7\nevent: chunking\ndata: {}\n\n",
        )
    )

    response = client.get(
        "/plagiarism/checks/chk-live/events", headers={"Last-Event-ID": "6"}
    )
    body = response.get_data(as_text=True)

    assert route.calls.last.request.headers["Last-Event-ID"] == "6"
    assert "event: chunking" in body
    assert response.headers["Content-Type"].startswith("text/event-stream")
    assert response.headers["X-Accel-Buffering"] == "no"


@respx.mock
def test_events_proxy_omits_the_header_on_a_first_connection(client):
    stub_check("chk-live", "running")
    route = respx.get(f"{API_BASE}/v1/plagiarism/checks/chk-live/progress").mock(
        return_value=httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, content=b"event: queued\ndata: {}\n\n"
        )
    )
    client.get("/plagiarism/checks/chk-live/events").get_data()
    assert "Last-Event-ID" not in route.calls.last.request.headers


@respx.mock
def test_events_proxy_does_not_turn_an_upstream_error_into_a_200_stream(client):
    respx.get(f"{API_BASE}/v1/plagiarism/checks/chk-nope/progress").mock(
        return_value=httpx.Response(
            404,
            json={
                "error": {
                    "code": "plagiarism_check_not_found",
                    "message": "no such check",
                    "detail": {},
                }
            },
        )
    )
    response = client.get("/plagiarism/checks/chk-nope/events")
    assert response.status_code == 404
    assert not response.headers["Content-Type"].startswith("text/event-stream")


@respx.mock
def test_progress_script_ships_only_with_javascript_enabled(client, config):
    stub_check("chk-live", "running")
    body = html(client.get("/plagiarism/checks/chk-live"))
    assert ("check.js" in body) is not config.nojs
    # The refresh fallback is the inverse: present exactly when scripts are not.
    assert ('http-equiv="refresh"' in body) is config.nojs
```

- [x] **Step 2: 跑测试确认失败**

```bash
cd knowledge-web && python -m pytest tests/test_plagiarism.py -v -k "events or progress_script"
```
预期：失败，`events` 仍返回 204

- [x] **Step 3: 实现 SSE 代理**

替换 `kbweb/views/plagiarism.py` 里的 `events` 占位：

```python
import time
from contextlib import ExitStack

from flask import Response, stream_with_context

# Well under kbsvc's plag_sse_max_seconds (900): one stream pins one waitress
# thread for its whole life, so the cap must come from this side. EventSource
# reconnects on its own and carries Last-Event-ID, which kbsvc resumes from
# exactly - so cutting the stream costs the reader nothing.
PROXY_MAX_SECONDS = 120


@bp.get("/checks/<check_id>/events")
def events(check_id: str):
    api = client()
    last_event_id = request.headers.get("Last-Event-ID", "")

    # Enter upstream before committing the downstream 200 headers. Otherwise a
    # backend 404/503 becomes a fake successful SSE response whose body happens
    # to contain JSON or a late generator exception.
    stack = ExitStack()
    try:
        upstream = stack.enter_context(
            api.stream_progress(check_id, last_event_id=last_event_id)
        )
    except Exception:
        stack.close()
        raise

    @stream_with_context
    def relay():
        # stream_with_context matters: without it the app context pops when
        # this view returns and teardown closes the client mid-stream.
        try:
            deadline = time.monotonic() + PROXY_MAX_SECONDS
            for line in upstream.iter_lines():
                # iter_lines drops the newline; SSE needs it back, and blank
                # lines are what delimit frames.
                yield f"{line}\n"
                if time.monotonic() >= deadline:
                    return
        finally:
            stack.close()

    response = Response(
        relay(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    # Covers clients/middleware that close the response without iterating the
    # generator, in addition to relay()'s finally block.
    response.call_on_close(stack.close)
    return response
```

- [x] **Step 4: 写 `check.js`**

新建 `kbweb/static/js/check.js`：

```javascript
// Progress over SSE, plus opening the matching passage card when a highlight
// is clicked. Both are enhancements: the page already renders its state
// server-side and the cards are <details>, which open without any script.
(function () {
  var panel = document.querySelector('[data-check-progress]');

  if (panel) {
    var STAGES = {
      queued: '排队中',
      started: '开始检测',
      chunking: '切分文本',
      retrieving: '检索候选',
      aligning: '对齐比对',
      persisting: '写入结果'
    };
    var stageEl = panel.querySelector('[data-progress-stage]');
    var barEl = panel.querySelector('[data-progress-bar]');
    var source = new EventSource(panel.dataset.eventsUrl);
    var fallbackTimer = null;

    // SSE is an enhancement, not the only way out of the progress page. A
    // healthy stream emits keepalives every ~2s; if neither data nor keepalive
    // arrives for 15s, reload and let the server render the current state.
    function armFallback() {
      if (fallbackTimer) window.clearTimeout(fallbackTimer);
      fallbackTimer = window.setTimeout(function () {
        source.close();
        window.location.reload();
      }, 15000);
    }

    // Start the deadline immediately as well: a connection can hang before
    // firing either open or error.
    armFallback();
    source.onopen = armFallback;
    source.onerror = function () {
      // Keep native EventSource reconnection (and Last-Event-ID) alive, but do
      // not let repeated errors postpone the fallback forever.
      if (!fallbackTimer) armFallback();
    };
    source.addEventListener('keepalive', armFallback);

    Object.keys(STAGES).forEach(function (stage) {
      source.addEventListener(stage, function (event) {
        armFallback();
        stageEl.textContent = STAGES[stage];
        try {
          var payload = JSON.parse(event.data);
          if (typeof payload.progress === 'number') {
            barEl.style.width = Math.round(payload.progress * 100) + '%';
          }
        } catch (e) { /* a malformed frame must not stop the stream */ }
      });
    });

    ['completed', 'completed_partial', 'failed', 'cancelled'].forEach(function (stage) {
      source.addEventListener(stage, function () {
        if (fallbackTimer) window.clearTimeout(fallbackTimer);
        source.close();
        // Let the server render the report; there is one renderer, not two.
        window.location.reload();
      });
    });
  }

  // Clicking a marker in the submission opens its card. The href already
  // jumps to the source, so this only adds the expansion.
  document.querySelectorAll('.hit__marker').forEach(function (marker) {
    marker.addEventListener('click', function () {
      var target = document.querySelector(marker.getAttribute('href'));
      if (!target) return;
      target.querySelectorAll('details').forEach(function (card) { card.open = true; });
    });
  });
})();
```

- [x] **Step 5: 挂上脚本**

在 `kbweb/templates/check.html` 末尾加：

```html
{% block scripts %}
<script src="{{ url_for('static', filename='js/check.js') }}" defer></script>
{% endblock %}
```

`base.html:66-69` 已经用 `{% if not nojs %}` 把整个 `scripts` block 包住，所以无脚本模式下这段自动不输出。

- [x] **Step 6: meta refresh 只在无脚本时输出**

`check.html` 里的条件已是 `{% if live and nojs %}`，与 Step 1 的测试一致。确认无误即可。

- [x] **Step 7: 调 waitress 线程数**

`knowledge-web/deploy/Dockerfile:29` 改为：

```dockerfile
# Each open SSE stream holds a thread for as long as it lives, so the default
# of 4 would let a couple of progress pages starve the whole site.
CMD ["waitress-serve", "--host", "0.0.0.0", "--port", "5055", "--threads=16", "wsgi:app"]
```

- [x] **Step 8: 跑测试确认通过**

视图/客户端层再覆盖上游 503、错误 Content-Type、网络断开，以及下游响应未迭代便关闭时上游上下文仍被释放。除这些测试外，e2e 必须覆盖两种 JS 路径：正常 SSE 终态触发 reload；SSE 持续失败时 15 秒兜底 reload 后仍能进入报告。这样“渐进增强”才是被测试的行为，而不是注释里的愿望。

```bash
cd knowledge-web && python -m pytest tests/ -v
```
预期：全部 PASS

- [x] **Step 9: 提交**

```bash
git add knowledge-web/kbweb knowledge-web/tests knowledge-web/deploy/Dockerfile
git commit -m "feat(web): SSE 进度代理与阶段进度条

三件事必须一起做，少一件账就不平：单条流 120 秒主动关闭（后端上限
是 900 秒，而一条流占住一个 waitress 线程）；Last-Event-ID 透传（否则
每次重连都重放整条流，后端的精确续传白做）；waitress 线程数从默认 4
提到 16。

终态收尾用 reload 让服务端渲染报告，报告只有一处渲染逻辑。"
```

---

### Task 9: e2e 全流程

**Files:**
- Modify: `tests/e2e/stub_backend.py`
- Create: `tests/e2e/test_plagiarism_flow.py`

**Interfaces:**
- Consumes: Task 0–8 的全部产物

- [x] **Step 1: 读现有 stub 的写法**

```bash
cd knowledge-web && sed -n '1,220p' tests/e2e/stub_backend.py && grep -n "class FakeKbClient\|def " tests/e2e/stub_backend.py
```

按它已有的 `FakeKbClient` 类方法风格扩展，不要另起 HTTP stub 或路由层。

- [x] **Step 2: 扩展实际注入的 `FakeKbClient`**

`tests/e2e/conftest.py` 已核实使用 `patch("kbweb.KbClient", FakeKbClient)`，没有启动 HTTP 假后端。因此这里增加类方法和一个假的流式响应对象，不得添加 `@app` 路由。

```python
# --- plagiarism ---------------------------------------------------------

_CHECK_STATE = {"done": False}

PLAG_REPORT = {
    "check_id": "chk-e2e",
    "status": "completed",
    "snapshot_at": "2026-08-08T10:00:00",
    "algorithm_config_hash": "cfg-e2e",
    "query_chars": 19,
    "matched_chars": 11,
    "checked_chunks": 4,
    "total_chunks": 4,
    "coverage_reason": None,
    "is_complete": True,
    "query_text": "夫天地者，万物之逆旅也。古人秉烛夜游。",
    "sources": [
        {
            "document_id": "doc-1111-2222",
            "version_id": "ver-1",
            "version": 1,
            "content_hash": "abc",
            "title": "春夜宴从弟桃花园序",
            "matched_chars": 11,
            "score": 0.94,
            "passages": [
                {
                    "query_start": 0,
                    "query_end": 11,
                    "source_start": 0,
                    "source_end": 11,
                    "score": 0.94,
                    "preview": "夫天地者，万物之逆旅也",
                }
            ],
        }
    ],
    "unique_passages": [[0, 11]],
}


class _FakeSseResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def iter_lines(self):
        progress = 'id: 1\nevent: retrieving\ndata: {"progress": 0.6}\n\n'
        terminal = 'id: 2\nevent: completed\ndata: {"progress": 1.0}\n\n'
        yield from progress.splitlines()
        _CHECK_STATE["done"] = True
        yield from terminal.splitlines()


# Add these methods inside FakeKbClient.
def corpus_status(self) -> dict:
    return {
        "total_documents": 1,
        "ready_documents": 1,
        "pending_documents": 0,
        "failed_documents": 0,
        "algorithm_config_hash": "cfg-e2e",
        "is_ready": True,
    }


def create_text_check(self, **_kwargs) -> dict:
    _CHECK_STATE["done"] = False
    return {"check_id": "chk-e2e", "status": "pending"}


def create_document_check(self, _document_id: str, **_kwargs) -> dict:
    return self.create_text_check()


def list_checks(self, **_kwargs) -> list[dict]:
    return []


def get_check(self, _check_id: str) -> dict:
    status = "completed" if _CHECK_STATE["done"] else "running"
    return {
        "check_id": "chk-e2e",
        "status": status,
        "created_at": "2026-08-08T10:00:00",
        "snapshot_at": "2026-08-08T10:00:00",
        "algorithm_config_hash": "cfg-e2e",
        "source_document_id": None,
        "query_chars": PLAG_REPORT["query_chars"],
        "matched_chars": PLAG_REPORT["matched_chars"] if _CHECK_STATE["done"] else 0,
    }


def get_plag_report(self, _check_id: str) -> dict:
    return PLAG_REPORT


def delete_check(self, _check_id: str) -> int:
    return 204


def stream_progress(self, _check_id: str, **_kwargs):
    return _FakeSseResponse()
```

同时把既有 `FakeKbClient.get_chunks` 签名扩展为接受 `version: int | None = None`，与生产 client 保持一致。假的 SSE 必须至少发一个进度阶段和一个终态；终态发出后把模块级状态置为 completed，使 reload 后服务端真正渲染报告。

- [x] **Step 3: 写 e2e 测试**

新建 `tests/e2e/test_plagiarism_flow.py`：

```python
"""Submit → watch → read the report, in a real browser."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_submit_then_read_the_report(page, live_server):
    page.goto(f"{live_server}/plagiarism/")

    page.fill("textarea[name=text]", "夫天地者，万物之逆旅也。古人秉烛夜游。")
    page.click("button[type=submit]")

    # The URL becomes the check's own page and stays there through both states.
    page.wait_for_url("**/plagiarism/checks/chk-e2e")
    assert "检测" in page.content()

    # The JavaScript path receives a real fake-SSE terminal event, then reloads
    # the same URL. This is not get_check polling.
    page.wait_for_selector(".verdict__figure", timeout=15000)
    assert "57.9%" in page.inner_text(".verdict__figure")
    assert "春夜宴从弟桃花园序" in page.content()

    # The submission is highlighted from backend offsets.
    assert page.locator("mark.hit").count() >= 1

    # And the card opens.
    page.click(".passage__summary")
    assert "夫天地者，万物之逆旅也" in page.inner_text(".passage__body")
```

再加响应式布局 e2e：桌面宽度下断言 `.report-layout` 的 computed `grid-template-columns` 含两列、`.report-layout__sources` 为 `position: sticky`；移动宽度下断言单列且 aside 为 `static`。两个宽度都断言 `document.documentElement.scrollWidth == document.documentElement.clientWidth`，防止全文或来源卡造成横向溢出。

另设一个 Fake SSE 失败场景：`stream_progress()` 连续返回失败，但下一次详情页读取允许 `get_check()` 返回 completed；浏览器测试必须证明 fallback reload 最终进入报告。测试可在导航前用 `page.add_init_script()` 包装 `window.setTimeout`，把 15000ms 的回退计时压到约 100ms；生产代码仍保持 15000ms，避免 e2e 固定等待 15 秒。

`live_server` 与 `page` fixture 名已在 `tests/e2e/conftest.py` 核实，不再作为执行期占位符。
`57.9%` 是 `11 / 19` 的结果；stub 的 `query_chars` 必须与 `len(query_text)` 一致，不能为了凑展示数字破坏后端不变量。

- [x] **Step 4: 跑 e2e**

```bash
cd knowledge-web && python -m pytest tests/e2e/test_plagiarism_flow.py -m e2e -v
```
预期：PASS。（e2e 默认被 `addopts = "-m 'not e2e'"` 排除，必须显式 `-m e2e`。）

- [x] **Step 5: 跑全量**

```bash
cd knowledge-web && python -m pytest tests/ -v && python -m pytest tests/ -m e2e -v && python -m ruff check kbweb tests
```
预期：全绿

- [x] **Step 6: 提交**

```bash
git add knowledge-web/tests
git commit -m "test(web): 查重全流程 e2e

FakeKbClient 发出真实的假 SSE 进度与终态事件，终态前更新跨请求共享
状态，使浏览器 reload 后得到 completed 报告；测试不再假装 JS 路径
会轮询 get_check。"
```

---

## Self-Review

**规格覆盖检查：**

| 规格条目 | 落在哪个 Task |
|---|---|
| 六条路由 | Task 3（前三条）、Task 4（详情、删除）、Task 8（events） |
| 详情页两态共用 URL | Task 4 Step 4 |
| 提交页与历史合一 | Task 3 |
| `coverage_segments` 不改 `highlight_segments` | Task 1 |
| 顶部判定条与 `≥` 下界 | Task 5 Step 8 |
| 主体两栏与角标 | Task 6 |
| `<details>` 对照卡 | Task 7 Step 5 |
| 来源正文上限 8 | Task 7 Step 3 + 测试 |
| 冻结版本 + chunks 分页上限 200 | Task 0、Task 2、Task 7 + 契约测试 |
| 三条运行路径 | Task 4（nojs）、Task 8（SSE + 15 秒失败回退） |
| SSE 截流、续传、资源关闭与线程数 | Task 8 Step 3、Step 7 + 404/503/断流/close 测试 |
| 幂等 token | Task 3 Step 4 + 测试 |
| 语料就绪门禁 | Task 3 |
| 取消/删除 204 与 202 分流 | Task 2（返回状态码）、Task 4 |
| 真实错误码分流 | Task 3/4/5：`feature_disabled`、各 `plagiarism_*`、`idempotency_conflict`、`report_visibility_changed` |
| ACL 撤权 fail-closed 与请求后竞态降级 | Task 0、Task 5、Task 7 |
| 四层测试 | Task 1/5/6/7（纯函数）、Task 3/4/5/6/7/8（视图）、Task 3 Step 9（nojs）、Task 9（e2e） |
| Dockerfile 线程数 | Task 8 Step 7 |
| 文本/文档模式 `query_text` 与真实来源版本 | Task 0 门禁，Task 6/7 消费 |
| 实际 `FakeKbClient` e2e 与假 SSE | Task 9 |
| 桌面两栏 sticky / 移动单栏 / 无横向溢出 | Task 6 + Task 9 浏览器断言 |

实施者仍须以每个 Task 的门禁测试为完成条件；本表是索引，不替代测试。

**类型一致性：** `numbered_sources` 产出的 `ordinal` 被 `query_spans`（Task 6）与 `attach_excerpts`（Task 7）共同消费；`coverage_segments` 的 `frozenset[int]` 在模板里经 `| sort` 输出；`delete_check` 全程返回 `int`；`source.version` 是真实正整数并原样传给 chunks API。

**已核实的既有名字：** 文档详情 `library.document`、阅读页 `library.read`；`BackendError` 具有 `status/code/message/detail`；e2e fixture 为 `live_server` 与 `page`；e2e 后端替身是注入式 `FakeKbClient`。

---

## 实施顺序与并行

```text
Task 0 ─────────────────────┬── Task 6 ──┐
                           └── Task 7 ──┤
Task 1 ──┬── Task 3 ── Task 4 ── Task 5 ─┼── Task 9
Task 2 ──┘                  └── Task 8 ──┘
```

- Task 6 依赖 Task 0 的两种模式 `query_text` 门禁与 Task 5 的报告骨架。
- Task 7 依赖 Task 0 的真实来源版本、Task 2 的 version-aware `get_chunks` 与 Task 5 的来源排序。
- Task 9 必须在 Task 6、7、8 都完成后运行，才能同时验证全文高亮、版本摘录和 SSE 收尾。
- 若 Task 0 暂时阻塞，可以先交付 Task 1–5、8 的中间里程碑，但不能把缺少全文高亮/冻结版本摘录的 Task 9 标为完成。
