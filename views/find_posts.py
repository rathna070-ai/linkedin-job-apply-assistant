import pandas as pd
import streamlit as st

from jobapply.config import get_secret
from jobapply.llm import ClaudeError
from jobapply.scraper import POSTED_LIMITS, PRICE_PER_POST_USD, build_queries
from jobapply.services import analyze_pending, run_search
from jobapply.ui import flash, get_claude, get_store, show_flashes

store = get_store()
settings = store.get_settings()
claude = get_claude()

st.title("Find hiring posts")
st.caption("Search LinkedIn posts by hashtag, score each one against your resume, and shortlist the ones to apply to.")
show_flashes()

if store.get_latest_resume() is None:
    st.warning("Upload your latest resume in **Settings** first. Claude needs it to score posts.", icon=":material/description:")

POSTED_LABELS = {"24h": "Last 24 hours", "week": "Last week", "month": "Last month"}

with st.form("search"):
    c1, c2 = st.columns([2, 1])
    hashtags = c1.text_input(
        "Hashtags",
        settings["hashtags"],
        help="Separate with spaces or commas, for example: #hiring #deliverymanager",
    )
    location = c2.text_input("Location (optional)", settings["location"])
    keywords = st.text_area(
        "Role keywords (optional, one search per line)",
        settings["keywords"],
        height=110,
        help="Each line becomes its own LinkedIn search, combined with the hashtags and location.",
    )
    c3, c4, c5 = st.columns(3)
    posted_limit = c3.selectbox(
        "Posted within",
        POSTED_LIMITS,
        index=POSTED_LIMITS.index(settings["posted_limit"]) if settings["posted_limit"] in POSTED_LIMITS else 1,
        format_func=POSTED_LABELS.get,
    )
    max_posts = c4.number_input("Max posts per search", min_value=5, max_value=200, step=5, value=int(settings["max_posts"]))
    with c5:
        require_hashtag = st.checkbox("Post must contain one of the hashtags", value=bool(settings["require_hashtag"]))
        only_email = st.checkbox("Only posts with an email address", value=bool(settings["only_with_email"]))
    submitted = st.form_submit_button("Search LinkedIn", type="primary", icon=":material/search:")

if submitted:
    queries = build_queries(hashtags, keywords, location)
    store.save_settings(
        hashtags=hashtags,
        keywords=keywords,
        location=location,
        posted_limit=posted_limit,
        max_posts=int(max_posts),
        require_hashtag=require_hashtag,
        only_with_email=only_email,
    )
    apify_token = get_secret("APIFY_TOKEN")
    if not queries:
        st.error("Enter at least one hashtag or keyword.")
    elif not apify_token:
        st.error("APIFY_TOKEN is not set. Add it in the app secrets (see Settings > Connections).")
    else:
        max_cost = len(queries) * int(max_posts) * PRICE_PER_POST_USD
        st.caption(f"Searches: {' · '.join(queries)} — Apify cost at most about ${max_cost:.2f}")
        with st.status("Searching LinkedIn...", expanded=True) as status:
            try:
                summary = run_search(
                    store=store,
                    apify_token=apify_token,
                    claude=claude,
                    queries=queries,
                    hashtags=hashtags,
                    max_posts=int(max_posts),
                    posted_limit=posted_limit,
                    require_hashtag=require_hashtag,
                    only_with_email=only_email,
                    progress=status.write,
                )
                status.update(
                    label=(
                        f"Done: {summary['found']} posts found, {summary['kept']} kept "
                        f"({summary['new']} new), {summary['analyzed']} scored"
                    ),
                    state="complete",
                    expanded=False,
                )
                if summary["note"]:
                    st.warning(summary["note"])
            except ClaudeError as exc:
                status.update(label="Posts were saved, but scoring failed", state="error")
                st.error(str(exc))
            except Exception as exc:
                status.update(label="Search failed", state="error")
                st.error(f"Search failed: {exc}")
        settings = store.get_settings()

st.divider()
st.subheader("Results")

c1, c2, c3 = st.columns([2, 1, 1])
threshold = c1.slider(
    "Minimum match score (confidence)",
    min_value=0,
    max_value=100,
    value=int(settings["match_threshold"]),
    step=5,
    help="Posts scored at or above this value are marked eligible. The score is Claude's estimate of how well "
    "your resume fits the post (85+ strong, 60-84 partial, below 60 weak).",
)
if threshold != int(settings["match_threshold"]):
    store.save_settings(match_threshold=threshold)
scope = c2.radio("Show", ["Latest search", "All saved posts"], index=0)
only_eligible = c3.checkbox("Only eligible posts", value=True)
hide_applied = c3.checkbox("Hide posts already applied to", value=True)

query = {}
if scope == "Latest search":
    if settings.get("last_search_id"):
        query["search_ids"] = settings["last_search_id"]
posts = store.list_posts(query)

unscored = [p["_id"] for p in posts if "analysis" not in p]
if unscored:
    cols = st.columns([3, 1])
    cols[0].info(f"{len(unscored)} posts are not scored yet.")
    if cols[1].button("Score them with Claude", disabled=claude is None or store.get_latest_resume() is None):
        bar = st.progress(0.0, text="Scoring...")
        try:
            count = analyze_pending(store, claude, unscored, progress=lambda d, t: bar.progress(d / t, text=f"Batch {d}/{t}"))
            flash("success", f"Scored {count} posts.")
            st.rerun()
        except (ClaudeError, ValueError) as exc:
            st.error(str(exc))

rows = []
for p in posts:
    a = p.get("analysis") or {}
    score = a.get("match_score")
    eligible = score is not None and score >= threshold
    if only_eligible and not eligible:
        continue
    if hide_applied and p.get("applied"):
        continue
    rows.append(
        {
            "id": p["_id"],
            "Shortlist": bool(p.get("shortlisted")),
            "Match %": score,
            "Eligible": eligible,
            "Company": a.get("company", ""),
            "Role": a.get("role", ""),
            "Location": a.get("location", ""),
            "Email": a.get("contact_email") or ", ".join(p.get("emails", [])),
            "Posted": (p.get("posted_at") or "")[:10],
            "Author": p.get("author_name", ""),
            "Applied": "Yes" if p.get("applied") else "",
            "Post": p.get("url", ""),
        }
    )

if not rows:
    hidden = len(posts)
    if hidden:
        st.info(f"No posts match the filters. {hidden} posts are hidden; lower the minimum score or untick the filters.")
    else:
        st.info("No posts yet. Run a search above.")
else:
    df = pd.DataFrame(rows).sort_values("Match %", ascending=False, na_position="last")
    st.caption(f"{len(df)} of {len(posts)} posts shown. Tick **Shortlist** for the posts you want to apply to.")
    edited = st.data_editor(
        df,
        hide_index=True,
        disabled=[c for c in df.columns if c != "Shortlist"],
        column_config={
            "id": None,
            "Shortlist": st.column_config.CheckboxColumn("Shortlist", width="small"),
            "Match %": st.column_config.ProgressColumn("Match %", min_value=0, max_value=100, format="%d%%"),
            "Eligible": st.column_config.CheckboxColumn("Eligible", width="small"),
            "Post": st.column_config.LinkColumn("Post", display_text="Open"),
        },
        key=f"posts_editor_{scope}_{threshold}_{only_eligible}_{hide_applied}",
    )
    b1, b2, _ = st.columns([1, 1, 3])
    save = b1.button("Save shortlist", icon=":material/bookmark:")
    go = b2.button("Save and go to Apply", type="primary", icon=":material/arrow_forward:")
    if save or go:
        selected = edited.loc[edited["Shortlist"], "id"].tolist()
        unselected = edited.loc[~edited["Shortlist"], "id"].tolist()
        store.set_shortlisted(selected, True)
        store.set_shortlisted(unselected, False)
        flash("success", f"{len(selected)} posts shortlisted.")
        if go:
            st.switch_page("views/apply.py")
        st.rerun()

    st.subheader("Post details")
    by_id = {p["_id"]: p for p in posts}
    choice = st.selectbox(
        "Choose a post",
        df["id"].tolist(),
        format_func=lambda pid: (
            f"{(by_id[pid].get('analysis') or {}).get('company') or by_id[pid].get('author_name', '')} — "
            f"{(by_id[pid].get('analysis') or {}).get('role') or 'not scored'}"
        ),
    )
    post = by_id[choice]
    a = post.get("analysis") or {}
    left, right = st.columns(2)
    with left:
        st.markdown(f"**Posted by** [{post.get('author_name', '')}]({post.get('author_url', '')}) · {post.get('posted_ago', '')}")
        if post.get("author_headline"):
            st.caption(post["author_headline"])
        with st.container(height=380):
            st.text(post.get("text", ""))
    with right:
        if a:
            st.metric("Match score", f"{a.get('match_score', 0)}%")
            st.markdown(f"**Experience asked:** {a.get('experience_required', '')}  \n**Salary:** {a.get('salary', '')}")
            st.markdown(f"**Why it fits:** {a.get('match_reasons', '')}")
            if a.get("gaps"):
                st.markdown(f"**Gaps:** {a['gaps']}")
            with st.expander("Job description (extracted)"):
                st.write(a.get("job_description", ""))
        else:
            st.info("Not scored yet.")
