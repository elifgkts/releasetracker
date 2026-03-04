# QA Release Tracker (No-API)

Streamlit app that shows:
- iOS: last N versions from App Store "Version History"
- Android: latest version + last updated + what's new (best-effort)

## Run locally
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
streamlit run streamlit_app.py
