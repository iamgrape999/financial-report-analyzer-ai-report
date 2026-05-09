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
                value = self._resolve_path(input_json, mapping.get("path", ""))
                if value is None:
                    continue

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
                fact_id = mapping["id_template"].format(entity=entity, period=period)

                float_value: Optional[float] = None
                try:
                    float_value = float(str(value).replace(",", "").replace("%", ""))
                except (ValueError, TypeError):
                    pass

                display = mapping.get("display_template", "").format(value=value) if mapping.get("display_template") else str(value)

                facts.append(
                    {
                        "id": fact_id,
                        "report_id": report_id,
                        "metric_name": mapping["metric"],
                        "entity": entity,
                        "period": period,
                        "value": float_value,
                        "value_text": str(value) if float_value is None else None,
                        "currency": mapping.get("currency"),
                        "unit": mapping.get("unit"),
                        "display": display,
                        "state": "validated",
                        "source_type": "analyst_input_json",
                        "source_priority": 1,
                        "source_section_no": self.section_no,
                    }
                )
            except Exception as exc:
                logger.debug("Fact mapping failed for %s: %s", mapping.get("id_template", "?"), exc)
                continue

        return facts

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
