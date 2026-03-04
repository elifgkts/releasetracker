import re
import yaml
import requests
import streamlit as st
import pandas as pd
from bs4 import BeautifulSoup
from datetime import date, timedelta, datetime
from dateutil.relativedelta import relativedelta
from dateutil import parser as dtparser

APP_CONFIG_PATH = "apps.yaml"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0 Safari/537.36"
    )
}

st.set_page_config(page_title="QA Release Tracker", layout="wide")


def load_apps_config(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("apps", [])


def compute_date_range(mode: str, start: date, end: date, last_n: int, unit: str) -> tuple[date, date]:
    today = date.today()
    if mode == "Tarih aralığı":
        return start, end

    if unit == "gün":
        return today - timedelta(days=last_n), today
    if unit == "hafta":
        return today - timedelta(weeks=last_n), today
    if unit == "ay":
        return (today - relativedelta(months=last_n)), today
    if unit == "yıl":
        return (today - relativedelta(years=last_n)), today

    return today - timedelta(days=30), today


def safe_parse_date(s: str) -> date | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        # dayfirst=True Türkiye tarih formatı için daha iyi
        return dtparser.parse(s, dayfirst=True).date()
    except Exception:
        return None


@st.cache_data(ttl=60 * 30)
def fetch_ios_version_history(app_url: str, max_items: int = 10) -> list[dict]:
    """
    App Store web sayfasındaki 'Version History' alanını parse eder.
    NOT: Apple zaman zaman HTML'i değiştirir; yine de pratikte çoğu uygulamada çalışıyor.
    """
    r = requests.get(app_url, headers=HEADERS, timeout=25)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # Find "Version History"
    vh_idx = None
    for i, ln in enumerate(lines):
        if ln.lower() == "version history":
            vh_idx = i
            break
    if vh_idx is None:
        return []

    out = []
    i = vh_idx + 1

    # Extract blocks: #### <version> / <date> / notes
    while i < len(lines) and len(out) < max_items:
        ln = lines[i]

        # In many App Store pages, version entries appear like "#### 4.6.2"
        m = re.match(r"^####\s*(.+)$", ln)
        if not m:
            i += 1
            continue

        version = m.group(1).strip()
        i += 1
        if i >= len(lines):
            break

        date_str = lines[i].strip()
        released_at = safe_parse_date(date_str)
        i += 1

        notes = []
        while i < len(lines):
            peek = lines[i].strip()

            # stop at next section
            if peek.lower() in {"app privacy", "ratings & reviews", "information"}:
                break

            # next version block
            if re.match(r"^####\s+.+$", peek):
                break

            # notes usually have bullet prefix
            if peek.startswith("*") or peek.startswith("•") or peek.startswith("-"):
                notes.append(peek.lstrip("*•- ").strip())

            i += 1

        out.append({
            "platform": "iOS",
            "version": version,
            "released_at": released_at,
            "notes": " ".join(notes).strip()
        })

    return out


@st.cache_data(ttl=60 * 30)
def fetch_android_latest(package_name: str, lang: str = "tr", country: str = "TR") -> dict | None:
    """
    Android tarafı public store’da genelde sadece 'latest' yakalanabiliyor.
    1) gplay-scraper ile (no API key) versiyon + updated + what's new almayı dener.
    2) olmazsa HTML fallback: Updated on + What's new (versiyon her zaman görünmeyebilir).
    """
    lang = (lang or "tr").lower()
    country = (country or "TR").lower()

    # 1) gplay-scraper
    try:
        from gplay_scraper import GPlayScraper  # pip: gplay-scraper
        scraper = GPlayScraper(http_client="requests")
        data = scraper.app_analyze(package_name, lang=lang, country=country)

        def pick(*keys):
            for k in keys:
                v = data.get(k)
                if v is not None and str(v).strip() != "":
                    return v
            return None

        version = pick("version", "appVersion", "softwareVersion", "app_version_name", "app_version")
        updated_raw = pick("last_update_date", "last_update", "lastUpdate", "updated", "updated_on", "updatedOn")
        notes = pick("update_notes", "recentChanges", "whats_new", "whatsNew", "changelog") or ""

        released_at = None
        if isinstance(updated_raw, (int, float)):
            try:
                released_at = datetime.fromtimestamp(updated_raw).date()
            except Exception:
                released_at = None
        elif isinstance(updated_raw, str):
            released_at = safe_parse_date(updated_raw)

        return {
            "platform": "Android",
            "version": str(version).strip() if version else "(unknown)",
            "released_at": released_at,
            "notes": str(notes).strip()
        }
    except Exception:
        pass

    # 2) fallback: HTML
    try:
        url = f"https://play.google.com/store/apps/details?id={package_name}&hl={lang}&gl={country}"
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text("\n")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

        updated = None
        notes = ""

        for i, ln in enumerate(lines):
            if ln.lower() == "updated on" and i + 1 < len(lines):
                updated = safe_parse_date(lines[i + 1])
                break

        for i, ln in enumerate(lines):
            if ln.lower() in {"what’s new", "what's new"}:
                notes = " ".join(lines[i + 1:i + 6]).strip()
                break

        return {
            "platform": "Android",
            "version": "(not shown on web)",
            "released_at": updated,
            "notes": notes
        }
    except Exception:
        return None


def apply_date_filter(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    if df.empty or "Release Date" not in df.columns:
        return df
    d = pd.to_datetime(df["Release Date"], errors="coerce").dt.date
    mask = d.isna() | ((d >= start) & (d <= end))
    return df.loc[mask].copy()


# ---------------- UI ----------------
apps = load_apps_config(APP_CONFIG_PATH)

st.title("QA Release Tracker (API’siz)")
st.caption("iOS: Version History'den son N versiyon. Android: public store'dan latest (best-effort).")

with st.sidebar:
    st.header("Seçimler")

    app_name = st.selectbox("Uygulama", [a["name"] for a in apps])
    app_cfg = next(a for a in apps if a["name"] == app_name)

    platforms = st.multiselect("Platform", ["iOS", "Android"], default=["iOS", "Android"])

    mode = st.radio("Tarih filtresi", ["Tarih aralığı", "Son X"], horizontal=True)
    if mode == "Tarih aralığı":
        start = st.date_input("Başlangıç", value=date.today() - timedelta(days=90))
        end = st.date_input("Bitiş", value=date.today())
        last_n, unit = 30, "gün"
    else:
        last_n = st.number_input("Son kaç?", min_value=1, max_value=3650, value=30)
        unit = st.selectbox("Birim", ["gün", "hafta", "ay", "yıl"])
        start, end = date.today() - timedelta(days=30), date.today()

    start_date, end_date = compute_date_range(mode, start, end, int(last_n), unit)

    ios_last_n = st.slider("iOS kaç versiyon gösterilsin?", 1, 20, 3)

    lang = st.selectbox("Android dil (hl)", ["tr", "en"], index=0)
    country = st.selectbox("Android ülke (gl)", ["TR", "US", "GB", "DE"], index=0)

    run = st.button("Getir", type="primary")


if run:
    rows = []

    if "iOS" in platforms:
        with st.spinner("iOS Version History çekiliyor..."):
            ios_items = fetch_ios_version_history(app_cfg["ios_url"], max_items=ios_last_n)
            for it in ios_items:
                rows.append({
                    "App": app_cfg["name"],
                    "Platform": "iOS",
                    "Version": it["version"],
                    "Release Date": it["released_at"],
                    "Notes": it["notes"],
                    "Source": "apps.apple.com"
                })

    if "Android" in platforms:
        with st.spinner("Android latest info çekiliyor..."):
            a = fetch_android_latest(app_cfg["android_package"], lang=lang, country=country)
            if a:
                rows.append({
                    "App": app_cfg["name"],
                    "Platform": "Android",
                    "Version": a["version"],
                    "Release Date": a["released_at"],
                    "Notes": a["notes"],
                    "Source": "play.google.com (best-effort)"
                })

    df = pd.DataFrame(rows)

    if df.empty:
        st.error("Hiç veri gelmedi. (Store erişimi engellenmiş olabilir veya link/package yanlış olabilir.)")
        st.stop()

    df = apply_date_filter(df, start_date, end_date)
    df = df.sort_values(["Platform", "Release Date"], ascending=[True, False], na_position="last")

    st.caption(f"Filtre: {start_date} – {end_date}")
    st.dataframe(df, use_container_width=True)

    c1, c2 = st.columns(2)
    with c1:
        st.download_button(
            "CSV indir",
            df.to_csv(index=False).encode("utf-8"),
            file_name=f"{app_cfg['key']}_releases_{start_date}_{end_date}.csv",
            mime="text/csv",
        )
    with c2:
        import io
        buff = io.BytesIO()
        with pd.ExcelWriter(buff, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="releases")
        st.download_button(
            "Excel indir",
            buff.getvalue(),
            file_name=f"{app_cfg['key']}_releases_{start_date}_{end_date}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
else:
    st.info("Soldan seçim yapıp **Getir**’e bas.")
