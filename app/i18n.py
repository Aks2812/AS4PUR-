"""
Landing-page translations (2026-09-16), server-side rendered - never
client-side JS string-swapping. Scope, per explicit operator instruction:
landing page CONTENT ONLY (hero, features, Challenge section, Security
section). The shared brand panel (icon/wordmark/tagline) and footer are
NOT covered here - they stay English always, since they're shared with
login.html, which is out of scope for this feature. feature_1/2/3_title
are deliberately identical across "en"/"id" (the three operation names
stay English everywhere post-login, so switching them here only would be
inconsistent) - encoded as literally the same value in both dicts rather
than a template-level "skip translating this one" exception, per explicit
instruction: simpler, and it can't drift out of sync the way a special
case could.
"""
from __future__ import annotations

LANDING_TRANSLATIONS = {
    "en": {
        "eyebrow": "WELCOME TO AS4PUR",
        "hero_heading": "Secure Access. Automated.",
        "hero_description": "AS4PUR helps you manage private app definitions, user provision, and RTP with automation, security, and efficiency.",
        "feature_1_title": "Private App Definition",
        "feature_1_desc": "Manage and secure your private applications with ease.",
        "feature_2_title": "User Provision",
        "feature_2_desc": "Automate user access and lifecycle management.",
        "feature_3_title": "RTP Creation",
        "feature_3_desc": "Bulk-create Netskope Private Access policies in minutes, not hours - no more manual per-rule work in the Netskope console.",
        "cta_button": "Go to Login",
        "challenge_eyebrow": "THE CHALLENGE",
        "challenge_heading": "Manual Netskope operations don't scale.",
        "challenge_point_1": "Hand-run scripts, with no dry-run safety net before changes go live.",
        "challenge_point_2": "No consistent audit trail across operators or clients.",
        "challenge_point_3": "Hours spent on repetitive, error-prone Excel-driven policy work.",
        "security_eyebrow": "SECURITY BY DESIGN",
        "security_heading": "Every action reviewed before it's live.",
        "security_1_title": "Dry-run review gates",
        "security_1_desc": "Every change is previewed and requires explicit confirmation before anything is written.",
        "security_2_title": "Full audit trail",
        "security_2_desc": "Every action is logged - who, what, when - for complete operational accountability.",
        "security_3_title": "Role-based access",
        "security_3_desc": "Admin-gated invites and permissions ensure only authorized team members can act.",
    },
    "id": {
        "eyebrow": "SELAMAT DATANG DI AS4PUR",
        "hero_heading": "Akses Aman. Otomatis.",
        "hero_description": "AS4PUR membantu Anda mengelola definisi aplikasi privat, provisi pengguna, dan RTP dengan otomatisasi, keamanan, dan efisiensi.",
        "feature_1_title": "Private App Definition",
        "feature_1_desc": "Kelola dan amankan aplikasi privat Anda dengan mudah.",
        "feature_2_title": "User Provision",
        "feature_2_desc": "Otomatisasi akses pengguna dan manajemen siklus hidup.",
        "feature_3_title": "RTP Creation",
        "feature_3_desc": "Buat kebijakan Netskope Private Access secara massal dalam hitungan menit, bukan jam - tidak perlu lagi kerja manual per-aturan di konsol Netskope.",
        "cta_button": "Lanjut ke Login",
        "challenge_eyebrow": "TANTANGANNYA",
        "challenge_heading": "Operasi Netskope manual tidak scalable.",
        "challenge_point_1": "Skrip yang dijalankan manual, tanpa jaring pengaman dry-run sebelum perubahan diterapkan.",
        "challenge_point_2": "Tidak ada jejak audit yang konsisten di semua operator atau klien.",
        "challenge_point_3": "Berjam-jam dihabiskan untuk pekerjaan kebijakan berbasis Excel yang repetitif dan rawan kesalahan.",
        "security_eyebrow": "KEAMANAN SEJAK AWAL",
        "security_heading": "Setiap tindakan ditinjau sebelum diterapkan.",
        "security_1_title": "Gerbang tinjauan dry-run",
        "security_1_desc": "Setiap perubahan ditampilkan terlebih dahulu dan memerlukan konfirmasi eksplisit sebelum apa pun disimpan.",
        "security_2_title": "Jejak audit lengkap",
        "security_2_desc": "Setiap tindakan dicatat - siapa, apa, kapan - demi akuntabilitas operasional yang lengkap.",
        "security_3_title": "Akses berbasis peran",
        "security_3_desc": "Undangan dan izin yang dikontrol admin memastikan hanya anggota tim resmi yang dapat bertindak.",
    },
}

LANG_COOKIE_NAME = "as4pur_lang"
DEFAULT_LANG = "en"


def resolve_lang(query_lang: str | None, cookie_lang: str | None) -> str:
    """Query param wins when it's a recognized value (this is what an
    actual click on the header <select> sends); otherwise fall back to
    the cookie; otherwise the default. An unrecognized value in either
    source (a stale cookie from some future removed language, a hand-
    edited URL) is treated as "not provided" rather than raising -
    fails closed to English, never a 500."""
    if query_lang in LANDING_TRANSLATIONS:
        return query_lang
    if cookie_lang in LANDING_TRANSLATIONS:
        return cookie_lang
    return DEFAULT_LANG
