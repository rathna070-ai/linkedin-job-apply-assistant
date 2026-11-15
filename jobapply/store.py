"""MongoDB storage: posts, applications (audit trail), events, resumes and settings."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from bson import Binary, ObjectId
from pymongo import ASCENDING, DESCENDING, MongoClient

DEFAULT_SETTINGS: dict[str, Any] = {
    "hashtags": "#hiring",
    "keywords": "delivery manager\nprogram manager\ntest manager",
    "location": "Bengaluru",
    "posted_limit": "week",
    "max_posts": 50,
    "require_hashtag": True,
    "only_with_email": True,
    "match_threshold": 85,
    "default_mode": "latest",
    "full_name": "",
    "email": "",
    "phone": "",
    "linkedin": "",
    "timezone": "Asia/Kolkata",
    "last_search_id": None,
}

# Response statuses an application can have in the audit trail.
RESPONSE_STATUSES = [
    "no_response",
    "replied",
    "interview",
    "request_info",
    "rejected",
    "auto_reply",
    "other",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_object_id(value: str | ObjectId) -> ObjectId:
    return value if isinstance(value, ObjectId) else ObjectId(str(value))


class Store:
    def __init__(self, uri: str | None = None, db_name: str = "job_apply_assistant", client: Any = None):
        if client is None:
            if not uri:
                raise ValueError("MONGODB_URI is not set.")
            client = MongoClient(uri, serverSelectionTimeoutMS=15000, tz_aware=True)
        self.client = client
        self.db = client[db_name]
        self.posts = self.db["posts"]
        self.applications = self.db["applications"]
        self.events = self.db["events"]
        self.resumes = self.db["resumes"]
        self.settings_col = self.db["settings"]
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        self.posts.create_index([("last_seen_at", DESCENDING)])
        self.posts.create_index([("shortlisted", ASCENDING)])
        self.applications.create_index([("sent_at", DESCENDING)])
        self.applications.create_index([("recipient", ASCENDING)])
        self.events.create_index([("ts", DESCENDING)])
        self.events.create_index([("application_id", ASCENDING)])

    def ping(self) -> bool:
        self.client.admin.command("ping")
        return True

    # ----- settings -----
    def get_settings(self) -> dict[str, Any]:
        doc = self.settings_col.find_one({"_id": "app"}) or {}
        merged = dict(DEFAULT_SETTINGS)
        merged.update({k: v for k, v in doc.items() if k != "_id"})
        return merged

    def save_settings(self, **values: Any) -> None:
        if values:
            self.settings_col.update_one({"_id": "app"}, {"$set": values}, upsert=True)

    # ----- events (audit log) -----
    def log_event(
        self,
        event_type: str,
        *,
        application_id: ObjectId | str | None = None,
        post_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.events.insert_one(
            {
                "ts": utcnow(),
                "type": event_type,
                "application_id": to_object_id(application_id) if application_id else None,
                "post_id": post_id,
                "details": details or {},
            }
        )

    def list_events(self, application_id: ObjectId | str | None = None, limit: int = 200) -> list[dict]:
        query: dict[str, Any] = {}
        if application_id:
            query["application_id"] = to_object_id(application_id)
        return list(self.events.find(query).sort([("ts", DESCENDING), ("_id", DESCENDING)]).limit(limit))

    # ----- posts -----
    def upsert_posts(self, posts: Iterable[dict[str, Any]], search_id: str) -> int:
        """Saves scraped posts. Returns how many were new."""
        now = utcnow()
        posts = list(posts)
        ids = [p["post_id"] for p in posts]
        if not ids:
            return 0
        existing = {d["_id"] for d in self.posts.find({"_id": {"$in": ids}}, {"_id": 1})}
        for post in posts:
            fields = {k: v for k, v in post.items() if k != "post_id"}
            fields["last_seen_at"] = now
            self.posts.update_one(
                {"_id": post["post_id"]},
                {
                    "$set": fields,
                    "$setOnInsert": {"first_seen_at": now, "shortlisted": False, "applied": False},
                    "$addToSet": {"search_ids": search_id},
                },
                upsert=True,
            )
        return len(set(ids) - existing)

    def get_post(self, post_id: str) -> dict | None:
        return self.posts.find_one({"_id": post_id})

    def list_posts(self, query: dict[str, Any] | None = None, limit: int = 1000) -> list[dict]:
        return list(self.posts.find(query or {}).sort("last_seen_at", DESCENDING).limit(limit))

    def save_analysis(self, post_id: str, analysis: dict[str, Any], model: str) -> None:
        self.posts.update_one(
            {"_id": post_id},
            {"$set": {"analysis": analysis, "analyzed_at": utcnow(), "analysis_model": model}},
        )

    def set_shortlisted(self, post_ids: Iterable[str], value: bool) -> None:
        ids = list(post_ids)
        if ids:
            self.posts.update_many({"_id": {"$in": ids}}, {"$set": {"shortlisted": value}})

    def update_draft(self, post_id: str, **fields: Any) -> None:
        if fields:
            self.posts.update_one(
                {"_id": post_id},
                {"$set": {f"draft.{k}": v for k, v in fields.items()} | {"draft.updated_at": utcnow()}},
            )

    def mark_applied(self, post_id: str, application_id: ObjectId) -> None:
        self.posts.update_one(
            {"_id": post_id},
            {"$set": {"applied": True, "application_id": application_id, "shortlisted": False}},
        )

    # ----- resumes -----
    def save_latest_resume(
        self,
        *,
        pdf_name: str,
        pdf_bytes: bytes,
        text: str,
        docx_name: str | None = None,
        docx_bytes: bytes | None = None,
    ) -> None:
        doc = {
            "kind": "latest",
            "pdf_name": pdf_name,
            "pdf": Binary(pdf_bytes),
            "docx_name": docx_name,
            "docx": Binary(docx_bytes) if docx_bytes else None,
            "text": text,
            "uploaded_at": utcnow(),
        }
        self.resumes.replace_one({"_id": "latest"}, doc, upsert=True)

    def update_latest_resume_text(self, text: str) -> None:
        self.resumes.update_one({"_id": "latest"}, {"$set": {"text": text}})

    def get_latest_resume(self) -> dict | None:
        return self.resumes.find_one({"_id": "latest"})

    def save_tailored_resume(
        self,
        *,
        post_id: str,
        base_name: str,
        docx_bytes: bytes,
        pdf_bytes: bytes,
        data: dict[str, Any],
        model: str,
    ) -> ObjectId:
        result = self.resumes.insert_one(
            {
                "kind": "tailored",
                "post_id": post_id,
                "docx_name": f"{base_name}.docx",
                "docx": Binary(docx_bytes),
                "pdf_name": f"{base_name}.pdf",
                "pdf": Binary(pdf_bytes),
                "data": data,
                "model": model,
                "created_at": utcnow(),
            }
        )
        return result.inserted_id

    def get_resume(self, resume_id: ObjectId | str) -> dict | None:
        if resume_id == "latest":
            return self.get_latest_resume()
        return self.resumes.find_one({"_id": to_object_id(resume_id)})

    # ----- applications (audit trail) -----
    def create_application(self, doc: dict[str, Any]) -> ObjectId:
        doc = dict(doc)
        doc.setdefault("created_at", utcnow())
        doc.setdefault("response_status", "no_response")
        return self.applications.insert_one(doc).inserted_id

    def update_application(self, application_id: ObjectId | str, **fields: Any) -> None:
        if fields:
            self.applications.update_one({"_id": to_object_id(application_id)}, {"$set": fields})

    def get_application(self, application_id: ObjectId | str) -> dict | None:
        return self.applications.find_one({"_id": to_object_id(application_id)})

    def list_applications(self, query: dict[str, Any] | None = None) -> list[dict]:
        return list(self.applications.find(query or {}).sort("created_at", DESCENDING))

    def applications_to(self, recipient: str) -> list[dict]:
        if not recipient:
            return []
        pattern = {"$regex": f"^{_escape_regex(recipient.strip())}$", "$options": "i"}
        return list(self.applications.find({"recipient": pattern, "status": "sent"}))


def _escape_regex(text: str) -> str:
    import re

    return re.escape(text)
