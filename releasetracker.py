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


# ----------------------------
# Utilities
# ----------------------------
def load_apps_config(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("apps", [])


def now_tr_date() -> date:
    # Streamlit Cloud UTC olabilir; TR için +3 saat
    return (datetime.utcnow() + timedelta(hours=3)).date()


def compute_date_range(mode: str, start: date, end: date, last_n: int, unit: str) -> tuple[date, date]:
    today = now_tr_date()
    if mode == "Tarih aralığı":
        return start, end

    if unit == "gün":
        return today - timedelta(days=last_n), today
    if unit == "hafta":
        return today - timedelta(weeks=last_n), today
    if unit == "ay":
        return today - relativedelta(months=last_n), today
    if unit == "yıl":
        return today - relativedelta(years=last_n), today

    return today - timedelta(days=30), today


def is_version_str(s: str) -> bool:
    s = (s or "").strip()
    # 9.6.4 / 1.2.3.4 / 12.1
    return bool(re.fullmatch(r"\d+(?:\.\d+){1,4}", s))


def clean_text(x, max_len: int = 700) -> str:
    if x is None:
        return ""
    if isinstance(x, list):
        x = " ".join([str(i).strip() for i in x if str(i).strip()])
    else:
        x = str(x).strip()
    x = re.sub(r"\s+", " ", x).strip()
    if len(x) > max_len:
        x = x[:max_len].rstrip() + "…"
    return x


def parse_relative_or_date(s: str, base: date) -> date | None:
    """
    TR: '4 gün önce', '1 hafta önce', '1 ay önce', '2 yıl önce', 'dün', 'bugün'
    EN: '4 days ago', '1 week ago', '1 month ago', 'yesterday', 'today'
    """
    s = (s or "").strip().lower()
    if not s:
        return None

    # TR short words
    if s in {"bugün"}:
        return base
    if s in {"dün"}:
        return base - timedelta(days=1)

    # EN short words
    if s in {"today"}:
        return base
    if s in {"yesterday"}:
        return base - timedelta(days=1)

    # TR patterns
    m = re.search(r"(\d+)\s*gün\s*önce", s)
    if m:
        return base - timedelta(days=int(m.group(1)))

    m = re.search(r"(\d+)\s*hafta\s*önce", s)
    if m:
        return base - timedelta(weeks=int(m.group(1)))

    m = re.search(r"(\d+)\s*ay\s*önce", s)
    if m:
        return base - relativedelta(months=int(m.group(1)))

    m = re.search(r"(\d+)\s*yıl\s*önce", s)
    if m:
        return base - relativedelta(years=int(m.group(1)))

    # EN patterns
    m = re.search(r"(\d+)\s*day[s]?\s*ago", s)
    if m:
        return base - timedelta(days=int(m.group(1)))

    m = re.search(r"(\d+)\s*week[s]?\s*ago", s)
    if m:
        return base - timedelta(weeks=int(m.group(1)))

    m = re.search(r"(\d+)\s*month[s]?\s*ago", s)
    if m:
        return base - relativedelta(months=int(m.group(1)))

    m = re.search(r"(\d+)\s*year[s]?\s*ago", s)
    if m:
        return base - relativedelta(years=int(m.group(1)))

    # Absolute date fallback
    try:
        return dtparser.parse(s, dayfirst=True).date()
    except Exception:
        return None


def apply_date_filter(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    if df.empty or "Release Date" not in df.columns:
        return df

    d = pd.to_datetime(df["Release Date"], errors="coerce")
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    mask = d.isna() | ((d >= start_ts) & (d <= end_ts))
    return df.loc[mask].copy()


# ----------------------------
# iOS: App Store Version History (TR/EN + relative time)
# ----------------------------
@st.cache_data(ttl=60 * 30)
def fetch_ios_version_history(app_url: str, max_items: int = 10) -> list[dict]:
    r = requests.get(app_url, headers=HEADERS, timeout=25)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # Headings can be TR or EN
    headings = {"version history", "sürüm geçmişi"}
    stop_markers = {
        "app privacy", "uygulama gizliliği",
        "ratings & reviews", "derecelendirmeler ve yorumlar", "puanlar ve yorumlar",
        "information", "bilgi"
    }

    # Find start
    start_idx = None
    for i, ln in enumerate(lines):
        if ln.strip().lower() in headings:
            start_idx = i
            break
    if start_idx is None:
        return []

    base = now_tr_date()
    out = []
    i = start_idx + 1

    # Scan after heading: version -> relative/date -> notes until next version
    while i < len(lines) and len(out) < max_items:
        ln = lines[i].strip()
        low = ln.lower()

        if low in stop_markers:
            break

        # Sometimes version appears like "Sürüm 9.6.4" or "Version 9.6.4"
        if low.startswith("sürüm "):
            ln = ln.split(" ", 1)[1].strip()
        if low.startswith("version "):
            ln = ln.split(" ", 1)[1].strip()

        if not is_version_str(ln):
            i += 1
            continue

        version = ln
        i += 1
        if i >= len(lines):
            break

        age_or_date = lines[i].strip()
        released_at = parse_relative_or_date(age_or_date, base)
        i += 1

        notes = []
        while i < len(lines):
            peek = lines[i].strip()
            plow = peek.lower()

            if plow in stop_markers:
                break

            # Next version begins
            pv = peek
            pvlow = pv.lower()
            if pvlow.startswith("sürüm "):
                pv = pv.split(" ", 1)[1].strip()
            if pvlow.startswith("version "):
                pv = pv.split(" ", 1)[1].strip()
            if is_version_str(pv):
                break

            # Skip obvious headers
            if plow in headings or plow in {"yenilikler", "what’s new", "what's new"}:
                i += 1
                continue

            cleaned = peek.lstrip("*•- ").strip()
            if cleaned:
                notes.append(cleaned)

            i += 1

        out.append({
            "platform": "iOS",
            "version": version,
            "released_at": released_at,
            "age_text": age_or_date,
            "notes": clean_text(" ".join(notes))
        })

    return out


# ----------------------------
# Android: best-effort (public)
# ----------------------------
@st.cache_data(ttl=60 * 30)
def fetch_android_latest(package_name: str, lang: str = "tr", country: str = "TR") -> dict | None:
    # Public Play Store often returns "Varies with device" for version.
    # We'll still show updated/changes best-effort.
    lang = (lang or "tr").lower()
    country = (country or "TR").upper()

    # Try google_play_scraper first if installed (optional)
    try:
        from google_play_scraper import app as gp_app  # optional dependency
        data = gp_app(package_name, lang=lang, country=country)

        version = data.get("version") or "(unknown)"
        updated_ms = data.get("updated")
        released_at = None
        if isinstance(updated_ms, (int, float)):
            released_at = datetime.fromtimestamp(updated_ms / 1000).date()

        notes = data.get("recentChanges") or ""
        return {
            "platform": "Android",
            "version": clean_text(version, 120),
            "released_at": released_at,
            "notes": clean_text(notes)
        }
    except Exception:
        pass

    # Fallback HTML (version usually not present)
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
                try:
                    updated = dtparser.parse(lines[i + 1]).date()
                except Exception:
                    updated = None
                break

        for i, ln in enumerate(lines):
            if ln.lower() in {"what’s new", "what's new"}:
                notes = " ".join(lines[i + 1:i + 10]).strip()
                break

        return {
            "platform": "Android",
            "version": "Varies with device",
            "released_at": updated,
            "notes": clean_text(notes)
        }
    except Exception:
        return None


# ----------------------------
# UI
# ----------------------------
apps = load_apps_config(APP_CONFIG_PATH)

st.title("QA Release Tracker")
st.caption("iOS: Sürüm Geçmişi'nden son N versiyon + 'X gün önce' → tarih hesaplama. Android: public latest (best-effort).")

with st.sidebar:
    st.header("Seçimler")

    app_name = st.selectbox("Uygulama", [a["name"] for a in apps])
    app_cfg = next(a for a in apps if a["name"] == app_name)

    platforms = st.multiselect("Platform", ["iOS", "Android"], default=["iOS", "Android"])

    mode = st.radio("Tarih filtresi", ["Tarih aralığı", "Son X"], horizontal=True)
    if mode == "Tarih aralığı":
        start = st.date_input("Başlangıç", value=now_tr_date() - timedelta(days=90))
        end = st.date_input("Bitiş", value=now_tr_date())
        last_n, unit = 30, "gün"
    else:
        last_n = st.number_input("Son kaç?", min_value=1, max_value=3650, value=30)
        unit = st.selectbox("Birim", ["gün", "hafta", "ay", "yıl"])
        start, end = now_tr_date() - timedelta(days=30), now_tr_date()

    start_date, end_date = compute_date_range(mode, start, end, int(last_n), unit)

    ios_last_n = st.slider("iOS kaç versiyon?", 1, 20, 3)

    lang = st.selectbox("Android dil (hl)", ["tr", "en"], index=0)
    country = st.selectbox("Android ülke (gl)", ["TR", "US", "GB", "DE"], index=0)

    run = st.button("Getir", type="primary")


if run:
    rows = []

    if "iOS" in platforms:
        with st.spinner("iOS Sürüm Geçmişi çekiliyor..."):
            ios_items = fetch_ios_version_history(app_cfg["ios_url"], max_items=ios_last_n)
            for it in ios_items:
                rows.append({
                    "App": app_cfg["name"],
                    "Platform": "iOS",
                    "Version": it["version"],
                    "Release Date": it["released_at"],
                    "Age": it.get("age_text", ""),
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
                    "Age": "",
                    "Notes": a["notes"],
                    "Source": "play.google.com (best-effort)"
                })

    df = pd.DataFrame(rows)

    if df.empty:
        st.error("Hiç veri gelmedi. (Store erişimi engellenmiş olabilir veya link/package yanlış olabilir.)")
        st.stop()

    df = apply_date_filter(df, start_date, end_date)

    # ISO week helper
    dts = pd.to_datetime(df["Release Date"], errors="coerce")
    iso = dts.dt.isocalendar()
    df["ISO Week"] = iso["year"].astype("Int64").astype(str) + "-W" + iso["week"].astype("Int64").astype(str).str.zfill(2)

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
