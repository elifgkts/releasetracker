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


def clean_text(x, max_len: int = 900) -> str:
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


def parse_relative_or_date(s: str, base: date) -> date | None:
    """
    TR: '4 gün önce', '1 hafta önce', '1 ay önce', '2 yıl önce', 'dün', 'bugün'
    EN: '4 days ago', '1 week ago', '1 month ago', 'yesterday', 'today'
    App Store kısa tarih: '5 Feb' gibi
    """
    s = (s or "").strip().lower()
    if not s:
        return None

    if s in {"bugün", "today"}:
        return base
    if s in {"dün", "yesterday"}:
        return base - timedelta(days=1)

    # TR
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

    # EN
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

    # Absolute fallback
    try:
        d = dtparser.parse(s, dayfirst=True).date()
        if d > base + timedelta(days=7):
            d = date(d.year - 1, d.month, d.day)
        return d
    except Exception:
        return None


def filter_in_range(items: list[dict], start: date, end: date) -> list[dict]:
    """Tarihi parse edilemeyen kayıtları dahil etmeyiz (filtre mantığı net kalsın)."""
    return [
        it for it in items
        if isinstance(it.get("released_at"), date) and (start <= it["released_at"] <= end)
    ]


def add_iso_week(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "Release Date" not in df.columns:
        return df
    dts = pd.to_datetime(df["Release Date"], errors="coerce")
    iso = dts.dt.isocalendar()
    df = df.copy()
    df["ISO Week"] = iso["year"].astype("Int64").astype(str) + "-W" + iso["week"].astype("Int64").astype(str).str.zfill(2)
    return df


def dedupe_and_sort(df: pd.DataFrame) -> pd.DataFrame:
    """Duplicate versiyonları kaldır + kesin yeni->eski sırala + index'i sıfırla."""
    if df.empty:
        return df

    df = df.copy()
    df["_release_dt"] = pd.to_datetime(df["Release Date"], errors="coerce")

    # önce yeni->eski sıralayıp dupe'larda ilk kaydı tut
    df = df.sort_values(["_release_dt"], ascending=False, na_position="last")
    df = df.drop_duplicates(subset=["Version", "_release_dt"], keep="first")

    # final sort: yeni->eski
    df = df.sort_values(["_release_dt", "Version"], ascending=[False, False], na_position="last")
    df = df.drop(columns=["_release_dt"]).reset_index(drop=True)
    return df


# ----------------------------
# iOS: App Store Version History (tam liste)
# ----------------------------
@st.cache_data(ttl=60 * 30)
def fetch_ios_version_history(app_url: str) -> list[dict]:
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
            "notes": "Sürüm Geçmişi / Version History bulunamadı (Apple sayfa yapısı değişmiş olabilir).",
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

    while i < len(lines):
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
# Android: Uptodown versions (tam liste)
# ----------------------------
UPTODOWN_VERSIONS_URL_BY_PACKAGE = {
    "com.turkcell.gncplay": "https://turkcell-gncplay.en.uptodown.com/android/versions",  # fizy
    "com.turkcell.bip": "https://bip.en.uptodown.com/android/versions",                   # BiP
    "tr.com.turkcell.akillidepo": "https://akll-depo.en.uptodown.com/android/versions",   # lifebox
    "com.turkcell.ott": "https://turkcell-tv.en.uptodown.com/android/versions",           # TV+
}

TR_MONTHS = {
    "oca": 1, "ocak": 1,
    "şub": 2, "sub": 2, "şubat": 2, "subat": 2,
    "mar": 3, "mart": 3,
    "nis": 4, "nisan": 4,
    "may": 5, "mayıs": 5, "mayis": 5,
    "haz": 6, "haziran": 6,
    "tem": 7, "temmuz": 7,
    "ağu": 8, "agu": 8, "ağustos": 8, "agustos": 8,
    "eyl": 9, "eylül": 9, "eylul": 9,
    "eki": 10, "ekim": 10,
    "kas": 11, "kasım": 11, "kasim": 11,
    "ara": 12, "aralık": 12, "aralik": 12,
}

def parse_uptodown_date(date_str: str) -> date | None:
    s = (date_str or "").strip()
    if not s:
        return None
    try:
        return dtparser.parse(s, dayfirst=True).date()
    except Exception:
        pass
    m = re.match(r"^(\d{1,2})\s+([A-Za-zÇĞİÖŞÜçğıöşü\.]+)\s+(\d{4})$", s)
    if m:
        day = int(m.group(1))
        mon_raw = m.group(2).strip().lower().replace(".", "")
        year = int(m.group(3))
        mon = TR_MONTHS.get(mon_raw)
        if mon:
            try:
                return date(year, mon, day)
            except Exception:
                return None
    return None


def extract_uptodown_versions(full_text: str) -> list[dict]:
    t = (full_text or "").replace("\xa0", " ")
    t = re.sub(r"\s+", " ", t).strip()

    date_pat = r"(?:[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}|\d{1,2}\s+[A-Za-zÇĞİÖŞÜçğıöşü\.]+\s+\d{4})"
    pat = re.compile(
        rf"\b(apk|xapk)\s+([0-9A-Za-z.\-_]+)\s+Android\s*\+\s*([0-9.]+)\s+({date_pat})\b",
        re.IGNORECASE
    )

    out = []
    seen = set()

    for m in pat.finditer(t):
        file_type = m.group(1).lower()
        version = m.group(2).strip()
        date_str = m.group(4).strip()
        released_at = parse_uptodown_date(date_str)

        key = (version, released_at)
        if key in seen:
            continue
        seen.add(key)

        out.append({
            "platform": "Android",
            "version": version,
            "released_at": released_at,
            "notes": file_type.upper(),
        })

    return out


@st.cache_data(ttl=60 * 30)
def fetch_android_versions_uptodown(package_name: str) -> list[dict]:
    url = UPTODOWN_VERSIONS_URL_BY_PACKAGE.get(package_name)
    if not url:
        return [{
            "platform": "Android",
            "version": "N/A",
            "released_at": None,
            "notes": f"Uptodown URL mapping yok: {package_name}",
        }]

    status, html = fetch_text(url)
    if status != 200:
        return [{
            "platform": "Android",
            "version": "N/A",
            "released_at": None,
            "notes": f"Uptodown fetch failed. HTTP {status}.",
        }]

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")

    items = extract_uptodown_versions(text)
    if not items:
        snippet = clean_text(text, 500)
        return [{
            "platform": "Android",
            "version": "N/A",
            "released_at": None,
            "notes": f"Uptodown parse edilemedi. Snippet: {snippet}",
        }]

    return items


# ----------------------------
# UI
# ----------------------------
apps = load_apps_config(APP_CONFIG_PATH)

st.title("QA Release Tracker")
st.caption("iOS ve Android ayrı tablolarda. Tekrar yok. Sıralama yeni→eski.")

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

    run = st.button("Getir", type="primary")


if run:
    st.caption(f"Filtre: {start_date} – {end_date}")

    # iOS
    ios_df = pd.DataFrame()
    if "iOS" in platforms:
        with st.spinner("iOS sürüm geçmişi çekiliyor..."):
            ios_all = fetch_ios_version_history(app_cfg["ios_url"])
        ios_in_range = filter_in_range(ios_all, start_date, end_date)

        if ios_all and ios_all[0].get("version") == "N/A":
            st.warning(f"iOS kaynak mesajı: {ios_all[0].get('notes','')}")

        if ios_in_range:
            ios_df = pd.DataFrame([{
                "App": app_cfg["name"],
                "Platform": "iOS",
                "Version": it["version"],
                "Release Date": it["released_at"],
                "Age": it.get("age_text", ""),
                "Notes": it.get("notes", ""),
                "Source": "apps.apple.com",
            } for it in ios_in_range])
            ios_df = add_iso_week(ios_df)
            ios_df = dedupe_and_sort(ios_df)

    # Android
    android_df = pd.DataFrame()
    if "Android" in platforms:
        with st.spinner("Android sürüm geçmişi çekiliyor..."):
            android_all = fetch_android_versions_uptodown(app_cfg["android_package"])
        android_in_range = filter_in_range(android_all, start_date, end_date)

        if android_all and android_all[0].get("version") == "N/A":
            st.warning(f"Android kaynak mesajı: {android_all[0].get('notes','')}")

        if android_in_range:
            android_df = pd.DataFrame([{
                "App": app_cfg["name"],
                "Platform": "Android",
                "Version": it["version"],
                "Release Date": it["released_at"],
                "Age": "",
                "Notes": it.get("notes", ""),
                "Source": "uptodown.com",
            } for it in android_in_range])
            android_df = add_iso_week(android_df)
            android_df = dedupe_and_sort(android_df)

    # Tables
    if "iOS" in platforms:
        st.subheader("iOS")
        if ios_df.empty:
            st.info("Seçtiğin tarih aralığında iOS kaydı bulunamadı.")
        else:
            st.dataframe(ios_df, use_container_width=True)
            st.download_button(
                "iOS CSV indir",
                ios_df.to_csv(index=False).encode("utf-8"),
                file_name=f"{app_cfg['key']}_ios_{start_date}_{end_date}.csv",
                mime="text/csv",
            )

    if "Android" in platforms:
        st.subheader("Android")
        if android_df.empty:
            st.info("Seçtiğin tarih aralığında Android kaydı bulunamadı.")
        else:
            st.dataframe(android_df, use_container_width=True)
            st.download_button(
                "Android CSV indir",
                android_df.to_csv(index=False).encode("utf-8"),
                file_name=f"{app_cfg['key']}_android_{start_date}_{end_date}.csv",
                mime="text/csv",
            )

    # Combined download
    combined = pd.concat([df for df in [ios_df, android_df] if not df.empty], ignore_index=True)
    if not combined.empty:
        st.divider()
        c1, c2 = st.columns(2)
        with c1:
            st.download_button(
                "Tümü CSV indir",
                combined.to_csv(index=False).encode("utf-8"),
                file_name=f"{app_cfg['key']}_all_{start_date}_{end_date}.csv",
                mime="text/csv",
            )
        with c2:
            import io
            buff = io.BytesIO()
            with pd.ExcelWriter(buff, engine="openpyxl") as writer:
                combined.to_excel(writer, index=False, sheet_name="releases")
            st.download_button(
                "Tümü Excel indir",
                buff.getvalue(),
                file_name=f"{app_cfg['key']}_all_{start_date}_{end_date}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

else:
    st.info("Soldan seçim yapıp **Getir**’e bas.")
