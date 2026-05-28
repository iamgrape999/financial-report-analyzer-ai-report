"""
Auto-fetch financial documents from:
  - MOPS/TWSE  (Taiwan listed companies, via stock code)
  - SEC EDGAR  (US / foreign private issuers, via company name)
  - Direct URL (any publicly accessible PDF/DOCX)

All functions are async and share a single httpx.AsyncClient passed by the caller.
Each returns list[FetchedDoc]; errors are logged and swallowed so callers get
partial results rather than a hard failure.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

import httpx

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT   = 20.0   # seconds for metadata / search requests
_DOWNLOAD_TIMEOUT = 60.0  # seconds for binary file downloads
_MAX_FILE_BYTES  = 50 * 1024 * 1024  # 50 MB hard cap (matches server upload limit)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; CUB-CreditReport/1.0; "
        "+https://github.com/iamgrape999/financial-report-analyzer-ai-report)"
    ),
    "Accept": "text/html,application/pdf,application/json,*/*;q=0.8",
}


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class FetchedDoc:
    filename: str
    data: bytes
    source: str         # "mops" | "edgar" | "direct"
    document_type: str  # matches DOCUMENT_TYPES in generate.py


@dataclass
class FetchError:
    source: str
    message: str


# ── Shared download helper ────────────────────────────────────────────────────

async def _download_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout: float = _DOWNLOAD_TIMEOUT,
    max_bytes: int = _MAX_FILE_BYTES,
) -> bytes | None:
    """GET url → raw bytes, or None on any error / size exceeded."""
    try:
        r = await client.get(url, headers=_HEADERS, timeout=timeout, follow_redirects=True)
        r.raise_for_status()
        if len(r.content) > max_bytes:
            logger.warning("doc_fetcher: file too large (%d bytes) from %s — skipped", len(r.content), url)
            return None
        if len(r.content) < 512:
            logger.warning("doc_fetcher: suspiciously small response (%d bytes) from %s — skipped", len(r.content), url)
            return None
        return r.content
    except httpx.HTTPError as exc:
        logger.warning("doc_fetcher: download failed url=%s: %s", url, exc)
        return None


# ── MOPS / TWSE ───────────────────────────────────────────────────────────────
# Annual reports (年報) are filed to TWSE and stored on doc.twse.com.tw.
# Flow:
#   1. GET the document listing page for a given stock code + ROC year
#   2. Parse HTML for clkFile() JavaScript calls → file IDs
#   3. Download each file via t57sb02

_TWSE_LIST_URL     = "https://doc.twse.com.tw/server-java/t57sb01"
_TWSE_DOWNLOAD_URL = "https://doc.twse.com.tw/server-java/t57sb02"
_MOPS_LIST_URL     = "https://mops.twse.com.tw/mops/web/ajax_t163sb05"

_CLK_FILE_RE = re.compile(r"clkFile\('([^']{6,})'(?:,'(\d+)')?(?:,'([^']*)')?\)", re.IGNORECASE)
_HREF_PDF_RE = re.compile(r'href="([^"]+\.pdf[^"]*)"', re.IGNORECASE)


def _roc_year(gregorian_year: int) -> int:
    return gregorian_year - 1911


async def _twse_list_html(client: httpx.AsyncClient, stock_code: str, roc_year: int) -> str:
    """Fetch the TWSE document listing HTML for a stock/year."""
    params = {
        "step": "1", "colorchg": "1",
        "co_id": stock_code, "year": str(roc_year),
        "seamon": "", "mtype": "A", "dtype": "",
    }
    try:
        r = await client.get(_TWSE_LIST_URL, params=params, headers=_HEADERS, timeout=_FETCH_TIMEOUT)
        r.raise_for_status()
        return r.text
    except httpx.HTTPError as exc:
        logger.warning("mops: TWSE listing failed stock=%s year=%d: %s", stock_code, roc_year, exc)
        return ""


async def _mops_list_html(client: httpx.AsyncClient, stock_code: str, roc_year: int) -> str:
    """Fetch the MOPS document listing via POST for a stock/year."""
    payload = {
        "encodeURIComponent": "1", "step": "1", "firstin": "true",
        "off": "1", "TYPEK": "sii",
        "co_id": stock_code, "year": str(roc_year),
        "seamon": "", "report_id": "A",
    }
    try:
        r = await client.post(_MOPS_LIST_URL, data=payload, headers=_HEADERS, timeout=_FETCH_TIMEOUT)
        r.raise_for_status()
        return r.text
    except httpx.HTTPError as exc:
        logger.warning("mops: MOPS listing failed stock=%s year=%d: %s", stock_code, roc_year, exc)
        return ""


def _extract_file_ids(html: str) -> list[tuple[str, str]]:
    """Return list of (file_id, seq) pairs from clkFile() calls in HTML."""
    results = []
    for m in _CLK_FILE_RE.finditer(html):
        fid = m.group(1).strip()
        seq = (m.group(2) or "1").strip()
        if fid:
            results.append((fid, seq))
    return results


def _extract_pdf_hrefs(html: str) -> list[str]:
    """Return absolute PDF URLs found as href attributes in HTML."""
    hrefs = []
    for m in _HREF_PDF_RE.finditer(html):
        href = m.group(1).strip()
        if href.startswith("http"):
            hrefs.append(href)
        elif href.startswith("/"):
            hrefs.append("https://doc.twse.com.tw" + href)
    return hrefs


async def fetch_mops_annual_reports(
    client: httpx.AsyncClient,
    stock_code: str,
    *,
    years_back: int = 2,
) -> tuple[list[FetchedDoc], list[FetchError]]:
    """
    Fetch up to `years_back` annual report PDFs from MOPS/TWSE.

    Returns (docs, errors). Each doc's filename encodes stock code + year.
    At most 2 PDFs per year are downloaded (covers main + supplement filings).
    """
    stock_code = stock_code.strip().upper()
    current_gregorian = date.today().year
    docs: list[FetchedDoc] = []
    errors: list[FetchError] = []

    for offset in range(years_back):
        greg_year = current_gregorian - offset
        roc_year  = _roc_year(greg_year)

        # Try TWSE doc server first (primary), fall back to MOPS POST
        html = await _twse_list_html(client, stock_code, roc_year)
        if not html or len(html) < 200:
            html = await _mops_list_html(client, stock_code, roc_year)

        if not html or len(html) < 200:
            errors.append(FetchError("mops", f"{stock_code} ROC {roc_year}: no document listing returned"))
            continue

        file_ids = _extract_file_ids(html)
        pdf_hrefs = _extract_pdf_hrefs(html)

        # Download via clkFile IDs
        fetched_this_year = 0
        for fid, seq in file_ids[:3]:
            dl_url = f"{_TWSE_DOWNLOAD_URL}?filename={fid}"
            data = await _download_bytes(client, dl_url)
            if data and data[:4] == b"%PDF":
                fname = f"MOPS_{stock_code}_{greg_year}_annualreport_{fid}.pdf"
                docs.append(FetchedDoc(filename=fname, data=data, source="mops", document_type="annual_report"))
                logger.info("mops: downloaded %s (%d bytes) for %s %d", fname, len(data), stock_code, greg_year)
                fetched_this_year += 1
                if fetched_this_year >= 2:
                    break

        # Fall back to direct href PDFs if clkFile yielded nothing
        if fetched_this_year == 0:
            for href in pdf_hrefs[:2]:
                data = await _download_bytes(client, href)
                if data:
                    fname = f"MOPS_{stock_code}_{greg_year}_annualreport.pdf"
                    docs.append(FetchedDoc(filename=fname, data=data, source="mops", document_type="annual_report"))
                    logger.info("mops: downloaded href %s (%d bytes)", href, len(data))
                    break

    return docs, errors


# ── SEC EDGAR ─────────────────────────────────────────────────────────────────
# Flow:
#   1. Full-text search → get accession number + CIK of most recent 10-K / 20-F
#   2. Fetch filing index JSON to find primary document filename
#   3. Download primary document (HTML or PDF)

_EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
_EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_EDGAR_FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{primary}"
_EDGAR_INDEX_URL  = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form}&dateb=&owner=include&count=5&search_text=&output=atom"
_EDGAR_HEADERS = {
    **_HEADERS,
    "User-Agent": "CUB Credit Report Analyzer contact@cub.com",  # SEC requires real contact
}


def _padded_cik(cik: str | int) -> str:
    return str(cik).zfill(10)


async def _edgar_search(client: httpx.AsyncClient, company: str) -> list[dict]:
    """Return up to 5 recent 10-K / 20-F hits from EDGAR full-text search."""
    params = {
        "q": f'"{company}"',
        "forms": "10-K,20-F",
        "dateRange": "custom",
        "startdt": f"{date.today().year - 3}-01-01",
        "enddt": date.today().isoformat(),
    }
    try:
        r = await client.get(_EDGAR_SEARCH_URL, params=params, headers=_EDGAR_HEADERS, timeout=_FETCH_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return data.get("hits", {}).get("hits", [])
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("edgar: search failed company=%r: %s", company, exc)
        return []


async def _edgar_primary_doc(client: httpx.AsyncClient, cik: str, accession_no: str) -> str | None:
    """Return filename of the primary document for a filing, or None."""
    acc_clean = accession_no.replace("-", "")
    index_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_clean}/{accession_no}-index.json"
    try:
        r = await client.get(index_url, headers=_EDGAR_HEADERS, timeout=_FETCH_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        # primary document is typically first item with type == "10-K" or "20-F"
        for item in data.get("directory", {}).get("item", []):
            name = item.get("name", "")
            if name.lower().endswith((".htm", ".html", ".pdf")):
                return name
        return None
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("edgar: filing index failed cik=%s acc=%s: %s", cik, accession_no, exc)
        return None


async def fetch_edgar_filings(
    client: httpx.AsyncClient,
    company_name: str,
    *,
    max_docs: int = 2,
) -> tuple[list[FetchedDoc], list[FetchError]]:
    """
    Fetch the most recent 10-K / 20-F filings from SEC EDGAR for `company_name`.

    Returns (docs, errors). At most `max_docs` files are downloaded.
    Primary documents are typically large HTML; PDFs are preferred when available.
    """
    docs: list[FetchedDoc] = []
    errors: list[FetchError] = []

    hits = await _edgar_search(client, company_name)
    if not hits:
        errors.append(FetchError("edgar", f"No 10-K/20-F filings found for '{company_name}' in EDGAR"))
        return docs, errors

    seen_acc: set[str] = set()
    for hit in hits[:5]:
        if len(docs) >= max_docs:
            break
        src = hit.get("_source", {})
        accession_no = src.get("accession_no", "").strip()
        cik = str(src.get("entity_id") or src.get("cik") or "").strip().lstrip("0")
        form_type = src.get("form_type", "10-K").strip()
        period = src.get("period_of_report", "").replace("-", "")[:6]

        if not accession_no or not cik or accession_no in seen_acc:
            continue
        seen_acc.add(accession_no)

        primary = await _edgar_primary_doc(client, cik, accession_no)
        if not primary:
            logger.warning("edgar: could not resolve primary doc cik=%s acc=%s", cik, accession_no)
            continue

        acc_clean = accession_no.replace("-", "")
        dl_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_clean}/{primary}"
        data = await _download_bytes(client, dl_url, timeout=_DOWNLOAD_TIMEOUT)
        if not data:
            continue

        ext = primary.rsplit(".", 1)[-1].lower() if "." in primary else "htm"
        safe_company = re.sub(r"[^\w]", "_", company_name)[:30]
        fname = f"EDGAR_{safe_company}_{form_type}_{period}.{ext}"
        docs.append(FetchedDoc(filename=fname, data=data, source="edgar", document_type="financial_statement"))
        logger.info("edgar: downloaded %s (%d bytes) for %r", fname, len(data), company_name)

    if not docs:
        errors.append(FetchError("edgar", f"Found EDGAR filings for '{company_name}' but all downloads failed"))

    return docs, errors


# ── Direct URL ────────────────────────────────────────────────────────────────

async def fetch_direct_url(
    client: httpx.AsyncClient,
    url: str,
    *,
    filename: str | None = None,
) -> tuple[FetchedDoc | None, FetchError | None]:
    """Download a single document from a direct URL."""
    try:
        r = await client.get(url, headers=_HEADERS, timeout=120.0, follow_redirects=True)
        r.raise_for_status()
        data = r.content
    except httpx.HTTPStatusError as exc:
        return None, FetchError("direct", f"HTTP {exc.response.status_code} from server — URL may require authentication or the file no longer exists: {url}")
    except httpx.TimeoutException:
        return None, FetchError("direct", f"Download timed out (120 s) — file may be too large or the server is slow: {url}")
    except httpx.HTTPError as exc:
        return None, FetchError("direct", f"Network error downloading {url}: {exc}")

    if len(data) > _MAX_FILE_BYTES:
        return None, FetchError("direct", f"File too large ({len(data) // 1_048_576} MB) — max 50 MB: {url}")

    content_type = r.headers.get("content-type", "")
    if "text/html" in content_type:
        preview = data[:120].decode("utf-8", errors="replace").strip()
        return None, FetchError("direct", f"Server returned an HTML page instead of a document (the URL may redirect to a login wall). Preview: {preview!r}")

    if len(data) < 512:
        return None, FetchError("direct", f"Response only {len(data)} bytes — likely an error page, not a document: {url}")

    if not filename:
        path_part = url.split("?")[0].rstrip("/").split("/")[-1]
        filename = path_part if "." in path_part else "document.pdf"
    filename = re.sub(r"[^\w.\-]", "_", filename)

    # Guess document type from filename
    name_lower = filename.lower()
    if any(kw in name_lower for kw in ("annual", "year", "年報")):
        doc_type = "annual_report"
    elif any(kw in name_lower for kw in ("financial", "statement", "finance", "財報")):
        doc_type = "financial_statement"
    else:
        doc_type = "other"

    return FetchedDoc(filename=filename, data=data, source="direct", document_type=doc_type), None


# ── Orchestrator ──────────────────────────────────────────────────────────────

async def run_auto_fetch(
    *,
    sources: list[str],
    stock_code: str | None = None,
    company_name: str | None = None,
    direct_urls: list[str] | None = None,
) -> tuple[list[FetchedDoc], list[FetchError]]:
    """
    Run all enabled fetchers and aggregate results.

    sources: subset of ["mops", "edgar", "direct"]
    Returns (all_docs, all_errors).
    """
    all_docs: list[FetchedDoc] = []
    all_errors: list[FetchError] = []

    async with httpx.AsyncClient(follow_redirects=True) as client:
        if "mops" in sources:
            if stock_code:
                d, e = await fetch_mops_annual_reports(client, stock_code)
                all_docs.extend(d)
                all_errors.extend(e)
            else:
                all_errors.append(FetchError("mops", "stock_code is required for MOPS source"))

        if "edgar" in sources:
            if company_name:
                d, e = await fetch_edgar_filings(client, company_name)
                all_docs.extend(d)
                all_errors.extend(e)
            else:
                all_errors.append(FetchError("edgar", "company_name is required for EDGAR source"))

        if "direct" in sources:
            for url in (direct_urls or []):
                doc, err = await fetch_direct_url(client, url)
                if doc:
                    all_docs.append(doc)
                if err:
                    all_errors.append(err)

    return all_docs, all_errors
