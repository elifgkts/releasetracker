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

IOS_STOP_MARKERS = {"app privacy", "ratings & reviews", "information", "developer website", "app support"}


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
        return dtparser.parse(s, dayfirst=True).date()
    except Exception:
        return None


def is_version_str(s: str) -> bool:
    s = (s or "").strip()
    # 1.2.3 / 12.4 / 550.0.0 gibi
    return bool(re.fullmatch(r"\d+(?:\.\d+){1,4}", s))


def to_clean_text(x, max_len: int = 700) -> str:
    """List/dict vs gelirse stringe çevir + çok uzunsa kısalt."""
    if x is None:
        return ""
    if isinstance(x, list):
        x = " ".join([str(i).strip() for i in x if str(i).strip()])
    else:
        x = str(x).strip()
    x = re.sub(r"\s+", " ", x).strip()
    if len(x) > max_len:
        return x[:max_len].rstrip() + "…"
    return x


@st.cache_data(ttl=60 * 30)
def fetch_ios_version_history(app_url: str, max_items: int = 10) -> list[dict]:
    """
    iOS App Store 'Version History' bölümünden son N versiyonu yakalar.
    HTML değişirse 'best-effort' çalışır.
    """
    r = requests.get(app_url, headers=HEADERS, timeout=25)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # "Version History" bul
    vh_idx = None
    for i, ln in enumerate(lines):
        if ln.lower() == "version history":
            vh_idx = i
            break
    if vh_idx is None:
        return []

    out = []
    i = vh_idx + 1

    while i < len(lines) and len(out) < max_items:
        ln = lines[i].strip()

        # bazen "Version 4.6.2" gibi gelir
        if ln.lower().startswith("version "):
            candidate = ln.split(" ", 1)[1].strip()
            if is_version_str(candidate):
                ln = candidate

        if not is_version_str(ln):
            # stop marker'a girdiysek çık
            if ln.lower() in IOS_STOP_MARKERS:
                break
            i += 1
            continue

        version = ln
        i += 1
        if i >= len(lines):
            break

        released_at = safe_parse_date(lines[i])
        i += 1

        notes = []
        while i < len(lines):
            peek = lines[i].strip()
            low = peek.lower()

            if low in IOS_STOP_MARKERS:
                break

            # sonraki versiyon bloğu
            cand = peek
            if cand.lower().startswith("version "):
                cand = cand.split(" ", 1)[1].strip()
            if is_version_str(cand):
                break

            # boş/başlık satırlarını filtrele
            if low in {"what’s new", "what's new", "version history"}:
                i += 1
                continue

            # notları topla
            cleaned = peek.lstrip("*•- ").strip()
            if cleaned:
                notes.append(cleaned)

            i += 1

        out.append({
            "platform": "iOS",
            "version": version,
            "released_at": released_at,
            "notes": to_clean_text(" ".join(notes))
        })

    return out


@st.cache_data(ttl=60 * 30)
def fetch_android_latest(package_name: str, lang: str = "tr", country: str = "TR") -> dict | None:
    """
    Android public kaynaklardan latest (best-effort).
    Öncelik: google_play_scraper (daha stabil)
    Fallback: gplay-scraper
    Fallback: HTML
    """
    lang = (lang or "tr").lower()
    country = (country or "TR").upper()

    # 1) google_play_scraper
    try:
        from google_play_scraper import app as gp_app

        data = gp_app(package_name, lang=lang, country=country)

        version = data.get("version") or "(unknown)"
        updated_ms = data.get("updated")  # epoch ms
        released_at = None
        if isinstance(updated_ms, (int, float)):
            released_at = datetime.fromtimestamp(updated_ms / 1000).date()

        notes = data.get("recentChanges") or ""
        notes = to_clean_text(notes)

        return {
            "platform": "Android",
            "version": to_clean_text(version, max_len=120),
            "released_at": released_at,
            "notes": notes
        }
    except Exception:
        pass

    # 2) gplay-scraper
    try:
        from gplay_scraper import GPlayScraper
        scraper = GPlayScraper(http_client="requests")
        data = scraper.app_analyze(package_name, lang=lang, country=country.lower())

        def pick(*keys):
            for k in keys:
                v = data.get(k)
                if v is not None and str(v).strip() != "":
                    return v
            return None

        version = pick("version", "appVersion", "softwareVersion", "current_version", "currentVersion") or "(unknown)"
        updated_raw = pick("last_update_date", "last_update", "lastUpdate", "updated", "updated_on", "updatedOn")
        notes = pick("recentChanges", "recent_changes", "whats_new", "whatsNew", "update_notes", "changelog") or ""

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
            "version": to_clean_text(version, max_len=120),
            "released_at": released_at,
            "notes": to_clean_text(notes)
        }
    except Exception:
        pass

    # 3) HTML fallback (version genelde gelmez)
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
                notes = " ".join(lines[i + 1:i + 8]).strip()
                break

        return {
            "platform": "Android",
            "version": "(not shown on web)",
            "released_at": updated,
            "notes": to_clean_text(notes)
        }
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


# ---------------- UI ----------------
apps = load_apps_config(APP_CONFIG_PATH)

st.title("QA Release Tracker (API’siz)")
st.caption("iOS: Version History (son N). Android: public latest (best-effort).")

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

                if str(a["version"]).lower().strip() == "varies with device":
                    st.warning(
                        "Android için Google Play public sayfası bazı uygulamalarda versiyon numarasını paylaşmıyor "
                        "('Varies with device'). Bu durumda API’siz net versiyon almak mümkün olmayabilir."
                    )

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
