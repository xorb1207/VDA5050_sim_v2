"""editor_html.py 의 템플릿 주입 동작 회귀 가드.

PR #?? 에서 인라인 raw string `_TEMPLATE` 을 외부 `editor_template.html` 로 분리했음.
이 테스트는 (a) 템플릿 파일 존재 + sentinel 보존, (b) PAYLOAD 주입 후 sentinel 부재,
(c) caller 별 시그니처 (server_map_id 유무) 모두 정상 동작 — 셋을 보장.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

from src.domain.map.external_importer import import_map
from src.interfaces.map_editor import build_editor_html
from src.interfaces.map_editor.editor_html import (
    _PAYLOAD_SENTINEL,
    _TEMPLATE_PATH,
    _load_template,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SYNTHETIC_PLANT = REPO_ROOT / "maps" / "synthetic_plant.json"


@pytest.fixture(scope="module")
def imported():
    assert SYNTHETIC_PLANT.exists(), f"fixture map missing: {SYNTHETIC_PLANT}"
    return import_map(SYNTHETIC_PLANT)


def test_template_file_exists():
    """editor_template.html 이 모듈 옆에 존재 — 패키지 자원."""
    assert _TEMPLATE_PATH.exists(), f"missing: {_TEMPLATE_PATH}"
    assert _TEMPLATE_PATH.name == "editor_template.html"


def test_template_contains_sentinel():
    """템플릿 자체엔 sentinel 이 정확히 한 번 들어가 있어야 — 주입 지점."""
    tpl = _load_template()
    count = tpl.count(_PAYLOAD_SENTINEL)
    assert count == 1, f"expected exactly 1 sentinel, got {count}"
    # 디자이너가 템플릿을 그대로 브라우저로 열어도 JS syntax 유효해야 함 (빈 dict).
    # const PAYLOAD = /*__PAYLOAD_JSON__*/{};  형태.
    assert "const PAYLOAD = /*__PAYLOAD_JSON__*/{};" in tpl


def test_build_editor_html_replaces_sentinel(imported):
    """주입 후 sentinel 잔존 없음 — 운영 흐름."""
    html = build_editor_html(imported, source_name="synthetic_plant",
                             server_map_id="srv-42")
    assert _PAYLOAD_SENTINEL not in html, "sentinel must be replaced"


def test_build_editor_html_injects_payload_fields(imported):
    """PAYLOAD JSON 안에 nodes / edges / server_map_id / source_name 모두 들어감."""
    html = build_editor_html(imported, source_name="synthetic_plant",
                             server_map_id="srv-42")
    # const PAYLOAD = {...}; 라인 캡처
    m = re.search(r"const PAYLOAD = (\{.*?\});", html)
    assert m, "PAYLOAD = {...}; 형태를 찾지 못함"
    payload = json.loads(m.group(1))
    assert payload["source_name"] == "synthetic_plant"
    assert payload["server_map_id"] == "srv-42"
    assert isinstance(payload["nodes"], list) and len(payload["nodes"]) > 0
    assert isinstance(payload["edges"], list) and len(payload["edges"]) > 0
    # 각 노드/엣지가 기대 필드 보유 (역회귀)
    n = payload["nodes"][0]
    assert {"id", "x", "y", "name", "role", "is_charger", "is_holding"}.issubset(n)
    e = payload["edges"][0]
    assert {"id", "src", "dst", "bidir", "v_max", "graph_idx"}.issubset(e)


def test_build_editor_html_without_server_map_id(imported):
    """import_map_demo.py --edit 흐름 — server_map_id 없으면 null 직렬화."""
    html = build_editor_html(imported, source_name="demo")
    m = re.search(r"const PAYLOAD = (\{.*?\});", html)
    payload = json.loads(m.group(1))
    assert payload["server_map_id"] is None


def test_build_editor_html_node_edge_counts_match_input(imported):
    """직렬화된 nodes/edges 카운트 = ImportedMap 카운트 (드롭 없음)."""
    html = build_editor_html(imported, source_name="synthetic_plant")
    m = re.search(r"const PAYLOAD = (\{.*?\});", html)
    payload = json.loads(m.group(1))
    assert len(payload["nodes"]) == len(imported.nodes)
    assert len(payload["edges"]) == len(imported.edges)
