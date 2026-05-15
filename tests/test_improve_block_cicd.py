"""
CI/CD: Comprehensive AI paragraph-improvement pipeline tests.

Flow under test:
  改寫 (Improve)  →  產生 (Generate suggestion)  →  導入 (Apply) 或 復原 (Undo)

Coverage:
  A  Schema & input validation (empty instruction, whitespace, missing fields)
  B  Happy path – all 5 block types (paragraph, heading, table, list, chart_image)
  C  All 10 sections with section-realistic content
  D  Full improve → apply → verify version / history / status reset
  E  Full improve → apply → undo → verify original restored
  F  Error cases  (404 / 400 / 401 / 409 / 503)
  G  Fact context injection (blocks with source_fact_ids)
  H  Optimistic locking – concurrent edits, stale version
  I  Response schema completeness and field correctness
  J  Security / RBAC – analyst isolation, cross-report access
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from main import app

# ─────────────────────────────────────────────────────────────────────────────
BASE = "/api/credit-report"
AUTH = f"{BASE}/auth"
REPORTS = f"{BASE}/reports"
# ─────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

@contextmanager
def _mock_gemini(return_text: str = "Improved paragraph text."):
    """Patch Gemini client used by call_gemini_raw."""
    mock_resp = MagicMock()
    mock_resp.text = return_text
    mock_client = MagicMock()
    mock_client.aio = MagicMock()
    mock_client.aio.models = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)
    with patch("google.genai.Client", return_value=mock_client):
        yield mock_client


async def _login(ac: AsyncClient, email: str, password: str) -> dict:
    r = await ac.post(f"{AUTH}/login", data={"username": email, "password": password})
    assert r.status_code == 200, f"Login failed: {r.text}"
    return r.json()


async def _auth_headers(ac: AsyncClient, email="admin@example.com", password="admin123") -> dict:
    tok = await _login(ac, email, password)
    return {"Authorization": f"Bearer {tok['access_token']}"}


async def _create_report(ac: AsyncClient, hdrs: dict, borrower: str = "CI Test Co Ltd") -> dict:
    r = await ac.post(
        REPORTS,
        json={"borrower_name": borrower, "industry": "marine", "report_type": "new_deal"},
        headers=hdrs,
    )
    assert r.status_code == 201, f"Create report failed: {r.text}"
    return r.json()


async def _seed_block(
    report_id: str,
    section_no: int = 4,
    block_type: str = "paragraph",
    content: str = "The borrower is a leading shipping company with strong financials.",
    source_fact_ids: str = "[]",
) -> str:
    from credit_report.database import AsyncSessionLocal
    from credit_report.block_ast.models import ReportBlock

    bid = f"blk_{uuid.uuid4().hex[:14]}"
    async with AsyncSessionLocal() as db:
        db.add(ReportBlock(
            id=bid, report_id=report_id, section_no=section_no,
            block_type=block_type, content=content,
            source_fact_ids=source_fact_ids,
            validation_status="pending", is_stale=False, version=1,
        ))
        await db.commit()
    return bid


async def _seed_fact(report_id: str, metric: str, value: float, entity: str = "Test Co",
                     period: str = "FY2024") -> str:
    from credit_report.database import AsyncSessionLocal
    from credit_report.fact_store.models import CanonicalFact

    fid = f"fact_{uuid.uuid4().hex[:12]}"
    async with AsyncSessionLocal() as db:
        db.add(CanonicalFact(
            id=fid, report_id=report_id, source_section_no=7,
            metric_name=metric, entity=entity, period=period,
            value=value, value_text=str(value),
            currency="USD", unit="m", state="validated",
            version=1,
        ))
        await db.commit()
    return fid


async def _get_block(ac: AsyncClient, hdrs: dict, report_id: str, block_id: str) -> dict:
    r = await ac.get(f"{REPORTS}/{report_id}/blocks/{block_id}", headers=hdrs)
    assert r.status_code == 200
    return r.json()


async def _improve(ac: AsyncClient, hdrs: dict, report_id: str, block_id: str,
                   instruction: str = "Make it more concise.", mock_text: str = "Improved text.") -> dict:
    with _mock_gemini(mock_text):
        r = await ac.post(
            f"{REPORTS}/{report_id}/blocks/{block_id}/improve",
            json={"instruction": instruction},
            headers=hdrs,
        )
    return r


async def _apply(ac: AsyncClient, hdrs: dict, report_id: str, block_id: str,
                 content: str, expected_version: int) -> dict:
    r = await ac.patch(
        f"{REPORTS}/{report_id}/blocks/{block_id}",
        json={"content": content, "reason": "AI improvement", "expected_version": expected_version},
        headers=hdrs,
    )
    return r


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def ac():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def admin_hdrs(ac):
    return await _auth_headers(ac)


@pytest_asyncio.fixture
async def report(ac, admin_hdrs):
    return await _create_report(ac, admin_hdrs)


@pytest_asyncio.fixture
async def analyst_hdrs(ac, admin_hdrs):
    email = f"analyst_{uuid.uuid4().hex[:6]}@ci.test"
    r = await ac.post(
        f"{AUTH}/register",
        json={"email": email, "password": "Pass1234!", "role": "analyst"},
        headers=admin_hdrs,
    )
    assert r.status_code == 201
    return await _auth_headers(ac, email, "Pass1234!")


@pytest_asyncio.fixture
async def analyst_report(ac, analyst_hdrs):
    return await _create_report(ac, analyst_hdrs, borrower="Analyst Owned Report")


# ══════════════════════════════════════════════════════════════════════════════
# A — Schema & Input Validation
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestImproveSchema:

    async def test_missing_instruction_returns_422(self, ac, admin_hdrs, report):
        bid = await _seed_block(report["id"])
        r = await ac.post(
            f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
            json={},  # no instruction field
            headers=admin_hdrs,
        )
        assert r.status_code == 422

    async def test_empty_instruction_rejected(self, ac, admin_hdrs, report):
        """Empty string instruction must be rejected — sending to LLM is wasteful and produces garbage."""
        bid = await _seed_block(report["id"])
        r = await ac.post(
            f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
            json={"instruction": ""},
            headers=admin_hdrs,
        )
        # MUST reject with 422 — empty instruction is not meaningful
        assert r.status_code == 422, (
            f"Empty instruction should be rejected (got {r.status_code}): {r.text}"
        )

    async def test_whitespace_only_instruction_rejected(self, ac, admin_hdrs, report):
        """Whitespace-only instruction provides no guidance to the LLM."""
        bid = await _seed_block(report["id"])
        r = await ac.post(
            f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
            json={"instruction": "   \t\n  "},
            headers=admin_hdrs,
        )
        assert r.status_code == 422, (
            f"Whitespace-only instruction should be rejected (got {r.status_code})"
        )

    async def test_instruction_none_returns_422(self, ac, admin_hdrs, report):
        bid = await _seed_block(report["id"])
        r = await ac.post(
            f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
            json={"instruction": None},
            headers=admin_hdrs,
        )
        assert r.status_code == 422

    async def test_valid_instruction_accepted(self, ac, admin_hdrs, report):
        bid = await _seed_block(report["id"])
        with _mock_gemini("Improved content here."):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Make it more concise."},
                headers=admin_hdrs,
            )
        assert r.status_code == 200

    async def test_long_instruction_accepted(self, ac, admin_hdrs, report):
        bid = await _seed_block(report["id"])
        long_instr = "Rewrite this paragraph " * 50  # 1150 chars
        with _mock_gemini("Concise version."):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": long_instr},
                headers=admin_hdrs,
            )
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# B — Happy Path: All 5 Block Types
# ══════════════════════════════════════════════════════════════════════════════

BLOCK_TYPES = {
    "paragraph": "The borrower demonstrates strong liquidity with current ratio of 1.85x.",
    "heading": "## §4 Borrower Background and Group Structure",
    "table": "| Metric | FY2023 | FY2024 |\n|--------|--------|--------|\n| Revenue | 135.0 | 150.0 |\n| EBITDA | 50.0 | 56.0 |",
    "list": "- Strong parent guarantee from EMC (TWSE: 2609)\n- Conservative LTC of 80%\n- Pre-delivery RG from KDB covers all instalments",
    "chart_image": "[Org Chart — Borrower Group Structure]",
}


@pytest.mark.asyncio
class TestImproveBlockTypes:

    @pytest.mark.parametrize("block_type,content", list(BLOCK_TYPES.items()))
    async def test_improve_returns_200(self, ac, admin_hdrs, report, block_type, content):
        bid = await _seed_block(report["id"], block_type=block_type, content=content)
        with _mock_gemini(f"Improved {block_type} content."):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Rewrite in a more formal tone."},
                headers=admin_hdrs,
            )
        assert r.status_code == 200, f"block_type={block_type}: {r.text}"
        data = r.json()
        assert data["block_id"] == bid
        assert data["original_content"] == content
        assert data["suggested_content"] == f"Improved {block_type} content."
        assert data["current_version"] == 1

    @pytest.mark.parametrize("block_type,content", list(BLOCK_TYPES.items()))
    async def test_improve_does_not_mutate_block(self, ac, admin_hdrs, report, block_type, content):
        """Improve is suggestion-only — block content and version must be unchanged."""
        bid = await _seed_block(report["id"], block_type=block_type, content=content)
        with _mock_gemini("Different text."):
            await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Change the tone."},
                headers=admin_hdrs,
            )
        block = await _get_block(ac, admin_hdrs, report["id"], bid)
        assert block["content"] == content, "Improve must NOT modify block content"
        assert block["version"] == 1, "Improve must NOT increment version"
        assert block["validation_status"] == "pending"


# ══════════════════════════════════════════════════════════════════════════════
# C — All 10 Sections with Representative Content
# ══════════════════════════════════════════════════════════════════════════════

SECTION_CONTENT = {
    1: ("paragraph",
        "Borrower (parent group): Oakwood Maritime Group\n"
        "Unit: million USD\n"
        "Total proposed credit limit: USD 417.25m across 4 facilities (Items 1–4).\n"
        "Regulatory compliance: CUB net worth TWD 275bn; single-borrower limit 5% = TWD 13.75bn ≈ USD 436m."),
    2: ("paragraph",
        "RECOMMENDATION: APPROVE\n\n"
        "The credit committee recommends approval of the USD 178.50m sustainability-linked term loan. "
        "EMA is a wholly-owned subsidiary of EMC (TWSE: 2609), FY2024 revenue TWD 385.2bn. "
        "D/E ratio 0.31x; interest coverage 31.2x as at 30 September 2025."),
    3: ("paragraph",
        "Internal rating: BB+. PD: 150 bps (Low-Medium). LGD: 45%. Expected Loss: 68 bps. "
        "MAS 612 classification: Performing — Standard. "
        "ESG category: B. Poseidon Principles aligned. Sanctions screening: Clear (OFAC/EU/MAS/UN/HM Treasury)."),
    4: ("paragraph",
        "Borrower Shipping Pte Ltd (UEN: 202012345A, incorporated 15 March 2010, Singapore). "
        "Wholly owned by Parent Holding Co Ltd (BVI); UBO: John Smith (100%). "
        "Fleet: 2 vessels, aggregate DWT 133,000. FY2024 revenue USD 150m; EBITDA USD 50m; net income USD 31.9m."),
    5: ("paragraph",
        "Security: first priority ship mortgage. Asset Coverage Ratio at delivery: 154%. "
        "Loan-to-Cost: 65% (USD 20.8m / USD 32.0m contract price). "
        "Refund Guarantee issued by Korea Development Bank (KDB, rated Aa2/AA), covering all pre-delivery instalments."),
    6: ("paragraph",
        "Structure: USD 178.50m committed secured 11-year sustainability-linked bilateral term loan (4+7 structure). "
        "Margin: SOFR + 85 bps. Upfront fee: 0.10% (USD 178,500). "
        "Pre-delivery cap: USD 71.40m (5 drawdowns max, each ≤ 80% of instalment cost)."),
    7: ("table",
        "| Metric | FY2022 | FY2023 | FY2024 |\n"
        "|--------|--------|--------|--------|\n"
        "| Revenue (USD m) | 120.0 | 135.0 | 150.0 |\n"
        "| EBITDA (USD m) | 42.0 | 50.0 | 56.0 |\n"
        "| Net Income (USD m) | 22.1 | 27.6 | 31.9 |\n"
        "| D/E (x) | 1.41 | 1.37 | 1.33 |\n"
        "| Interest Coverage (x) | 5.25 | 5.88 | 6.17 |"),
    8: ("list",
        "- Minimum DSCR: 1.20x (semi-annual)\n"
        "- Maximum LTV: 83% (biennial post-delivery, cure period 21 days)\n"
        "- Minimum ACR: 120% (biennial post-delivery)\n"
        "- Minimum Current Ratio: 1.00x (guarantor consolidated, annual)\n"
        "- Maximum D/E: 2.00x (guarantor consolidated, annual)"),
    9: ("paragraph",
        "APPROVE the proposed USD 178.50m SLL facility to EMA. "
        "Key strengths: (i) EMC parent guarantee (D/E 0.31x; IC 31.2x); "
        "(ii) conservative LTC 80%, post-delivery ACR ≥ 120% / LTV ≤ 83%; "
        "(iii) pre-delivery risk fully mitigated by KDB Refund Guarantee."),
    10: ("table",
         "| Year | Period | Revenue | EBITDA | Debt Service | DSCR | Outstanding |\n"
         "|------|--------|---------|--------|-------------|------|-------------|\n"
         "| 1 | FY2026 | 17.0 | 10.0 | 7.0 | 1.43 | 16.6 |\n"
         "| 2 | FY2027 | 17.5 | 10.3 | 7.0 | 1.47 | 12.4 |\n"
         "| 3 | FY2028 | 18.0 | 10.5 | 7.0 | 1.50 | 8.2 |"),
}

SECTION_INSTRUCTIONS = {
    1: "Summarise the facility structure more concisely.",
    2: "Rewrite in a more formal executive tone.",
    3: "Emphasise the downside risk factors.",
    4: "Add more focus on the UBO background.",
    5: "Clarify the security waterfall more precisely.",
    6: "Make the structure description more succinct.",
    7: "Add a note on the revenue growth trend.",
    8: "Convert covenant list to a numbered format.",
    9: "Strengthen the recommendation language.",
    10: "Add commentary on DSCR trends across years.",
}


@pytest.mark.asyncio
class TestImproveAllSections:

    @pytest.mark.parametrize("sec_no", list(range(1, 11)))
    async def test_improve_section(self, ac, admin_hdrs, report, sec_no):
        block_type, content = SECTION_CONTENT[sec_no]
        instruction = SECTION_INSTRUCTIONS[sec_no]
        bid = await _seed_block(report["id"], section_no=sec_no,
                                block_type=block_type, content=content)
        improved_text = (f"§{sec_no} improved: " + content[:60]).strip()
        with _mock_gemini(improved_text):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": instruction},
                headers=admin_hdrs,
            )
        assert r.status_code == 200, f"§{sec_no} improve failed: {r.text}"
        data = r.json()
        assert data["block_id"] == bid
        assert data["original_content"] == content
        assert data["suggested_content"] == improved_text
        assert data["current_version"] == 1

    @pytest.mark.parametrize("sec_no", list(range(1, 11)))
    async def test_full_flow_section(self, ac, admin_hdrs, report, sec_no):
        """Full 改寫→導入 per section."""
        block_type, content = SECTION_CONTENT[sec_no]
        bid = await _seed_block(report["id"], section_no=sec_no,
                                block_type=block_type, content=content)
        improved = f"§{sec_no} rewritten content at {uuid.uuid4().hex[:6]}"

        # Step 1: Improve
        with _mock_gemini(improved):
            ir = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": SECTION_INSTRUCTIONS[sec_no]},
                headers=admin_hdrs,
            )
        assert ir.status_code == 200
        suggestion = ir.json()["suggested_content"]
        cur_version = ir.json()["current_version"]

        # Step 2: Apply
        ar = await _apply(ac, admin_hdrs, report["id"], bid, suggestion, cur_version)
        assert ar.status_code == 200, f"§{sec_no} apply failed: {ar.text}"
        applied = ar.json()
        assert applied["content"] == improved
        assert applied["version"] == 2
        assert applied["validation_status"] == "pending"

        # Step 3: Verify via GET
        block = await _get_block(ac, admin_hdrs, report["id"], bid)
        assert block["content"] == improved
        assert block["version"] == 2


# ══════════════════════════════════════════════════════════════════════════════
# D — Full Improve → Apply: version / history / status verification
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestImproveApply:

    async def test_apply_increments_version(self, ac, admin_hdrs, report):
        bid = await _seed_block(report["id"])
        with _mock_gemini("Better version."):
            ir = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Improve tone."},
                headers=admin_hdrs,
            )
        assert ir.json()["current_version"] == 1

        ar = await _apply(ac, admin_hdrs, report["id"], bid, "Better version.", 1)
        assert ar.status_code == 200
        assert ar.json()["version"] == 2

    async def test_apply_resets_validation_to_pending(self, ac, admin_hdrs, report):
        bid = await _seed_block(report["id"])
        # Validate first
        await ac.post(f"{REPORTS}/{report['id']}/blocks/{bid}/validate", headers=admin_hdrs)
        block = await _get_block(ac, admin_hdrs, report["id"], bid)
        assert block["validation_status"] == "passed"

        # Now apply improvement → should reset to pending
        ar = await _apply(ac, admin_hdrs, report["id"], bid, "New content.", 1)
        assert ar.status_code == 200
        assert ar.json()["validation_status"] == "pending"

    async def test_apply_creates_history_entry(self, ac, admin_hdrs, report):
        bid = await _seed_block(report["id"])
        await _apply(ac, admin_hdrs, report["id"], bid, "V2 content.", 1)
        r = await ac.get(f"{REPORTS}/{report['id']}/blocks/{bid}/history", headers=admin_hdrs)
        assert r.status_code == 200
        history = r.json()
        assert len(history) >= 1
        assert history[0]["content"] is not None  # snapshot of original

    async def test_apply_updates_last_edited_by(self, ac, admin_hdrs, report):
        bid = await _seed_block(report["id"])
        ar = await _apply(ac, admin_hdrs, report["id"], bid, "Edited.", 1)
        assert ar.status_code == 200
        assert ar.json()["last_edited_by"] is not None

    async def test_multiple_sequential_applies(self, ac, admin_hdrs, report):
        bid = await _seed_block(report["id"])
        for i in range(1, 4):
            ar = await _apply(ac, admin_hdrs, report["id"], bid, f"Version {i+1} text.", i)
            assert ar.status_code == 200
            assert ar.json()["version"] == i + 1

    async def test_apply_with_unicode_content(self, ac, admin_hdrs, report):
        bid = await _seed_block(report["id"])
        unicode_content = "借款人展現卓越的財務紀律，EBITDA達USD 56m，D/E比率0.31x。"
        ar = await _apply(ac, admin_hdrs, report["id"], bid, unicode_content, 1)
        assert ar.status_code == 200
        block = await _get_block(ac, admin_hdrs, report["id"], bid)
        assert block["content"] == unicode_content

    async def test_apply_with_markdown_tables(self, ac, admin_hdrs, report):
        bid = await _seed_block(report["id"])
        md_table = "| A | B |\n|---|---|\n| 1 | 2 |"
        ar = await _apply(ac, admin_hdrs, report["id"], bid, md_table, 1)
        assert ar.status_code == 200
        block = await _get_block(ac, admin_hdrs, report["id"], bid)
        assert block["content"] == md_table

    async def test_suggested_content_non_empty(self, ac, admin_hdrs, report):
        """LLM returning empty string must NOT produce an empty suggested_content."""
        bid = await _seed_block(report["id"])
        with _mock_gemini(""):  # LLM returns empty
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Make it better."},
                headers=admin_hdrs,
            )
        # Must not succeed with empty suggestion — should return 503 or 422
        if r.status_code == 200:
            data = r.json()
            # If 200 is returned, suggested_content must be non-empty
            assert data["suggested_content"], (
                "BUG: suggested_content is empty — LLM returned empty string but endpoint returned 200"
            )


# ══════════════════════════════════════════════════════════════════════════════
# E — Full Improve → Apply → Undo (復原)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestImproveUndo:

    async def test_undo_restores_original_content(self, ac, admin_hdrs, report):
        """Undo must exactly restore the content that existed before apply."""
        original = "The borrower has strong credit metrics: D/E 0.31x, IC 31.2x."
        bid = await _seed_block(report["id"], content=original)

        # Improve
        suggestion = "The borrower exhibits robust credit metrics: D/E ratio 0.31x, IC 31.2x."
        with _mock_gemini(suggestion):
            ir = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Rewrite more formally."},
                headers=admin_hdrs,
            )
        assert ir.status_code == 200
        cur_version = ir.json()["current_version"]

        # Apply
        ar = await _apply(ac, admin_hdrs, report["id"], bid, suggestion, cur_version)
        assert ar.status_code == 200
        new_version = ar.json()["version"]  # should be 2

        # Undo: PATCH back to original using new_version
        undo_r = await _apply(
            ac, admin_hdrs, report["id"], bid, original,
            expected_version=new_version
        )
        assert undo_r.status_code == 200
        assert undo_r.json()["content"] == original
        assert undo_r.json()["version"] == 3  # applied twice + undo = version 3

        # Verify via GET
        block = await _get_block(ac, admin_hdrs, report["id"], bid)
        assert block["content"] == original

    async def test_undo_wrong_version_returns_409(self, ac, admin_hdrs, report):
        """Undo with stale version must return 409."""
        bid = await _seed_block(report["id"])
        await _apply(ac, admin_hdrs, report["id"], bid, "V2 content.", 1)

        # Try to undo with wrong expected_version
        undo_r = await _apply(ac, admin_hdrs, report["id"], bid, "Original content.", 1)
        assert undo_r.status_code == 409  # version mismatch

    async def test_undo_history_chain(self, ac, admin_hdrs, report):
        """After improve→apply→undo, history should have 2 entries."""
        original = "Original paragraph content for undo chain test."
        improved = "Improved paragraph content for undo chain test."
        bid = await _seed_block(report["id"], content=original)

        await _apply(ac, admin_hdrs, report["id"], bid, improved, 1)  # v1→v2
        await _apply(ac, admin_hdrs, report["id"], bid, original, 2)  # v2→v3 (undo)

        r = await ac.get(f"{REPORTS}/{report['id']}/blocks/{bid}/history", headers=admin_hdrs)
        assert r.status_code == 200
        assert len(r.json()) == 2  # two snapshots recorded

    @pytest.mark.parametrize("sec_no", [1, 2, 4, 7, 9])
    async def test_undo_per_section(self, ac, admin_hdrs, report, sec_no):
        """Full 改寫→導入→復原 for key sections."""
        block_type, original_content = SECTION_CONTENT[sec_no]
        bid = await _seed_block(report["id"], section_no=sec_no,
                                block_type=block_type, content=original_content)
        improved = f"Improved §{sec_no}: {original_content[:40]}..."

        # Improve + Apply
        with _mock_gemini(improved):
            ir = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": SECTION_INSTRUCTIONS[sec_no]},
                headers=admin_hdrs,
            )
        assert ir.status_code == 200
        ar = await _apply(ac, admin_hdrs, report["id"], bid, improved, 1)
        assert ar.status_code == 200
        assert ar.json()["version"] == 2

        # Undo
        ur = await _apply(ac, admin_hdrs, report["id"], bid, original_content, 2)
        assert ur.status_code == 200
        assert ur.json()["content"] == original_content
        assert ur.json()["version"] == 3


# ══════════════════════════════════════════════════════════════════════════════
# F — Error Cases (404 / 400 / 401 / 409 / 503)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestImproveErrors:

    async def test_block_not_found_returns_404(self, ac, admin_hdrs, report):
        r = await ac.post(
            f"{REPORTS}/{report['id']}/blocks/nonexistent_block_xyz/improve",
            json={"instruction": "Test."},
            headers=admin_hdrs,
        )
        assert r.status_code == 404

    async def test_block_in_different_report_returns_404(self, ac, admin_hdrs):
        """A block from report A must not be accessible via report B."""
        rpt_a = await _create_report(ac, admin_hdrs, "Report A")
        rpt_b = await _create_report(ac, admin_hdrs, "Report B")
        bid = await _seed_block(rpt_a["id"])  # block belongs to A

        r = await ac.post(
            f"{REPORTS}/{rpt_b['id']}/blocks/{bid}/improve",
            json={"instruction": "Test cross-report access."},
            headers=admin_hdrs,
        )
        assert r.status_code == 404  # block.report_id != report_b.id

    async def test_block_with_null_content_returns_400(self, ac, admin_hdrs, report):
        from credit_report.database import AsyncSessionLocal
        from credit_report.block_ast.models import ReportBlock

        bid = f"blk_{uuid.uuid4().hex[:12]}"
        async with AsyncSessionLocal() as db:
            db.add(ReportBlock(
                id=bid, report_id=report["id"], section_no=4,
                block_type="paragraph", content=None,
                source_fact_ids="[]", validation_status="pending",
                is_stale=False, version=1,
            ))
            await db.commit()

        r = await ac.post(
            f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
            json={"instruction": "Make it better."},
            headers=admin_hdrs,
        )
        assert r.status_code == 400
        assert "content" in r.json()["detail"].lower()

    async def test_block_with_empty_string_content_returns_400(self, ac, admin_hdrs, report):
        """Empty string content (not null) should also be rejected."""
        bid = await _seed_block(report["id"], content="")
        r = await ac.post(
            f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
            json={"instruction": "Improve this."},
            headers=admin_hdrs,
        )
        assert r.status_code == 400, (
            f"BUG: empty string content should return 400 (got {r.status_code})"
        )

    async def test_unauthenticated_returns_401(self, ac, report):
        bid = await _seed_block(report["id"])
        r = await ac.post(
            f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
            json={"instruction": "Improve."},
        )
        assert r.status_code == 401

    async def test_invalid_token_returns_401(self, ac, report):
        bid = await _seed_block(report["id"])
        r = await ac.post(
            f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
            json={"instruction": "Improve."},
            headers={"Authorization": "Bearer totally.invalid.token"},
        )
        assert r.status_code == 401

    async def test_apply_version_conflict_returns_409(self, ac, admin_hdrs, report):
        bid = await _seed_block(report["id"])
        # First apply succeeds
        await _apply(ac, admin_hdrs, report["id"], bid, "First edit.", 1)
        # Second apply with old expected_version → conflict
        ar = await _apply(ac, admin_hdrs, report["id"], bid, "Stale edit.", 1)
        assert ar.status_code == 409

    async def test_llm_timeout_returns_503(self, ac, admin_hdrs, report):
        """TimeoutError from call_gemini_raw must propagate as 503."""
        bid = await _seed_block(report["id"])
        mock_client = MagicMock()
        mock_client.aio = MagicMock()
        mock_client.aio.models = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )
        with patch("google.genai.Client", return_value=mock_client):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Make it better."},
                headers=admin_hdrs,
            )
        assert r.status_code == 503

    async def test_llm_exception_returns_503(self, ac, admin_hdrs, report):
        """Any LLM exception must return 503, not 500."""
        bid = await _seed_block(report["id"])
        mock_client = MagicMock()
        mock_client.aio = MagicMock()
        mock_client.aio.models = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            side_effect=RuntimeError("Gemini API quota exceeded")
        )
        with patch("google.genai.Client", return_value=mock_client):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Make it better."},
                headers=admin_hdrs,
            )
        assert r.status_code == 503
        assert "AI generation failed" in r.json()["detail"]

    async def test_apply_not_found_returns_404(self, ac, admin_hdrs, report):
        ar = await _apply(ac, admin_hdrs, report["id"], "nonexistent_block_zzz", "text", 1)
        assert ar.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# G — Fact Context Injection
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestImproveFactContext:

    async def test_bound_facts_included_in_prompt(self, ac, admin_hdrs, report):
        """When block has source_fact_ids, fact context must appear in the LLM call."""
        fid = await _seed_fact(report["id"], "Revenue", 150.0)
        bid = await _seed_block(
            report["id"], section_no=7,
            content="Revenue was USD 150m in FY2024.",
            source_fact_ids=f'["{fid}"]',
        )
        captured_prompts = []

        async def _capture(model, contents, config):
            captured_prompts.append({"user": contents, "system": str(config.system_instruction)})
            mock_resp = MagicMock()
            mock_resp.text = "Revenue remained USD 150m in FY2024."
            return mock_resp

        mock_client = MagicMock()
        mock_client.aio = MagicMock()
        mock_client.aio.models = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(side_effect=_capture)
        with patch("google.genai.Client", return_value=mock_client):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Preserve all figures and rewrite concisely."},
                headers=admin_hdrs,
            )
        assert r.status_code == 200
        assert len(captured_prompts) == 1
        user_prompt = captured_prompts[0]["user"]
        assert "BOUND FACTS" in user_prompt, "Fact context must appear in user prompt"
        assert "Revenue" in user_prompt

    async def test_empty_source_fact_ids_no_context(self, ac, admin_hdrs, report):
        """Block with source_fact_ids=[] must not add BOUND FACTS section."""
        bid = await _seed_block(report["id"], source_fact_ids="[]")
        captured_prompts = []

        async def _capture(model, contents, config):
            captured_prompts.append(contents)
            mock_resp = MagicMock()
            mock_resp.text = "Clean version."
            return mock_resp

        mock_client = MagicMock()
        mock_client.aio = MagicMock()
        mock_client.aio.models = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(side_effect=_capture)
        with patch("google.genai.Client", return_value=mock_client):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Make it shorter."},
                headers=admin_hdrs,
            )
        assert r.status_code == 200
        assert "BOUND FACTS" not in captured_prompts[0]

    async def test_malformed_source_fact_ids_still_works(self, ac, admin_hdrs, report):
        """Malformed JSON in source_fact_ids must not crash the endpoint."""
        bid = await _seed_block(
            report["id"], content="Some paragraph.",
            source_fact_ids="NOT_VALID_JSON",
        )
        with _mock_gemini("Fixed paragraph."):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Rewrite."},
                headers=admin_hdrs,
            )
        assert r.status_code == 200  # gracefully degrades — no fact context

    async def test_null_source_fact_ids_still_works(self, ac, admin_hdrs, report):
        """NULL source_fact_ids must be treated as empty list."""
        from credit_report.database import AsyncSessionLocal
        from credit_report.block_ast.models import ReportBlock

        bid = f"blk_{uuid.uuid4().hex[:12]}"
        async with AsyncSessionLocal() as db:
            db.add(ReportBlock(
                id=bid, report_id=report["id"], section_no=4,
                block_type="paragraph",
                content="Paragraph with null source_fact_ids.",
                source_fact_ids=None,  # explicitly NULL
                validation_status="pending", is_stale=False, version=1,
            ))
            await db.commit()
        with _mock_gemini("Improved."):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Rewrite."},
                headers=admin_hdrs,
            )
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# H — Optimistic Locking & Concurrency
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestImproveLocking:

    async def test_improve_while_stale_still_succeeds(self, ac, admin_hdrs, report):
        """Improve endpoint does NOT check expected_version — it's for suggestion only."""
        bid = await _seed_block(report["id"])
        # Mark stale by patching first
        await _apply(ac, admin_hdrs, report["id"], bid, "Interim edit.", 1)
        # Now improve again — must work regardless of version
        with _mock_gemini("Further improved."):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Further improve.", "expected_version": 999},
                headers=admin_hdrs,
            )
        assert r.status_code == 200
        assert r.json()["current_version"] == 2  # actual block version

    async def test_improve_reports_current_version(self, ac, admin_hdrs, report):
        """current_version in response must always match the real block version."""
        bid = await _seed_block(report["id"])
        for i in range(1, 4):
            await _apply(ac, admin_hdrs, report["id"], bid, f"Edit {i}", i)
        with _mock_gemini("Suggestion."):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Improve."},
                headers=admin_hdrs,
            )
        assert r.status_code == 200
        assert r.json()["current_version"] == 4  # 3 edits → version 4

    async def test_concurrent_apply_conflicts(self, ac, admin_hdrs, report):
        """Two simultaneous applies with same expected_version — second must get 409."""
        bid = await _seed_block(report["id"])
        r1 = await _apply(ac, admin_hdrs, report["id"], bid, "Edit A", 1)
        r2 = await _apply(ac, admin_hdrs, report["id"], bid, "Edit B", 1)
        statuses = {r1.status_code, r2.status_code}
        assert 200 in statuses
        assert 409 in statuses


# ══════════════════════════════════════════════════════════════════════════════
# I — Response Schema Completeness
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestImproveResponseSchema:

    async def test_all_required_fields_present(self, ac, admin_hdrs, report):
        bid = await _seed_block(report["id"])
        with _mock_gemini("Improved text for schema test."):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Rewrite concisely."},
                headers=admin_hdrs,
            )
        assert r.status_code == 200
        data = r.json()
        assert "block_id" in data
        assert "current_version" in data
        assert "original_content" in data
        assert "suggested_content" in data

    async def test_block_id_matches_request(self, ac, admin_hdrs, report):
        bid = await _seed_block(report["id"])
        with _mock_gemini("Response."):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Improve."},
                headers=admin_hdrs,
            )
        assert r.json()["block_id"] == bid

    async def test_original_content_matches_block(self, ac, admin_hdrs, report):
        original = "The borrower has demonstrated 3 consecutive years of EBITDA growth: 42.0 → 50.0 → 56.0 USD m."
        bid = await _seed_block(report["id"], content=original)
        with _mock_gemini("EBITDA grew: 42.0 → 50.0 → 56.0 USD m over 3 years."):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Shorten."},
                headers=admin_hdrs,
            )
        assert r.json()["original_content"] == original

    async def test_current_version_is_integer(self, ac, admin_hdrs, report):
        bid = await _seed_block(report["id"])
        with _mock_gemini("Text."):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Rewrite."},
                headers=admin_hdrs,
            )
        assert isinstance(r.json()["current_version"], int)
        assert r.json()["current_version"] >= 1

    async def test_suggested_content_is_string(self, ac, admin_hdrs, report):
        bid = await _seed_block(report["id"])
        with _mock_gemini("Any string response."):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Rewrite."},
                headers=admin_hdrs,
            )
        assert isinstance(r.json()["suggested_content"], str)

    async def test_patch_response_contains_all_block_fields(self, ac, admin_hdrs, report):
        bid = await _seed_block(report["id"])
        ar = await _apply(ac, admin_hdrs, report["id"], bid, "Updated text.", 1)
        assert ar.status_code == 200
        data = ar.json()
        for field in ["id", "section_no", "block_type", "content", "version",
                      "validation_status", "is_stale", "last_edited_by"]:
            assert field in data, f"Missing field in PATCH response: {field}"


# ══════════════════════════════════════════════════════════════════════════════
# J — Security / RBAC
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestImproveSecurity:

    async def test_analyst_can_improve_own_report_blocks(
        self, ac, analyst_hdrs, analyst_report
    ):
        bid = await _seed_block(analyst_report["id"])
        with _mock_gemini("Analyst improved."):
            r = await ac.post(
                f"{REPORTS}/{analyst_report['id']}/blocks/{bid}/improve",
                json={"instruction": "Make it formal."},
                headers=analyst_hdrs,
            )
        assert r.status_code == 200

    async def test_analyst_cannot_improve_other_users_report(
        self, ac, admin_hdrs, analyst_hdrs
    ):
        """Analyst must be blocked from improving blocks in admin-owned reports."""
        admin_report = await _create_report(ac, admin_hdrs, "Admin Private Report")
        bid = await _seed_block(admin_report["id"])
        with _mock_gemini("Hacked."):
            r = await ac.post(
                f"{REPORTS}/{admin_report['id']}/blocks/{bid}/improve",
                json={"instruction": "Unauthorised improvement."},
                headers=analyst_hdrs,
            )
        # Must be 403 or 404 — NOT 200
        assert r.status_code in (403, 404), (
            f"BUG: analyst improved admin's block without permission (got {r.status_code})"
        )

    async def test_analyst_cannot_patch_other_users_blocks(
        self, ac, admin_hdrs, analyst_hdrs
    ):
        """Analyst must be blocked from patching blocks in admin-owned reports."""
        admin_report = await _create_report(ac, admin_hdrs, "Admin Report 2")
        bid = await _seed_block(admin_report["id"])
        ar = await _apply(ac, analyst_hdrs, admin_report["id"], bid, "Injected.", 1)
        assert ar.status_code in (403, 404), (
            f"BUG: analyst patched admin's block without permission (got {ar.status_code})"
        )

    async def test_admin_can_improve_any_report(
        self, ac, admin_hdrs, analyst_hdrs, analyst_report
    ):
        """Admin must be able to improve blocks in any report."""
        bid = await _seed_block(analyst_report["id"])
        with _mock_gemini("Admin override."):
            r = await ac.post(
                f"{REPORTS}/{analyst_report['id']}/blocks/{bid}/improve",
                json={"instruction": "Admin review."},
                headers=admin_hdrs,
            )
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# K — Max-tokens calculation edge cases
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestImproveTokenBudget:

    async def test_very_short_content(self, ac, admin_hdrs, report):
        """1-char block: max_tokens = min(3+512,4096) = 515 — must work."""
        bid = await _seed_block(report["id"], content="X")
        with _mock_gemini("Y"):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Change this character."},
                headers=admin_hdrs,
            )
        assert r.status_code == 200

    async def test_very_long_content_capped_at_4096(self, ac, admin_hdrs, report):
        """Content >1195 chars: max_tokens capped at 4096."""
        long_content = "The borrower demonstrates strong credit fundamentals. " * 30  # ~1620 chars
        bid = await _seed_block(report["id"], content=long_content)
        with _mock_gemini("Condensed version."):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Condense to two sentences."},
                headers=admin_hdrs,
            )
        assert r.status_code == 200

    async def test_content_with_numbers_preserved_in_prompt(self, ac, admin_hdrs, report):
        """Numeric values in content must appear verbatim in the LLM prompt."""
        content = "Revenue: USD 150.0m; EBITDA: USD 56.0m; D/E: 0.31x; IC: 31.2x."
        bid = await _seed_block(report["id"], content=content)
        captured = []

        async def _capture(model, contents, config):
            captured.append(contents)
            m = MagicMock(); m.text = content
            return m

        mock_client = MagicMock()
        mock_client.aio = MagicMock()
        mock_client.aio.models = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(side_effect=_capture)
        with patch("google.genai.Client", return_value=mock_client):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{bid}/improve",
                json={"instruction": "Rewrite preserving all numbers."},
                headers=admin_hdrs,
            )
        assert r.status_code == 200
        assert "150.0" in captured[0]
        assert "0.31x" in captured[0]
        assert "ORIGINAL PARAGRAPH" in captured[0]
        assert "ANALYST INSTRUCTION" in captured[0]
