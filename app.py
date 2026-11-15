import streamlit as st

st.set_page_config(page_title="Job Apply Assistant", page_icon="💼", layout="wide")

from jobapply.ui import require_login  # noqa: E402

require_login()

pages = [
    st.Page("views/find_posts.py", title="Find posts", icon=":material/search:", default=True),
    st.Page("views/apply.py", title="Apply", icon=":material/send:"),
    st.Page("views/audit.py", title="Audit trail", icon=":material/fact_check:"),
    st.Page("views/settings.py", title="Settings", icon=":material/settings:"),
]
st.navigation(pages).run()
