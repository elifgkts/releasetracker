def dedupe_and_sort(df: pd.DataFrame) -> pd.DataFrame:
    """
    - Aynı Version (+Release Date) tekrarlarını atar
    - Release Date'e göre yeni -> eski sıralar
    """
    if df.empty:
        return df

    df = df.copy()

    # Release Date'i datetime yap (sorting için)
    df["_release_dt"] = pd.to_datetime(df["Release Date"], errors="coerce")

    # Dedupe: Version + _release_dt bazlı
    # (Notlar farklıysa ilk gördüğümüz kaydı tutar)
    df = df.sort_values(["_release_dt"], ascending=False, na_position="last")
    df = df.drop_duplicates(subset=["Version", "_release_dt"], keep="first")

    # Son sıralama: yeni -> eski
    df = df.sort_values(["_release_dt", "Version"], ascending=[False, False], na_position="last")

    df = df.drop(columns=["_release_dt"])
    return df
