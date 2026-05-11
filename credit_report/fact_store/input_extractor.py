from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

try:
    from jsonpath_ng import parse as jsonpath_parse
    _JSONPATH_AVAILABLE = True
except ImportError:
    _JSONPATH_AVAILABLE = False

CONFIG_DIR = Path(__file__).parent / "fact_mapping_config"

_PERIOD_RE = re.compile(r"(\d{4})")
_ABBREV_RE = re.compile(r'["\']?([A-Z]{2,6})["\']?\s*\)')


class InputFactExtractor:
    """Extract CanonicalFact dicts from analyst section JSON using YAML mapping configs."""

    def __init__(self, section_no: int, industry: str = "marine") -> None:
        self.section_no = section_no
        config_path = CONFIG_DIR / industry / f"section_{section_no}.yaml"
        if config_path.exists() and _YAML_AVAILABLE:
            with config_path.open(encoding="utf-8") as f:
                self.config = yaml.safe_load(f) or {}
        else:
            if not config_path.exists():
                logger.info(
                    "No fact mapping config for section %d (%s). Skipping auto-extraction.",
                    section_no,
                    config_path,
                )
            self.config = {}

    def extract(self, report_id: str, input_json: dict) -> list[dict]:
        """Walk YAML fact mappings and return list of CanonicalFact dicts."""
        facts: list[dict] = []
        for mapping in self.config.get("facts", []):
            try:
                if "iterate_path" in mapping:
                    facts.extend(self._extract_iterate(report_id, input_json, mapping))
                else:
                    fact = self._extract_single(report_id, input_json, mapping)
                    if fact:
                        facts.append(fact)
            except Exception as exc:
                logger.debug("Fact mapping failed for %s: %s", mapping.get("id_template", "?"), exc)
                continue
        return facts

    def _extract_single(self, report_id: str, input_json: dict, mapping: dict) -> Optional[dict]:
        """Extract one fact from a static dot-notation path."""
        value = self._resolve_path(input_json, mapping.get("path", ""))
        if value is None:
            return None
        entity_raw = (
            self._resolve_path(input_json, mapping["entity_path"])
            if "entity_path" in mapping
            else mapping.get("entity", "UNKNOWN")
        )
        period_raw = (
            self._resolve_path(input_json, mapping["period_path"])
            if "period_path" in mapping
            else mapping.get("period", "")
        )
        entity = self._normalize_entity(str(entity_raw or "UNKNOWN"))
        period = self._normalize_period(str(period_raw or ""))
        return self._build_fact(report_id, mapping, entity, period, value)

    def _extract_iterate(self, report_id: str, input_json: dict, mapping: dict) -> list[dict]:
        """Iterate over all year-keys in a dict, emitting one fact per key.

        YAML directive: iterate_path points to a dict keyed by fiscal-year strings
        (e.g. "FY2024", "FY2025F"). The 'field' sub-key selects a value inside each entry.
        currency and unit can be resolved from a sibling path via currency_path / unit_path.

        Example YAML:
          iterate_path: 7A_borrower_financials.income_statement
          field: revenue
          metric: revenue
          entity: BORROWER
          id_template: "FIN-REVENUE-{entity}-{period}"
          currency_path: 7A_borrower_financials.reporting_currency
          unit_path: 7A_borrower_financials.unit
        """
        container = self._resolve_path(input_json, mapping["iterate_path"])
        if not isinstance(container, dict):
            return []

        currency = mapping.get("currency") or str(
            self._resolve_path(input_json, mapping.get("currency_path", "")) or ""
        ) or None
        unit = mapping.get("unit") or str(
            self._resolve_path(input_json, mapping.get("unit_path", "")) or ""
        ) or None

        entity_raw = (
            self._resolve_path(input_json, mapping["entity_path"])
            if "entity_path" in mapping
            else mapping.get("entity", "BORROWER")
        )
        entity = self._normalize_entity(str(entity_raw or "BORROWER"))

        field = mapping.get("field")
        facts: list[dict] = []
        for key, sub_obj in container.items():
            if not isinstance(sub_obj, dict):
                continue
            value = sub_obj.get(field) if field else sub_obj
            if value is None:
                continue
            period = self._normalize_period(str(key))
            override = {"currency": currency, "unit": unit}
            fact = self._build_fact(report_id, mapping, entity, period, value, override)
            facts.append(fact)
        return facts

    def _build_fact(
        self,
        report_id: str,
        mapping: dict,
        entity: str,
        period: str,
        value: Any,
        override: Optional[dict] = None,
    ) -> dict:
        """Assemble a CanonicalFact dict from resolved components."""
        template_id = mapping["id_template"].format(entity=entity, period=period)
        # Prefix with report_id shard so the same template doesn't collide across reports
        fact_id = f"{report_id[:8]}-{template_id}"
        float_value: Optional[float] = None
        try:
            float_value = float(str(value).replace(",", "").replace("%", ""))
        except (ValueError, TypeError):
            pass
        display = (
            mapping.get("display_template", "").format(value=value)
            if mapping.get("display_template")
            else str(value)
        )
        overrides = override or {}
        return {
            "id": fact_id,
            "report_id": report_id,
            "metric_name": mapping["metric"],
            "entity": entity,
            "period": period,
            "value": float_value,
            "value_text": str(value) if float_value is None else None,
            "currency": overrides.get("currency") or mapping.get("currency"),
            "unit": overrides.get("unit") or mapping.get("unit"),
            "display": display,
            "state": "validated",
            "source_type": "analyst_input_json",
            "source_priority": 1,
            "source_section_no": self.section_no,
        }

    def _resolve_path(self, obj: Any, path: str) -> Optional[Any]:
        """
        Simple dot-notation path resolver.
        Handles keys starting with numbers (e.g. '2B_solvency') which jsonpath_ng cannot.
        Supports bracket-index notation for lists: 'collateral_items[1].ltc_pct'.
        """
        if not path:
            return None
        # Strip leading '$.' if present
        path = path.lstrip("$.")
        import re
        current = obj
        for part in re.split(r"\.", path):
            if current is None:
                return None
            # Handle list index: e.g. 'collateral_items[1]'
            idx_match = re.match(r"^(.+)\[(\d+)\]$", part)
            if idx_match:
                key, idx = idx_match.group(1), int(idx_match.group(2))
                if isinstance(current, dict):
                    current = current.get(key)
                else:
                    return None
                if isinstance(current, list) and 0 <= idx < len(current):
                    current = current[idx]
                else:
                    return None
            elif isinstance(current, dict):
                current = current.get(part)
            else:
                return None
        return current

    @staticmethod
    def _normalize_period(raw: str) -> str:
        """FYE 31 Dec 2024 -> FY2024; 9M2025; H1 2024 -> H12024; etc."""
        upper = raw.upper()
        m = _PERIOD_RE.search(raw)
        yr = m.group(1) if m else "UNKNOWN"

        if "9M" in upper or "9 MONTH" in upper:
            return f"9M{yr}"
        if "6M" in upper or "H1" in upper or "INTERIM" in upper:
            return f"H1{yr}"
        if "H2" in upper:
            return f"H2{yr}"
        if "/" in raw:
            # FY2022/23 style
            return f"FY{raw.strip()}"
        return f"FY{yr}"

    @staticmethod
    def _normalize_entity(raw: str) -> str:
        """Extract abbreviation like EMA from 'Evergreen Marine (Asia) Pte. Ltd. ("EMA")'."""
        m = _ABBREV_RE.search(raw)
        if m:
            return m.group(1)
        # Fallback: first word if short enough
        first_word = raw.split()[0] if raw.strip() else raw
        return first_word[:10]
