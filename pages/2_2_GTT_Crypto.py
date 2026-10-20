import streamlit as st

st.set_page_config(page_title="Crypto Focus", page_icon="⚡", layout="wide")

from supabase import create_client
from gtt_process import cached_client
from crypto_flow import render_crypto_page


def _secret(name):
    try:
        return st.secrets[name]
    except Exception:
        return ""


try:
    supabase = cached_client(create_client(_secret("SUPABASE_URL"), _secret("SUPABASE_KEY")))
except Exception as e:
    st.error(f"Database connection failed: {e}")
    supabase = None

render_crypto_page(st, supabase)
