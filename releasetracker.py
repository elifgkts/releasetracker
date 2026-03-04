import io
import re
import time
import yaml
import requests
import streamlit as st
import pandas as pd
from bs4 import BeautifulSoup
from datetime import date, timedelta, datetime
from dateutil.relativedelta import relativedelta
from dateutil import parser as dtparser
from google_play_scraper import app as play_app

APP_CONFIG_PATH = "apps.yaml"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
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

def clean_text(x, max_len: int = 800) -> str:
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
    s = (s or "").strip().lower()
    if not s:
        return None

    if s in {"bugün", "today"}:
        return base
    if s in {"dün", "yesterday"}:
        return base - timedelta(days=1)

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

def fetch_text(url: str, timeout: int = 25, retries: int = 2) -> tuple[int, str]:
    last_status = 0
    last_text = ""
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            last_status = r.status_code
            last_text = r.text or ""
            if r.status_code == 200 and last_text:
                return last_status, last_text
            if r.status_code in {403, 429, 500, 502, 503, 504}:
                time.sleep(1.2 + attempt * 0.8)
                continue
            return last_status, last_text
        except Exception as e:
            last_status = 0
            last_text = f"ERROR: {type(e).__name__}: {e}"
            time.sleep(1.0 + attempt * 0.5)
    return last_status, last_text


# ----------------------------
# iOS: App Store Version History
# ----------------------------
@st.cache_data(ttl=60 * 30)
def fetch_ios_version_history(app_url: str, max_items: int = 10) -> list[dict]:
    status, html = fetch_text(app_url)
    if status != 200:
        return [{
            "platform": "iOS",
            "version": "N/A",
            "released_at": None,
            "age_text": "",
            "notes": f"App Store fetch failed. HTTP {status}.",
        }]

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    headings = {"version history", "sürüm geçmişi"}
    stop_markers = {
        "app privacy", "uygulama gizliliği",
        "ratings & reviews", "derecelendirmeler ve yorumlar", "puanlar ve yorumlar",
        "information", "bilgi",
    }

    start_idx = None
    for i, ln in enumerate(lines):
        if ln.strip().lower() in headings:
            start_idx = i
            break
    if start_idx is None:
        return [{
            "platform": "iOS",
            "version": "N/A",
            "released_at": None,
            "age_text": "",
            "notes": "Version History / Sürüm Geçmişi bulunamadı.",
        }]

    base = now_tr_date()
    out = []
    i = start_idx + 1

    def normalize_version_token(s: str) -> str:
        s = s.strip()
        low = s.lower()
        if low.startswith("sürüm "):
            return s.split(" ", 1)[1].strip()
        if low.startswith("version "):
            return s.split(" ", 1)[1].strip()
        return s

    def looks_like_version(s: str) -> bool:
        s = normalize_version_token(s)
        return bool(re.fullmatch(r"\d+(?:\.\d+){1,4}", s))

    while i < len(lines) and len(out) < max_items:
        ln = lines[i].strip()
        low = ln.lower()

        if low in stop_markers:
            break

        vtok = normalize_version_token(ln)
        if not looks_like_version(vtok):
            i += 1
            continue

        version = vtok
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

            if looks_like_version(peek):
                break

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
            "notes": clean_text(" ".join(notes)),
        })

    return out

# ----------------------------
# Android: Google Play Store + Aptoide Fallback
# ----------------------------
def get_aptoide_version(package_name: str) -> str:
    """
    Google Play versiyonu 'Varies with device' döndürdüğünde tam versiyonu Aptoide'den çeker.
    """
    try:
        url = f"https://ws75.aptoide.com/api/7/app/get/package_name={package_name}"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if "nodes" in data and "meta" in data["nodes"] and "data" in data["nodes"]["meta"]:
                app_data = data["nodes"]["meta"]["data"]
                version = app_data.get("file", {}).get("vername")
                return version
    except Exception:
        pass
    return None

@st.cache_data(ttl=60 * 30)
def fetch_android_version_gplay(package_name: str) -> list[dict]:
    try:
        # 1. Google Play'den genel bilgileri ve tarihi çek
        result = play_app(
            package_name,
            lang='tr',     
            country='tr'   
        )
        
        version = str(result.get('version', 'Bilinmiyor'))
        
        # 2. Eğer versiyon gizlenmişse Aptoide API'sine sor
        if version.lower() in ["varies with device", "cihaza göre değişir", "bilinmiyor", "none"]:
            fallback_version = get_aptoide_version(package_name)
            if fallback_version:
                version = fallback_version
        
        updated_ts = result.get('updated')
        if updated_ts:
            released_at = datetime.fromtimestamp(updated_ts).date()
        else:
            released_at = None
            
        recent_changes = result.get('recentChanges')
        notes = clean_text(recent_changes) if recent_changes else ""
        
        return [{
            "platform": "Android",
            "version": version,
            "released_at": released_at,
            "notes": notes
        }]
        
    except Exception as e:
        return [{
            "platform": "Android",
            "version": "N/A",
            "released_at": None,
            "notes": f"Play Store'dan veri çekilemedi. Hata: {e}"
        }]

# ----------------------------
# UI
# ----------------------------
try:
    apps = load_apps_config(APP_CONFIG_PATH)
except FileNotFoundError:
    st.error(f"{APP_CONFIG_PATH} dosyası bulunamadı. Lütfen apps.yaml dosyasını ana dizine ekleyin.")
    st.stop()

st.title("QA Release Tracker")
st.caption("iOS: Sürüm Geçmişi'nden son N versiyon. Android: Play Store'daki güncel versiyon.")

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
        last_n = st.number_input("Son kaç?", min_value=1, max_value=3650, value=8)
        unit = st.selectbox("Birim", ["gün", "hafta", "ay", "yıl"])
        start, end = now_tr_date() - timedelta(days=30), now_tr_date()

    start_date, end_date = compute_date_range(mode, start, end, int(last_n), unit)

    ios_last_n = st.slider("iOS kaç versiyon gelsin?", 1, 20, 3)

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
                    "Notes": it.get("notes", ""),
                    "Source": "apps.apple.com",
                })

    if "Android" in platforms:
        with st.spinner("Android güncel versiyonu çekiliyor..."):
            android_items = fetch_android_version_gplay(app_cfg["android_package"])
            for it in android_items:
                rows.append({
                    "App": app_cfg["name"],
                    "Platform": "Android",
                    "Version": it["version"],
                    "Release Date": it["released_at"],
                    "Age": "",
                    "Notes": it.get("notes", ""),
                    "Source": "play.google.com / aptoide",
                })

    df = pd.DataFrame(rows)

    if df.empty:
        st.error("Hiç veri gelmedi.")
        st.stop()

    df = apply_date_filter(df, start_date, end_date)

    # ISO Week
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
            file_name=f"{app_cfg.get('key', 'app')}_releases_{start_date}_{end_date}.csv",
            mime="text/csv",
        )
    with c2:
        buff = io.BytesIO()
        with pd.ExcelWriter(buff, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="releases")
        st.download_button(
            "Excel indir",
            buff.getvalue(),
            file_name=f"{app_cfg.get('key', 'app')}_releases_{start_date}_{end_date}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
else:
    st.info("Soldan seçim yapıp **Getir**’e bas.")
