from jobapply.scraper import (
    build_queries,
    dedupe_posts,
    extract_emails,
    has_any_hashtag,
    normalize_post,
    parse_hashtags,
    scrape_posts,
)


def test_parse_hashtags_normalizes_and_dedupes():
    assert parse_hashtags("#Hiring, deliverymanager #hiring  ##QA") == ["#hiring", "#deliverymanager", "#qa"]


def test_build_queries_one_per_keyword_line():
    queries = build_queries("#hiring", "delivery manager\n\nprogram manager\n", "Bengaluru")
    assert queries == ["#hiring delivery manager Bengaluru", "#hiring program manager Bengaluru"]


def test_build_queries_hashtags_only():
    assert build_queries("#hiring #qa", "", "") == ["#hiring #qa"]


def test_extract_emails_dedupes_and_trims_punctuation():
    text = "Send CV to Sarath.Kumar@izmoltd.com. Or sarath.kumar@izmoltd.com, or hr@company.co.in!"
    assert extract_emails(text) == ["Sarath.Kumar@izmoltd.com", "hr@company.co.in"]


def test_has_any_hashtag_matches_linkedin_hashtag_rendering():
    assert has_any_hashtag("We are hashtag#Hiring now", ["#hiring"])
    assert not has_any_hashtag("We are hiring now", ["#hiring"])


def test_normalize_post_maps_fields_and_merges_repost():
    item = {
        "id": "7527323497388236800",
        "linkedinUrl": "https://www.linkedin.com/posts/x",
        "content": "#hiring Program Manager. Mail jobs@acme.com",
        "repost": {"content": "Original text"},
        "author": {"name": "Sarath", "info": "Recruiter", "linkedinUrl": "https://linkedin.com/in/s"},
        "postedAt": {"date": "2026-11-14T10:00:00Z", "postedAgoText": "1 day ago"},
        "query": {"search": "#hiring program manager"},
    }
    post = normalize_post(item)
    assert post["post_id"] == "7527323497388236800"
    assert post["author_name"] == "Sarath"
    assert post["emails"] == ["jobs@acme.com"]
    assert "[Reposted]" in post["text"] and "Original text" in post["text"]
    assert post["query"] == "#hiring program manager"


def test_normalize_post_skips_empty():
    assert normalize_post({"id": "1", "content": "  "}) is None


def test_dedupe_posts():
    posts = [{"post_id": "a"}, {"post_id": "b"}, {"post_id": "a"}]
    assert [p["post_id"] for p in dedupe_posts(posts)] == ["a", "b"]


class _FakeRun:
    status = "SUCCEEDED"
    default_dataset_id = "ds1"


class _FakeActor:
    def __init__(self, calls):
        self.calls = calls

    def call(self, run_input, wait_duration):
        self.calls.append(run_input)
        return _FakeRun()


class _FakeDataset:
    def __init__(self, items):
        self.items = items

    def iterate_items(self):
        yield from self.items


class FakeApify:
    def __init__(self, items=None):
        self.calls = []
        self.items = items if items is not None else [{"id": "1", "content": "#hiring x"}]

    def actor(self, actor_id):
        assert actor_id == "harvestapi/linkedin-post-search"
        return _FakeActor(self.calls)

    def dataset(self, dataset_id):
        assert dataset_id == "ds1"
        return _FakeDataset(self.items)


def test_scrape_posts_builds_actor_input():
    fake = FakeApify()
    items = scrape_posts("token", ["#hiring qa"], max_posts=25, posted_limit="24h", client=fake)
    assert items == [{"id": "1", "content": "#hiring x"}]
    assert fake.calls == [{"searchQueries": ["#hiring qa"], "maxPosts": 25, "postedLimit": "24h", "sortBy": "date"}]
