"""Official-page fact extraction.

Guideline §10 makes the **official product website** the authoritative source
for every factual field. Discovery gives us a *pointer*; verification proves
the page is real and is the product it claims to be; this module is the step
that actually *reads the facts off that page*.

Why this module exists
----------------------
Before it, the pipeline could prove a product existed but not say anything
about it: :class:`~src.verification.verifier.OfficialSiteVerifier` accepted
injected ``extractors`` and nothing was ever injected, so every factual field
stayed blank and every record scored like an empty shell. This module is that
missing extractor.

Hard rules encoded here
-----------------------
* **Pure and offline.** :func:`extract_official_facts` takes markup + a URL and
  returns a record. No network, no clock, no global state — so every fact is
  reproducible and unit-testable from a fixture.
* **Nothing is fabricated.** A field is populated only when the page itself
  carries the evidence. There are no defaults, no "probably subscription", no
  "assume web". An absent fact stays ``None``/``[]`` forever.
* **Every fact carries its own evidence string.** :attr:`OfficialFacts.evidence`
  maps ``field -> what on the page justified it``, so a reviewer can audit any
  value without re-fetching the page.
* **Directory claims never enter here.** The only inputs are the official
  page's own markup and (optionally) additional *same-site* official pages.
* **Marketing copy is not a capability.** Capability/IO/integration signals
  require distinctive phrases or real same-site affordances, never a single
  fuzzy keyword.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from src.core.http_client import FetchResult, HttpClient
from src.core.logging_setup import get_logger
from src.core.text import clean_text
from src.core.urls import extract_registrable_domain, normalize_url, same_site
from src.extraction.html import absolutize, looks_like_challenge, node_text, parse_html
from src.models.enums import (
    AICapability,
    IOFormat,
    OpenSourceStatus,
    Platform,
    PricingModel,
    SignupRequirement,
)

logger = get_logger("extraction.official_page")

__all__ = [
    "OfficialFacts",
    "extract_official_facts",
    "OfficialFactsExtractor",
    "MAX_FEATURES",
    "MAX_USE_CASES",
]

#: Caps keep a record readable and stop a link-farm page from flooding a field.
MAX_FEATURES = 12
MAX_USE_CASES = 10
MAX_INTEGRATIONS = 20
MAX_LIMITATIONS = 8

#: Minimum/maximum length for a string to count as a feature/use-case bullet.
_MIN_BULLET = 8
_MAX_BULLET = 160

# --------------------------------------------------------------- section cues
#: Heading text that introduces a *feature* list.
_FEATURE_HEADINGS = (
    "features", "key features", "core features", "what you can do",
    "capabilities", "what it does", "how it works", "why choose",
    "everything you need", "powerful features", "main features",
)

#: Heading text that introduces a *use-case* list.
_USE_CASE_HEADINGS = (
    "use cases", "use case", "who is it for", "who it's for", "who it is for",
    "perfect for", "built for", "made for", "ideal for", "designed for",
    "what can you build", "who uses",
)

#: Heading text that introduces explicitly stated limitations.
_LIMITATION_HEADINGS = (
    "limitations", "limits", "known limitations", "restrictions",
    "what it can't do", "what it cannot do", "not supported",
)

#: Heading text that introduces an integration list.
_INTEGRATION_HEADINGS = (
    "integrations", "integrates with", "works with", "connect with",
    "connections", "supported platforms", "available on",
)

# ------------------------------------------------------------- AI capabilities
#: ``capability -> distinctive phrases``. Each phrase is specific enough that a
#: page carrying it really is claiming that capability. Single vague words
#: ("ai", "smart", "generate") are deliberately absent.
_CAPABILITY_PHRASES: tuple[tuple[AICapability, tuple[str, ...]], ...] = (
    (AICapability.TEXT_GENERATION, (
        "generate text", "text generation", "write copy", "ai writer",
        "content generation", "generate content", "ai writing assistant",
        "write articles", "generate articles", "copywriting",
    )),
    (AICapability.TEXT_SUMMARIZATION, (
        "summarize", "summarise", "summarization", "summarisation",
        "tl;dr", "key takeaways", "auto-summary",
    )),
    (AICapability.TRANSLATION, (
        "translate", "translation", "multilingual translation", "localize content",
    )),
    (AICapability.QUESTION_ANSWERING, (
        "ask questions about", "question answering", "answer questions from",
        "chat with your documents", "chat with your pdf", "chat with your data",
    )),
    (AICapability.CONVERSATIONAL_AI, (
        "chatbot", "conversational ai", "ai chat", "chat assistant",
        "ai assistant", "virtual assistant", "talk to", "ai companion",
    )),
    (AICapability.RAG, (
        "retrieval augmented", "retrieval-augmented", "rag pipeline",
        "knowledge base search", "your own data", "vector search",
        "semantic search over",
    )),
    (AICapability.CODE_GENERATION, (
        "generate code", "code generation", "write code", "ai code",
        "code completion", "autocomplete code", "code assistant",
    )),
    (AICapability.CODE_REVIEW, (
        "code review", "review your code", "review pull request",
        "detect bugs in your code",
    )),
    (AICapability.IMAGE_GENERATION, (
        "generate images", "image generation", "text to image", "text-to-image",
        "ai image generator", "create images from", "ai art generator",
    )),
    (AICapability.IMAGE_EDITING, (
        "edit images", "image editing", "remove background", "background remover",
        "upscale images", "image upscaler", "retouch", "inpainting", "outpainting",
        "object removal",
    )),
    (AICapability.IMAGE_RECOGNITION, (
        "image recognition", "object detection", "detect objects",
        "visual recognition", "face detection", "image classification",
    )),
    (AICapability.OCR, (
        "ocr", "optical character recognition", "extract text from images",
        "scanned document", "extract text from pdf",
    )),
    (AICapability.VIDEO_GENERATION, (
        "generate videos", "video generation", "text to video", "text-to-video",
        "ai video generator", "create videos from",
    )),
    (AICapability.VIDEO_EDITING, (
        "video editing", "edit videos", "auto-edit", "video editor",
        "remove silences", "add subtitles to",
    )),
    (AICapability.SPEECH_TO_TEXT, (
        "speech to text", "speech-to-text", "transcribe", "transcription",
        "voice to text", "audio to text",
    )),
    (AICapability.TEXT_TO_SPEECH, (
        "text to speech", "text-to-speech", "tts", "read aloud",
        "ai voiceover", "voice over generator", "natural sounding voices",
        "natural-sounding voices",
    )),
    (AICapability.VOICE_CLONING, (
        "voice cloning", "clone your voice", "voice clone", "custom voice",
    )),
    (AICapability.MUSIC_GENERATION, (
        "generate music", "music generation", "ai music", "royalty-free music",
        "create songs",
    )),
    (AICapability.RECOMMENDATION, (
        "recommendation engine", "personalized recommendations",
        "recommend products",
    )),
    (AICapability.FORECASTING, (
        "forecasting", "forecast demand", "predictive analytics",
        "predict future",
    )),
    (AICapability.DATA_ANALYSIS, (
        "data analysis", "analyze your data", "analyse your data",
        "business intelligence", "ai analytics", "insights from your data",
        "chat with your spreadsheet",
    )),
    (AICapability.WEB_SEARCH, (
        "ai search engine", "web search", "search the web", "search engine",
    )),
    (AICapability.WEB_BROWSING, (
        "browse the web", "web browsing", "browser agent", "browse websites",
    )),
    (AICapability.AGENTIC_WORKFLOW, (
        "ai agent", "ai agents", "autonomous agent", "agentic",
        "multi-agent", "agent workflow", "agents that",
    )),
    (AICapability.FUNCTION_CALLING, (
        "function calling", "tool calling", "tool use",
    )),
    (AICapability.MULTIMODAL, (
        "multimodal", "multi-modal", "text, image and audio",
        "text, image, and audio",
    )),
    (AICapability.FINE_TUNING, (
        "fine-tune", "fine tuning", "fine-tuning", "train your own model",
        "custom model training",
    )),
    (AICapability.EMBEDDINGS, (
        "embeddings", "embedding model", "vector embeddings",
    )),
    (AICapability.THREE_D_GENERATION, (
        "3d generation", "text to 3d", "generate 3d models", "3d model generator",
    )),
    (AICapability.ANOMALY_DETECTION, (
        "anomaly detection", "detect anomalies", "fraud detection",
    )),
    (AICapability.SENTIMENT_ANALYSIS, (
        "sentiment analysis", "analyze sentiment", "analyse sentiment",
    )),
    (AICapability.TEXT_CLASSIFICATION, (
        "text classification", "classify text", "categorize documents",
        "auto-tagging",
    )),
)

# ------------------------------------------------------------------ IO formats
#: ``format -> phrases``. Input and output tables are separate on purpose: a
#: page saying "upload a PDF" proves a PDF *input*, never a PDF output.
_INPUT_PHRASES: tuple[tuple[IOFormat, tuple[str, ...]], ...] = (
    (IOFormat.PROMPT, ("from a prompt", "enter a prompt", "your prompt",
                       "type a prompt", "describe what you want",
                       "text prompt")),
    (IOFormat.TEXT, ("paste your text", "enter text", "paste text",
                     "type your text", "input text", "paste in your")),
    (IOFormat.PDF, ("upload a pdf", "upload pdf", "upload your pdf",
                    "pdf upload", "drop a pdf", "from pdf", "pdf files")),
    (IOFormat.DOCX, ("upload a docx", "word document", ".docx", "doc, docx")),
    (IOFormat.IMAGE, ("upload an image", "upload image", "upload your image",
                      "drop an image", "upload a photo", "upload your photo",
                      "from an image", "png, jpg", "jpg, png")),
    (IOFormat.AUDIO, ("upload audio", "upload an audio", "upload your audio",
                      "audio file", "mp3 file", "record your voice",
                      "audio upload")),
    (IOFormat.VIDEO, ("upload a video", "upload video", "upload your video",
                      "video file", "mp4 file", "drop a video")),
    (IOFormat.URL, ("paste a url", "paste a link", "enter a url", "enter a link",
                    "paste any link", "from a url", "youtube link")),
    (IOFormat.CSV, ("upload a csv", "csv file", "upload csv", ".csv")),
    (IOFormat.XLSX, ("upload an excel", "excel file", "spreadsheet upload",
                     ".xlsx")),
    (IOFormat.CODE, ("paste your code", "your codebase", "your repository",
                     "source code")),
)

_OUTPUT_PHRASES: tuple[tuple[IOFormat, tuple[str, ...]], ...] = (
    (IOFormat.TEXT, ("generated text", "get text", "text output",
                     "written content", "generated copy", "generated article")),
    (IOFormat.MARKDOWN, ("markdown output", "export to markdown",
                         "as markdown", "in markdown")),
    (IOFormat.IMAGE, ("generated images", "generated image", "download the image",
                      "image output", "hd images", "images in seconds",
                      "download your image")),
    (IOFormat.EDITED_IMAGE, ("edited image", "retouched image",
                             "background removed")),
    (IOFormat.VIDEO, ("generated video", "generated videos", "video output",
                      "download the video", "export your video",
                      "download your video")),
    (IOFormat.EDITED_VIDEO, ("edited video", "final cut", "exported video")),
    (IOFormat.AUDIO, ("generated audio", "audio output", "download the audio",
                      "download mp3", "audio file output")),
    (IOFormat.TRANSCRIPT, ("transcript", "transcripts", "transcription output",
                           "accurate transcript")),
    (IOFormat.SUBTITLES, ("subtitles", "captions", "srt file", ".srt", "vtt")),
    (IOFormat.SUMMARY, ("summary", "summaries", "tl;dr", "key takeaways")),
    (IOFormat.CODE, ("generated code", "code output", "working code",
                     "production-ready code")),
    (IOFormat.PDF, ("export to pdf", "download as pdf", "pdf report",
                    "export as pdf")),
    (IOFormat.CSV, ("export to csv", "download as csv", "export as csv")),
    (IOFormat.JSON, ("json output", "json response", "structured json",
                     "returns json")),
    (IOFormat.SPREADSHEET, ("export to excel", "download as excel",
                            "spreadsheet output")),
    (IOFormat.SLIDE_DECK, ("presentation", "slide deck", "slides",
                           "powerpoint")),
    (IOFormat.STRUCTURED_REPORT, ("structured report", "detailed report",
                                  "generate a report")),
    (IOFormat.EXTRACTED_DATA, ("extracted data", "structured data",
                               "extract data")),
    (IOFormat.TRANSLATION, ("translated text", "translations")),
    (IOFormat.VOICE_CLONE, ("cloned voice", "your cloned voice")),
    (IOFormat.GLB, ("glb file", ".glb", "3d model file")),
)

# ------------------------------------------------------------------ platforms
#: Platforms proven by a *link host* (hard evidence: the store page exists).
_PLATFORM_LINK_HOSTS: tuple[tuple[Platform, tuple[str, ...]], ...] = (
    (Platform.CHROME_EXTENSION, ("chromewebstore.google.com", "chrome.google.com/webstore")),
    (Platform.FIREFOX_EXTENSION, ("addons.mozilla.org",)),
    (Platform.EDGE_EXTENSION, ("microsoftedge.microsoft.com",)),
    (Platform.IOS, ("apps.apple.com", "itunes.apple.com")),
    (Platform.ANDROID, ("play.google.com",)),
    (Platform.VSCODE_EXTENSION, ("marketplace.visualstudio.com",)),
    (Platform.JETBRAINS_PLUGIN, ("plugins.jetbrains.com",)),
    (Platform.FIGMA_PLUGIN, ("figma.com/community/plugin",)),
    (Platform.SLACK, ("slack.com/apps", "slack.com/marketplace")),
    (Platform.WORDPRESS_PLUGIN, ("wordpress.org/plugins",)),
    (Platform.SHOPIFY_APP, ("apps.shopify.com",)),
    (Platform.GOOGLE_WORKSPACE, ("workspace.google.com/marketplace",)),
)

#: Platforms proven by explicit download/availability wording.
_PLATFORM_PHRASES: tuple[tuple[Platform, tuple[str, ...]], ...] = (
    (Platform.WINDOWS, ("download for windows", "windows app", "available on windows",
                        "for windows 10", "for windows 11", ".exe")),
    (Platform.MACOS, ("download for mac", "mac app", "available on macos",
                      "for macos", "download for macos", "apple silicon",
                      ".dmg")),
    (Platform.LINUX, ("download for linux", "linux app", "available on linux",
                      "appimage", ".deb")),
    (Platform.IOS, ("download on the app store", "ios app", "iphone app",
                    "available on ios")),
    (Platform.ANDROID, ("get it on google play", "android app",
                        "available on android")),
    (Platform.CLI, ("command line", "command-line", "cli tool", "npm install -g",
                    "brew install", "pip install")),
    (Platform.SDK, ("python sdk", "javascript sdk", "node sdk", "typescript sdk",
                    "our sdk", "sdks for")),
    (Platform.SELF_HOSTED, ("self-host", "self host", "self-hosted",
                            "docker compose", "docker run", "helm chart")),
    (Platform.ON_PREMISE, ("on-premise", "on premise", "on-prem deployment")),
    (Platform.DISCORD, ("discord bot", "add to discord", "our discord bot")),
    (Platform.TELEGRAM, ("telegram bot",)),
    (Platform.WHATSAPP, ("whatsapp bot", "on whatsapp")),
)

# ---------------------------------------------------------------- integrations
#: Third-party products we will credit as an integration. A curated list is
#: the point: it stops "Google" in a footer cookie notice from becoming an
#: integration, and it keeps the field comparable across records.
_INTEGRATION_CATALOG: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Slack", ("slack",)),
    ("Notion", ("notion",)),
    ("Zapier", ("zapier",)),
    ("Make", ("make.com",)),
    ("Google Drive", ("google drive",)),
    ("Google Docs", ("google docs",)),
    ("Google Sheets", ("google sheets",)),
    ("Google Calendar", ("google calendar",)),
    ("Gmail", ("gmail",)),
    ("Microsoft Teams", ("microsoft teams", "ms teams")),
    ("Microsoft Word", ("microsoft word",)),
    ("Microsoft Excel", ("microsoft excel",)),
    ("Outlook", ("outlook",)),
    ("Salesforce", ("salesforce",)),
    ("HubSpot", ("hubspot",)),
    ("Shopify", ("shopify",)),
    ("WordPress", ("wordpress",)),
    ("Webflow", ("webflow",)),
    ("Figma", ("figma",)),
    ("Canva", ("canva",)),
    ("Adobe Photoshop", ("photoshop",)),
    ("Adobe Premiere Pro", ("premiere pro",)),
    ("GitHub", ("github",)),
    ("GitLab", ("gitlab",)),
    ("Bitbucket", ("bitbucket",)),
    ("Jira", ("jira",)),
    ("Linear", ("linear.app",)),
    ("Asana", ("asana",)),
    ("Trello", ("trello",)),
    ("ClickUp", ("clickup",)),
    ("Airtable", ("airtable",)),
    ("Discord", ("discord",)),
    ("Zoom", ("zoom",)),
    ("Google Meet", ("google meet",)),
    ("Stripe", ("stripe",)),
    ("Zendesk", ("zendesk",)),
    ("Intercom", ("intercom",)),
    ("Dropbox", ("dropbox",)),
    ("OneDrive", ("onedrive",)),
    ("Confluence", ("confluence",)),
    ("Snowflake", ("snowflake",)),
    ("BigQuery", ("bigquery",)),
    ("PostgreSQL", ("postgresql", "postgres")),
    ("MySQL", ("mysql",)),
    ("MongoDB", ("mongodb",)),
    ("Supabase", ("supabase",)),
    ("LangChain", ("langchain",)),
    ("Zoho", ("zoho",)),
    ("Pipedrive", ("pipedrive",)),
    ("Mailchimp", ("mailchimp",)),
    ("YouTube", ("youtube",)),
    ("TikTok", ("tiktok",)),
    ("LinkedIn", ("linkedin",)),
    ("Instagram", ("instagram",)),
    ("X (Twitter)", ("twitter", "x.com")),
    ("Obsidian", ("obsidian",)),
    ("Chrome", ("chrome extension",)),
    ("VS Code", ("vs code", "vscode", "visual studio code")),
)

#: Social/marketing hosts whose mere presence is *not* an integration. These
#: brands only count when the page frames them as integrations.
_SOCIAL_ONLY = frozenset(
    {"YouTube", "TikTok", "LinkedIn", "Instagram", "X (Twitter)", "Discord"}
)

# ------------------------------------------------------------------- pricing
_PRICE_RE = re.compile(
    r"(?P<sym>[$€£]|USD|EUR|GBP)\s?(?P<amount>\d{1,4}(?:[.,]\d{1,2})?)"
    r"(?P<per>\s*(?:/|per\s+)\s*(?:mo\b|month|mon\b|year|yr\b|annual|user|seat|"
    r"credit|image|video|minute|min\b|hour|1k|1m|million|request|token|word))?",
    re.IGNORECASE,
)

_CURRENCY_BY_SYMBOL = {"$": "USD", "€": "EUR", "£": "GBP",
                       "usd": "USD", "eur": "EUR", "gbp": "GBP"}

_PERIOD_WORDS: tuple[tuple[str, str], ...] = (
    ("month", "month"), ("/mo", "month"), ("mon", "month"),
    ("year", "year"), ("/yr", "year"), ("yr", "year"), ("annual", "year"),
    ("user", "seat"), ("seat", "seat"),
    ("credit", "credit"), ("image", "unit"), ("video", "unit"),
    ("minute", "unit"), ("min", "unit"), ("hour", "unit"),
    ("1k", "usage"), ("1m", "usage"), ("million", "usage"),
    ("request", "usage"), ("token", "usage"), ("word", "usage"),
)

_FREE_PLAN_PHRASES = (
    "free plan", "free tier", "free forever", "forever free", "start for free",
    "get started for free", "try for free", "free to use", "free version",
    "no credit card required", "free account", "100% free", "completely free",
    "$0/mo", "$0 / mo", "$0/month", "$0 per month",
)

_FREE_TRIAL_RE = re.compile(
    r"(?:(?P<days>\d{1,3})[-\s]day[s]?\s+(?:free\s+)?trial"
    r"|free\s+trial(?:\s+for\s+(?P<days2>\d{1,3})\s+days)?)",
    re.IGNORECASE,
)

_PRICING_MODEL_PHRASES: tuple[tuple[PricingModel, tuple[str, ...]], ...] = (
    (PricingModel.ONE_TIME, ("one-time payment", "one time payment",
                             "lifetime deal", "lifetime access",
                             "pay once", "buy it once")),
    (PricingModel.CREDITS, ("credit pack", "credits pack", "buy credits",
                            "credit-based", "credits per month", "top up credits")),
    (PricingModel.USAGE_BASED, ("pay as you go", "pay-as-you-go",
                                "usage-based pricing", "per 1k tokens",
                                "per 1m tokens", "per request", "metered billing",
                                "per minute of", "per image generated")),
    (PricingModel.CONTACT_SALES, ("contact sales", "contact us for pricing",
                                  "talk to sales", "request a quote",
                                  "custom pricing", "get a quote")),
    (PricingModel.ENTERPRISE, ("enterprise plan", "enterprise pricing",
                               "for enterprises")),
    (PricingModel.SUBSCRIPTION, ("/month", "per month", "/mo", "monthly plan",
                                 "billed annually", "billed monthly",
                                 "subscription", "/year", "per year")),
)

_OPEN_SOURCE_PHRASES = (
    "open source", "open-source", "mit license", "mit licence",
    "apache 2.0", "apache-2.0", "agpl", "gpl-3", "gplv3", "bsd license",
    "mozilla public license", "source code is available",
)

_SOURCE_AVAILABLE_PHRASES = (
    "source available", "business source license", "bsl 1.1",
    "fair-code", "elastic license",
)

_NO_SIGNUP_PHRASES = (
    "no sign up", "no sign-up", "no signup", "no account needed",
    "no account required", "no registration", "without an account",
    "no login required", "no sign up required", "no signup required",
)

_WAITLIST_PHRASES = (
    "join the waitlist", "join our waitlist", "request early access",
    "request access", "get on the waitlist", "sign up for the waitlist",
)

_INVITE_PHRASES = ("invite only", "invite-only", "by invitation")

_VERSION_RE = re.compile(
    r"\b(?:version|v)\s?(?P<ver>\d+(?:\.\d+){0,2})\b", re.IGNORECASE
)

_LAUNCH_RE = re.compile(
    r"\b(?:launched|released|founded|established|since|shipped)\s+"
    r"(?:in\s+|on\s+)?(?P<value>"
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2}?,?\s?\d{4}|\d{4})\b",
    re.IGNORECASE,
)

_USER_COUNT_RE = re.compile(
    r"\b(?P<count>\d{1,3}(?:[.,]\d{3})*(?:\s?[kKmM]\+?)?\+?)\s?"
    r"(?:\+\s?)?(?:happy\s+|active\s+|monthly\s+|registered\s+|paying\s+)?"
    r"(?:users|customers|creators|developers|teams|businesses|companies|"
    r"professionals|marketers|students|subscribers)\b"
)

_COPYRIGHT_RE = re.compile(
    r"(?:©|\(c\)|copyright)\s*(?:copyright\s*)?(?:\d{4}\s*[-–—]?\s*\d{0,4}\s*)?"
    r"(?P<name>[A-Z][\w&.,'’\- ]{1,60}?)"
    r"(?=\s*(?:\.|\||·|•|,|all rights|\d{4}|$))",
    re.IGNORECASE,
)

_COMPANY_SUFFIX_RE = re.compile(
    r"\b(?:inc|inc\.|llc|ltd|ltd\.|limited|gmbh|b\.?v\.?|s\.?a\.?s?|ab|oy|"
    r"pty|plc|co\.|corp|corporation|company|technologies|technology|labs|"
    r"software|studio|studios|holdings|s\.?r\.?l\.?|kg|ug|sp\. z o\.o\.)\b",
    re.IGNORECASE,
)

#: Legal/boilerplate words that must never be treated as a company name.
_COMPANY_STOPWORDS = frozenset({
    "all rights reserved", "all rights", "privacy policy", "terms of service",
    "terms", "cookie policy", "reserved", "rights reserved", "the company",
})


# ================================================================== the record
@dataclass
class OfficialFacts:
    """Facts read off one product's **official** page(s), with evidence.

    Every attribute is either ``None``/empty (we could not verify it) or a
    value the page itself supports. :attr:`evidence` explains each populated
    field in one auditable line.
    """

    official_url: str
    pages_read: list[str] = field(default_factory=list)

    company: str | None = None
    product_description: str | None = None
    detailed_overview: str | None = None
    key_features: list[str] = field(default_factory=list)
    use_cases: list[str] = field(default_factory=list)
    ai_capabilities: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    integrations: list[str] = field(default_factory=list)
    has_api: bool | None = None
    api_docs_url: str | None = None
    open_source_status: str | None = None
    repository_url: str | None = None
    signup_requirement: str | None = None

    pricing_model: str | None = None
    starting_price_amount: float | None = None
    starting_price_currency: str | None = None
    starting_price_period: str | None = None
    starting_price_raw: str | None = None
    has_free_plan: bool | None = None
    has_free_trial: bool | None = None
    free_trial_days: int | None = None
    pricing_url: str | None = None

    limitations: list[str] = field(default_factory=list)
    version: str | None = None
    launch_date: date | None = None
    launch_date_precision: str | None = None
    stated_user_count: str | None = None
    logo_url: str | None = None

    #: ``field name -> why this value is on the record``.
    evidence: dict[str, str] = field(default_factory=dict)
    #: Fields we looked for and could *not* verify (kept blank, on purpose).
    unverified_fields: list[str] = field(default_factory=list)

    # ------------------------------------------------------------- predicates
    @property
    def has_any_fact(self) -> bool:
        return bool(self.evidence)

    def note(self, field_name: str, why: str) -> None:
        """Record the justification for ``field_name`` (first one wins)."""
        self.evidence.setdefault(field_name, why)

    def to_dict(self) -> dict[str, Any]:
        return {
            "official_url": self.official_url,
            "pages_read": list(self.pages_read),
            "company": self.company,
            "product_description": self.product_description,
            "detailed_overview": self.detailed_overview,
            "key_features": list(self.key_features),
            "use_cases": list(self.use_cases),
            "ai_capabilities": list(self.ai_capabilities),
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "platforms": list(self.platforms),
            "integrations": list(self.integrations),
            "has_api": self.has_api,
            "api_docs_url": self.api_docs_url,
            "open_source_status": self.open_source_status,
            "repository_url": self.repository_url,
            "signup_requirement": self.signup_requirement,
            "pricing_model": self.pricing_model,
            "starting_price_amount": self.starting_price_amount,
            "starting_price_currency": self.starting_price_currency,
            "starting_price_period": self.starting_price_period,
            "starting_price_raw": self.starting_price_raw,
            "has_free_plan": self.has_free_plan,
            "has_free_trial": self.has_free_trial,
            "free_trial_days": self.free_trial_days,
            "pricing_url": self.pricing_url,
            "limitations": list(self.limitations),
            "version": self.version,
            "launch_date": self.launch_date.isoformat() if self.launch_date else None,
            "launch_date_precision": self.launch_date_precision,
            "stated_user_count": self.stated_user_count,
            "logo_url": self.logo_url,
            "evidence": dict(self.evidence),
            "unverified_fields": list(self.unverified_fields),
        }


# ============================================================ page extraction
@dataclass
class _PageView:
    """One parsed official page, reduced to the surfaces we read facts from."""

    url: str
    soup: Any
    text: str
    lowered: str
    title: str | None
    meta_description: str | None
    headings: list[tuple[str, str]]          # (tag, text)
    bullets: list[str]
    anchors: list[tuple[str, str]]           # (absolute_url, label)
    jsonld: list[Mapping[str, Any]]
    is_pricing_page: bool = False


def _page_view(markup: str | None, url: str, *, is_pricing: bool = False) -> _PageView | None:
    """Parse ``markup`` into a :class:`_PageView`, or ``None`` if unusable.

    A bot challenge is never parsed: its markup describes Cloudflare, not the
    product, so treating it as a page would manufacture false facts.
    """
    if not markup or not markup.strip():
        return None
    if looks_like_challenge(markup):
        return None
    soup = parse_html(markup)
    if soup is None:
        return None

    for tag in ("script", "style", "noscript", "template", "svg"):
        try:
            for node in soup.find_all(tag):
                # JSON-LD is read separately before stripping.
                if tag == "script" and (node.get("type") or "").lower() == "application/ld+json":
                    continue
                node.decompose()
        except Exception:  # noqa: BLE001 - malformed markup must not abort
            continue

    text = clean_text(soup.get_text(" ", strip=True)) or ""
    headings: list[tuple[str, str]] = []
    for tag in ("h1", "h2", "h3", "h4"):
        try:
            nodes = soup.find_all(tag)
        except Exception:  # noqa: BLE001
            nodes = []
        for node in nodes[:40]:
            value = node_text(node, max_length=200)
            if value:
                headings.append((tag, value))

    bullets: list[str] = []
    try:
        items = soup.find_all("li")
    except Exception:  # noqa: BLE001
        items = []
    for node in items[:400]:
        value = node_text(node, max_length=_MAX_BULLET)
        if value:
            bullets.append(value)

    anchors: list[tuple[str, str]] = []
    try:
        links = soup.find_all("a", href=True)
    except Exception:  # noqa: BLE001
        links = []
    for node in links[:600]:
        target = absolutize(node.get("href"), url)
        if not target:
            continue
        label = node_text(node, max_length=120) or ""
        anchors.append((target, label))

    return _PageView(
        url=url,
        soup=soup,
        text=text,
        lowered=text.casefold(),
        title=clean_text(node_text(soup.title), max_length=300),
        meta_description=clean_text(
            _meta(soup, ("description", "og:description", "twitter:description")),
            max_length=600,
        ),
        headings=headings,
        bullets=bullets,
        anchors=anchors,
        jsonld=_jsonld_blocks(soup),
        is_pricing_page=is_pricing,
    )


def _meta(soup: Any, keys: Sequence[str]) -> str | None:
    """First non-empty ``<meta>`` value among ``keys`` (property or name)."""
    for key in keys:
        for attr in ("property", "name"):
            try:
                node = soup.find("meta", attrs={attr: key})
            except Exception:  # noqa: BLE001
                node = None
            if node is None:
                continue
            value = clean_text(node.get("content"))
            if value:
                return value
    return None


def _jsonld_blocks(soup: Any) -> list[Mapping[str, Any]]:
    """Every parseable ``application/ld+json`` object, flattened one level.

    Schema.org markup is the single most reliable factual surface on a modern
    product page: it is machine-authored by the vendor. Malformed blocks are
    skipped silently — a broken block is not evidence.
    """
    out: list[Mapping[str, Any]] = []
    try:
        nodes = soup.find_all("script", attrs={"type": "application/ld+json"})
    except Exception:  # noqa: BLE001
        return out
    for node in nodes[:20]:
        raw = node.string or node.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001 - malformed JSON-LD is common
            continue
        for item in _iter_jsonld(data):
            out.append(item)
    return out


def _iter_jsonld(data: Any, depth: int = 0) -> Iterable[Mapping[str, Any]]:
    """Yield every mapping inside a JSON-LD document (``@graph`` included)."""
    if depth > 4:
        return
    if isinstance(data, Mapping):
        yield data
        graph = data.get("@graph")
        if isinstance(graph, (list, tuple)):
            for item in graph:
                yield from _iter_jsonld(item, depth + 1)
    elif isinstance(data, (list, tuple)):
        for item in data:
            yield from _iter_jsonld(item, depth + 1)


# ============================================================== public entry
def extract_official_facts(
    markup: str | None,
    official_url: str,
    *,
    product_name: str | None = None,
    extra_pages: Sequence[tuple[str, str]] | None = None,
) -> OfficialFacts:
    """Read verified facts off a product's official page(s). Pure/offline.

    ``markup`` is the fetched homepage body; ``extra_pages`` is an optional
    sequence of ``(url, markup)`` for additional **same-site** official pages
    (typically ``/pricing``). Off-site pages are refused: a fact is only
    "official" if it came from the product's own domain.

    Returns an :class:`OfficialFacts` whose populated fields are all backed by
    an entry in :attr:`OfficialFacts.evidence`. An unreadable page yields an
    empty record, never a guessed one.
    """
    url = normalize_url(official_url) or official_url
    facts = OfficialFacts(official_url=url)

    home = _page_view(markup, url)
    views: list[_PageView] = []
    if home is not None:
        views.append(home)
        facts.pages_read.append(url)

    for extra_url, extra_markup in extra_pages or ():
        target = normalize_url(extra_url) or extra_url
        if not same_site(url, target):
            # An off-site page cannot supply "official" facts about this product.
            continue
        view = _page_view(extra_markup, target, is_pricing=_looks_like_pricing_url(target))
        if view is None:
            continue
        views.append(view)
        facts.pages_read.append(target)

    if not views:
        facts.unverified_fields = ["all"]
        return facts

    primary = views[0]
    combined = " \n".join(view.lowered for view in views)

    _extract_company(facts, views, product_name=product_name)
    _extract_descriptions(facts, primary, views, product_name=product_name)
    _extract_features(facts, views)
    _extract_use_cases(facts, views)
    _extract_capabilities(facts, combined)
    _extract_io(facts, combined)
    _extract_platforms(facts, views, combined)
    _extract_integrations(facts, views, combined)
    _extract_api(facts, views, combined)
    _extract_open_source(facts, views, combined)
    _extract_signup(facts, views, combined)
    _extract_pricing(facts, views, combined)
    _extract_limitations(facts, views)
    _extract_version(facts, views, combined)
    _extract_launch(facts, views, combined)
    _extract_user_count(facts, combined)
    _extract_logo(facts, primary)

    facts.unverified_fields = sorted(
        name
        for name in (
            "company", "product_description", "detailed_overview", "key_features",
            "use_cases", "ai_capabilities", "inputs", "outputs", "platforms",
            "integrations", "has_api", "open_source_status", "signup_requirement",
            "pricing_model", "starting_price_amount", "has_free_plan",
            "has_free_trial", "limitations", "version", "launch_date",
        )
        if name not in facts.evidence
    )
    return facts


def _looks_like_pricing_url(url: str) -> bool:
    path = (urlsplit(url).path or "").casefold()
    return any(part in path for part in ("pricing", "plans", "price", "subscribe"))


# ============================================================ field extractors
def _extract_company(
    facts: OfficialFacts,
    views: Sequence[_PageView],
    *,
    product_name: str | None,
) -> None:
    """Company / developer, from JSON-LD first, then the copyright line.

    Order matters: ``Organization``/``publisher`` markup is authored by the
    vendor, while a copyright line is free text that often carries the product
    name instead of the legal entity. Both beat guessing; neither is invented.
    """
    for view in views:
        for block in view.jsonld:
            types = _jsonld_types(block)
            if types & {"organization", "corporation", "localbusiness"}:
                name = clean_text(block.get("name"), max_length=120)
                if _plausible_company(name):
                    facts.company = name
                    facts.note("company", f"JSON-LD Organization.name on {view.url}")
                    return
            for key in ("publisher", "author", "provider", "creator", "brand"):
                nested = block.get(key)
                if isinstance(nested, Mapping):
                    name = clean_text(nested.get("name"), max_length=120)
                    if _plausible_company(name):
                        facts.company = name
                        facts.note("company", f"JSON-LD {key}.name on {view.url}")
                        return

    # Fallback: a copyright notice with a legal suffix is a strong, explicit
    # claim ("© 2026 Acme Technologies Inc."). Without a suffix we only accept
    # it when it is clearly not just the product name repeated.
    for view in views:
        for match in _COPYRIGHT_RE.finditer(view.text):
            candidate = clean_text(match.group("name"), max_length=120)
            if not _plausible_company(candidate):
                continue
            has_suffix = bool(_COMPANY_SUFFIX_RE.search(candidate))
            if not has_suffix and product_name:
                if candidate.casefold() == (clean_text(product_name) or "").casefold():
                    # "© 2026 Acme" where Acme *is* the product: the copyright
                    # holder is plausibly the same entity, so accept it but say so.
                    facts.company = candidate
                    facts.note(
                        "company",
                        f"copyright line on {view.url} (matches the product name)",
                    )
                    return
            facts.company = candidate
            facts.note("company", f"copyright notice on {view.url}: '{match.group(0)[:80]}'")
            return


def _plausible_company(name: str | None) -> bool:
    """Reject boilerplate, URLs and single stop-words posing as a company."""
    if not name:
        return False
    lowered = name.casefold().strip(" .,|·•-")
    if len(lowered) < 2 or len(lowered) > 80:
        return False
    if lowered in _COMPANY_STOPWORDS:
        return False
    if any(word in lowered for word in ("rights reserved", "privacy", "cookie")):
        return False
    if lowered.startswith(("http", "www.")) or "@" in lowered:
        return False
    # A "company" made only of digits/punctuation is noise.
    return any(ch.isalpha() for ch in lowered)


def _jsonld_types(block: Mapping[str, Any]) -> set[str]:
    raw = block.get("@type")
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    return {str(v).casefold() for v in values if v}


def _extract_descriptions(
    facts: OfficialFacts,
    primary: _PageView,
    views: Sequence[_PageView],
    *,
    product_name: str | None,
) -> None:
    """Short description (meta/JSON-LD/hero) and a detailed overview.

    The overview is *assembled from the page's own sentences* — it is never
    written by this module. That keeps it factual: every clause appears on the
    official page verbatim.
    """
    # --- short description -------------------------------------------------
    for view in views:
        for block in view.jsonld:
            types = _jsonld_types(block)
            if types & {"softwareapplication", "webapplication", "product", "service",
                        "website", "organization"}:
                value = clean_text(block.get("description"), max_length=320)
                if value and len(value) >= 30:
                    facts.product_description = value
                    facts.note(
                        "product_description",
                        f"JSON-LD description on {view.url}",
                    )
                    break
        if facts.product_description:
            break

    if not facts.product_description and primary.meta_description:
        if len(primary.meta_description) >= 30:
            facts.product_description = clean_text(
                primary.meta_description, max_length=320
            )
            facts.note("product_description", f"meta description on {primary.url}")

    if not facts.product_description:
        # The hero subheading directly under the h1 is the product's own pitch.
        hero = _hero_subtext(primary, product_name=product_name)
        if hero:
            facts.product_description = clean_text(hero, max_length=320)
            facts.note("product_description", f"hero heading/subheading on {primary.url}")

    # --- detailed overview -------------------------------------------------
    overview = _assemble_overview(primary, facts)
    if overview:
        facts.detailed_overview = overview
        facts.note(
            "detailed_overview",
            f"assembled from headings and body sentences on {primary.url}",
        )


def _hero_subtext(view: _PageView, *, product_name: str | None) -> str | None:
    """The first substantive h1/h2 that is not just the brand name."""
    brand = (clean_text(product_name) or "").casefold()
    for tag, value in view.headings:
        if tag not in ("h1", "h2"):
            continue
        lowered = value.casefold()
        if brand and lowered == brand:
            continue
        if len(value) < 25 or len(value) > 300:
            continue
        return value
    return None


def _assemble_overview(view: _PageView, facts: OfficialFacts) -> str | None:
    """Build a factual overview out of the page's own sentences.

    Nothing is paraphrased or embellished: the result is a concatenation of
    sentences that literally appear on the official page. This is the honest
    way to produce a "detailed overview" without an LLM inventing claims.
    """
    parts: list[str] = []
    seen: set[str] = set()

    def push(value: str | None) -> None:
        cleaned = clean_text(value)
        if not cleaned or len(cleaned) < 30:
            return
        key = cleaned.casefold()[:80]
        if key in seen:
            return
        seen.add(key)
        parts.append(cleaned.rstrip(".") + ".")

    push(facts.product_description)
    for tag, value in view.headings:
        if tag in ("h1", "h2") and len(value) >= 40:
            push(value)
        if sum(len(p) for p in parts) > 900:
            break

    for sentence in _sentences(view.text)[:40]:
        if sum(len(p) for p in parts) > 900:
            break
        if len(sentence) < 60 or len(sentence) > 300:
            continue
        lowered = sentence.casefold()
        if any(noise in lowered for noise in (
            "cookie", "privacy policy", "terms of service", "all rights reserved",
            "subscribe to our newsletter", "javascript",
            "sign up", "log in", "login", "get started", "start free",
            "book a demo", "request a demo", "contact sales", "learn more",
            "read more", "view pricing", "see pricing", "join now",
            "download now", "try it free", "try for free",
        )):
            continue
        # Navigation/CTA fragments are not product descriptions. Reject
        # sentences that are mostly short UI labels joined by separators.
        if any(sep in sentence for sep in (" | ", " > ", " → ", " / ")):
            continue
        push(sentence)

    if not parts:
        return None
    overview = " ".join(parts)[:2000]
    return overview if len(overview) >= 120 else None


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_RE.split(text or "") if s.strip()]


def _extract_features(facts: OfficialFacts, views: Sequence[_PageView]) -> None:
    """Key features, from a feature *section* first, then feature-card headings.

    Requiring a section or a repeated heading pattern is what separates real
    features from random marketing sentences: a page that never enumerates
    anything gets an empty list rather than a list of slogans.
    """
    collected: list[str] = []
    source: str | None = None

    for view in views:
        items, where = _section_items(view, _FEATURE_HEADINGS)
        if items:
            collected = items
            source = where
            break

    if not collected:
        # Feature cards are commonly h3/h4 headings repeated across a grid.
        for view in views:
            cards = [
                value
                for tag, value in view.headings
                if tag in ("h3", "h4")
                and _MIN_BULLET <= len(value) <= _MAX_BULLET
                and _looks_like_feature(value)
            ]
            if len(cards) >= 3:
                collected = cards
                source = f"repeated h3/h4 feature cards on {view.url}"
                break

    features = _dedupe_bullets(collected, MAX_FEATURES)
    if features:
        facts.key_features = features
        facts.note("key_features", source or "feature list on the official page")


def _extract_use_cases(facts: OfficialFacts, views: Sequence[_PageView]) -> None:
    """Use cases — only from a section that explicitly introduces them."""
    for view in views:
        items, where = _section_items(view, _USE_CASE_HEADINGS)
        cases = _dedupe_bullets(items, MAX_USE_CASES)
        if cases:
            facts.use_cases = cases
            facts.note("use_cases", where or f"use-case section on {view.url}")
            return


def _extract_limitations(facts: OfficialFacts, views: Sequence[_PageView]) -> None:
    """Limitations — only when the page states them under its own heading.

    Inferred limitations would be editorial opinion masquerading as a fact, so
    nothing is derived here; :mod:`src.enrichment.editorial` handles the
    evidence-grounded editorial view separately.
    """
    for view in views:
        items, where = _section_items(view, _LIMITATION_HEADINGS)
        limits = _dedupe_bullets(items, MAX_LIMITATIONS)
        if limits:
            facts.limitations = limits
            facts.note("limitations", where or f"limitations section on {view.url}")
            return


def _section_items(
    view: _PageView, cues: Sequence[str]
) -> tuple[list[str], str | None]:
    """List items / card headings that follow a heading matching ``cues``.

    Walks the document *after* the matching heading and stops at the next
    same-or-higher-level heading, so a "Features" list cannot absorb the
    "Pricing" section beneath it.
    """
    try:
        headings = view.soup.find_all(["h1", "h2", "h3"])
    except Exception:  # noqa: BLE001
        return [], None

    for heading in headings:
        label = (node_text(heading) or "").casefold().strip(" :·—-")
        if not label or not any(_cue_matches(label, cue) for cue in cues):
            continue
        level = int(heading.name[1])
        items: list[str] = []
        for node in heading.find_all_next():
            name = getattr(node, "name", None)
            if name in ("h1", "h2", "h3") and int(name[1]) <= level:
                break
            if name == "li":
                value = node_text(node, max_length=_MAX_BULLET)
                if value and _MIN_BULLET <= len(value) <= _MAX_BULLET:
                    items.append(value)
            elif name in ("h3", "h4", "h5"):
                value = node_text(node, max_length=_MAX_BULLET)
                if value and _MIN_BULLET <= len(value) <= _MAX_BULLET:
                    items.append(value)
            if len(items) >= 40:
                break
        if items:
            return items, f"'{label}' section on {view.url}"
    return [], None


def _cue_matches(label: str, cue: str) -> bool:
    """A heading matches a cue when the cue is the heading's own subject.

    Substring matching alone would let "No features are missing from our
    privacy policy" count; requiring the cue at a word boundary near the start
    keeps it to headings that really introduce that section.
    """
    if label == cue:
        return True
    if label.startswith(cue) and len(label) <= len(cue) + 40:
        return True
    return f" {cue}" in f" {label}" and len(label) <= len(cue) + 40


def _looks_like_feature(value: str) -> bool:
    """Heuristic guard: a feature card headline, not navigation or legal text."""
    lowered = value.casefold()
    if any(noise in lowered for noise in (
        "cookie", "privacy", "terms", "sign in", "log in", "sign up",
        "pricing", "faq", "blog", "careers", "contact us", "about us",
        "all rights", "newsletter", "©",
    )):
        return False
    # Needs at least two words to be a claim rather than a nav label.
    return len(value.split()) >= 2


def _dedupe_bullets(values: Sequence[str], limit: int) -> list[str]:
    """Clean, de-duplicate and cap a bullet list, preserving page order."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = clean_text(value, max_length=_MAX_BULLET)
        if not cleaned or len(cleaned) < _MIN_BULLET:
            continue
        if not _looks_like_feature(cleaned):
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= limit:
            break
    return out


def _extract_capabilities(facts: OfficialFacts, combined: str) -> None:
    """AI capabilities claimed by the page, via distinctive phrases only."""
    found: list[str] = []
    hits: list[str] = []
    for capability, phrases in _CAPABILITY_PHRASES:
        for phrase in phrases:
            if phrase in combined:
                value = capability.value
                if value not in found:
                    found.append(value)
                    hits.append(f"'{phrase}'")
                break
    if found:
        facts.ai_capabilities = found
        facts.note(
            "ai_capabilities",
            "official page states: " + ", ".join(hits[:6]),
        )


def _extract_io(facts: OfficialFacts, combined: str) -> None:
    """Input and output formats, each from its own phrase table."""
    inputs, in_hits = _match_formats(_INPUT_PHRASES, combined)
    if inputs:
        facts.inputs = inputs
        facts.note("inputs", "official page states: " + ", ".join(in_hits[:5]))

    outputs, out_hits = _match_formats(_OUTPUT_PHRASES, combined)
    if outputs:
        facts.outputs = outputs
        facts.note("outputs", "official page states: " + ", ".join(out_hits[:5]))


def _match_formats(
    table: Sequence[tuple[IOFormat, tuple[str, ...]]], combined: str
) -> tuple[list[str], list[str]]:
    found: list[str] = []
    hits: list[str] = []
    for fmt, phrases in table:
        for phrase in phrases:
            if phrase and phrase in combined:
                if fmt.value not in found:
                    found.append(fmt.value)
                    hits.append(f"'{phrase}'")
                break
    return found, hits


def _extract_platforms(
    facts: OfficialFacts, views: Sequence[_PageView], combined: str
) -> None:
    """Platforms, preferring hard link evidence (a real store listing).

    ``web`` is only claimed when the page actually offers an in-browser entry
    point; a marketing site alone does not prove a web *app* exists.
    """
    found: list[str] = []
    hits: list[str] = []

    hosts_seen: list[str] = []
    for view in views:
        for target, _label in view.anchors:
            host = (urlsplit(target).netloc or "").casefold()
            if host:
                hosts_seen.append(f"{host}{urlsplit(target).path.casefold()}")

    for platform, host_fragments in _PLATFORM_LINK_HOSTS:
        for fragment in host_fragments:
            if any(fragment in seen for seen in hosts_seen):
                if platform.value not in found:
                    found.append(platform.value)
                    hits.append(f"link to {fragment}")
                break

    for platform, phrases in _PLATFORM_PHRASES:
        if platform.value in found:
            continue
        for phrase in phrases:
            if phrase in combined:
                found.append(platform.value)
                hits.append(f"'{phrase}'")
                break

    # A same-site app/dashboard/login entry point proves a web app.
    site = views[0].url if views else None
    for view in views:
        for target, label in view.anchors:
            if not same_site(site, target):
                continue
            path = (urlsplit(target).path or "").casefold()
            haystack = f"{path} {label.casefold()}"
            if any(cue in haystack for cue in (
                "/app", "dashboard", "/studio", "workspace", "console",
                "playground", "editor", "log in", "login", "sign in",
            )):
                if Platform.WEB.value not in found:
                    found.append(Platform.WEB.value)
                    hits.append("same-site web app/login entry point")
                break

    if found:
        facts.platforms = found
        facts.note("platforms", "official page evidence: " + ", ".join(hits[:6]))


def _extract_integrations(
    facts: OfficialFacts, views: Sequence[_PageView], combined: str
) -> None:
    """Third-party integrations named by the page.

    Two evidence levels: a dedicated integrations section (strong), or the
    brand appearing anywhere on the page (weaker). Social brands only count
    inside an integrations section, so a footer icon row never becomes an
    "integration".
    """
    section_text = ""
    section_where: str | None = None
    for view in views:
        items, where = _section_items(view, _INTEGRATION_HEADINGS)
        if items:
            section_text = " ".join(items).casefold()
            section_where = where
            break

    found: list[str] = []
    hits: list[str] = []
    for label, needles in _INTEGRATION_CATALOG:
        in_section = any(needle in section_text for needle in needles)
        in_page = any(needle in combined for needle in needles)
        if in_section:
            found.append(label)
            hits.append(label)
        elif in_page and label not in _SOCIAL_ONLY:
            found.append(label)
            hits.append(label)
        if len(found) >= MAX_INTEGRATIONS:
            break

    if found:
        facts.integrations = found
        facts.note(
            "integrations",
            (section_where or "named on the official page")
            + ": "
            + ", ".join(hits[:8]),
        )


def _extract_api(
    facts: OfficialFacts, views: Sequence[_PageView], combined: str
) -> None:
    """API availability, proven by a real API/docs link or explicit wording.

    ``has_api=False`` is never inferred: "we saw no API" is not "there is no
    API", so the absence of evidence leaves the field ``None``.
    """
    site = views[0].url if views else None
    for view in views:
        for target, label in view.anchors:
            path = (urlsplit(target).path or "").casefold()
            host = (urlsplit(target).netloc or "").casefold()
            haystack = f"{host}{path} {label.casefold()}"
            is_api_link = (
                re.search(r"(^|[/.])api([/.]|$)", f"{host}{path}") is not None
                or "/docs/api" in path
                or "api-reference" in haystack
                or "api reference" in haystack
                or "developer" in haystack and "api" in combined
            )
            if not is_api_link:
                continue
            # Only the product's own domain (or a docs subdomain of it) counts.
            if not (same_site(site, target) or _is_docs_subdomain(site, target)):
                continue
            facts.has_api = True
            facts.note("has_api", f"official page links to an API/docs page: {target}")
            if not facts.api_docs_url:
                facts.api_docs_url = target
                facts.note("api_docs_url", f"API/docs link on {view.url}")
            return

    for phrase in ("rest api", "our api", "public api", "api access",
                   "api endpoint", "api key", "developer api", "graphql api",
                   "api documentation"):
        if phrase in combined:
            facts.has_api = True
            facts.note("has_api", f"official page states '{phrase}'")
            return


def _is_docs_subdomain(site: str | None, target: str) -> bool:
    """True when ``target`` is a docs/api subdomain of the product's domain."""
    site_domain = extract_registrable_domain(site)
    target_domain = extract_registrable_domain(target)
    if not site_domain or site_domain != target_domain:
        return False
    host = (urlsplit(target).netloc or "").casefold()
    return host.startswith(("docs.", "api.", "developer.", "developers."))


def _extract_open_source(
    facts: OfficialFacts, views: Sequence[_PageView], combined: str
) -> None:
    """Open-source status, from a repository link or an explicit licence claim.

    A GitHub link alone is not enough (many closed products link to a demo or
    an SDK), so the link must be paired with open-source/licence wording, or
    the page must state the licence itself.
    """
    repo: str | None = None
    for view in views:
        for target, label in view.anchors:
            host = (urlsplit(target).netloc or "").casefold()
            if not any(h in host for h in ("github.com", "gitlab.com", "bitbucket.org",
                                           "huggingface.co", "codeberg.org")):
                continue
            path = (urlsplit(target).path or "").strip("/")
            if not path or path.count("/") < 1:
                continue  # an org profile, not a repository
            repo = target
            if not facts.repository_url:
                facts.repository_url = target
                facts.note(
                    "repository_url",
                    f"repository link on {view.url} ('{label[:40] or host}')",
                )
            break
        if repo:
            break

    licence_hit = next((p for p in _OPEN_SOURCE_PHRASES if p in combined), None)
    source_hit = next((p for p in _SOURCE_AVAILABLE_PHRASES if p in combined), None)

    if licence_hit and (repo or "license" in licence_hit or "licence" in licence_hit):
        facts.open_source_status = OpenSourceStatus.OPEN_SOURCE.value
        facts.note(
            "open_source_status",
            f"official page states '{licence_hit}'"
            + (f" and links to {repo}" if repo else ""),
        )
        return
    if source_hit:
        facts.open_source_status = OpenSourceStatus.SOURCE_AVAILABLE.value
        facts.note("open_source_status", f"official page states '{source_hit}'")


def _extract_signup(
    facts: OfficialFacts, views: Sequence[_PageView], combined: str
) -> None:
    """Signup requirement, from explicit wording or a real signup affordance."""
    for phrase in _WAITLIST_PHRASES:
        if phrase in combined:
            facts.signup_requirement = SignupRequirement.WAITLIST.value
            facts.note("signup_requirement", f"official page states '{phrase}'")
            return
    for phrase in _INVITE_PHRASES:
        if phrase in combined:
            facts.signup_requirement = SignupRequirement.INVITE_ONLY.value
            facts.note("signup_requirement", f"official page states '{phrase}'")
            return
    for phrase in _NO_SIGNUP_PHRASES:
        if phrase in combined:
            facts.signup_requirement = SignupRequirement.NONE.value
            facts.note("signup_requirement", f"official page states '{phrase}'")
            return

    site = views[0].url if views else None
    for view in views:
        for target, label in view.anchors:
            if not same_site(site, target):
                continue
            haystack = f"{(urlsplit(target).path or '').casefold()} {label.casefold()}"
            if any(cue in haystack for cue in (
                "sign up", "signup", "sign-up", "register", "create account",
                "create-account", "get started free", "start free",
            )):
                facts.signup_requirement = SignupRequirement.REQUIRED.value
                facts.note(
                    "signup_requirement",
                    f"official page offers account creation: {target}",
                )
                return


def _extract_pricing(
    facts: OfficialFacts, views: Sequence[_PageView], combined: str
) -> None:
    """Pricing facts — model, starting price, free plan/trial, pricing URL.

    This is the field most tempting to fabricate and the one most often wrong
    in directories, so the rules are strict:

    * the **model** is only set from an explicit phrase table, checked in
      specificity order (one-time/credits/usage before subscription), or from
      real ``$0`` free wording;
    * the **starting price** is the *lowest non-zero* price actually printed on
      the page, never an average or a guess, and it is only accepted from a
      pricing surface (a pricing page, or a pricing section of the homepage);
    * ``has_free_plan``/``has_free_trial`` are ``True`` only on explicit
      wording. They are never set to ``False`` by absence.
    """
    # --- pricing URL -------------------------------------------------------
    site = views[0].url if views else None
    for view in views:
        if view.is_pricing_page and not facts.pricing_url:
            facts.pricing_url = view.url
            facts.note("pricing_url", f"pricing page fetched: {view.url}")
            break
    if not facts.pricing_url:
        for view in views:
            for target, label in view.anchors:
                if not same_site(site, target):
                    continue
                haystack = f"{(urlsplit(target).path or '').casefold()} {label.casefold()}"
                if any(cue in haystack for cue in ("pricing", "plans", "/price")):
                    facts.pricing_url = target
                    facts.note("pricing_url", f"pricing link on {view.url}")
                    break
            if facts.pricing_url:
                break

    # --- free plan / trial -------------------------------------------------
    free_hit = next((p for p in _FREE_PLAN_PHRASES if p in combined), None)
    if free_hit:
        facts.has_free_plan = True
        facts.note("has_free_plan", f"official page states '{free_hit}'")

    trial = _FREE_TRIAL_RE.search(combined)
    if trial:
        facts.has_free_trial = True
        facts.note("has_free_trial", f"official page states '{trial.group(0)[:60]}'")
        days = trial.group("days") or trial.group("days2")
        if days:
            try:
                value = int(days)
            except ValueError:
                value = 0
            if 0 < value <= 365:
                facts.free_trial_days = value
                facts.note("free_trial_days", f"stated trial length: {value} days")

    # --- starting price ----------------------------------------------------
    # Only pricing surfaces are searched for prices: a testimonial saying
    # "saved us $5,000" on a homepage is not a price.
    price_sources: list[_PageView] = [v for v in views if v.is_pricing_page]
    if not price_sources:
        price_sources = [v for v in views if _has_pricing_section(v)]

    best: tuple[float, str, str, str] | None = None  # (amount, currency, period, raw)
    for view in price_sources:
        for match in _PRICE_RE.finditer(view.text):
            amount = _parse_amount(match.group("amount"))
            if amount is None or amount <= 0 or amount > 100_000:
                continue
            symbol = (match.group("sym") or "").casefold()
            currency = _CURRENCY_BY_SYMBOL.get(symbol) or _CURRENCY_BY_SYMBOL.get(
                symbol.strip(), "USD"
            )
            period = _period_for(match.group("per"))
            raw = clean_text(match.group(0), max_length=60) or ""
            if best is None or amount < best[0]:
                best = (amount, currency, period, raw)

    if best is not None:
        amount, currency, period, raw = best
        facts.starting_price_amount = amount
        facts.starting_price_currency = currency
        facts.starting_price_raw = raw
        facts.note(
            "starting_price_amount",
            f"lowest price printed on the pricing surface: '{raw}'",
        )
        if period:
            facts.starting_price_period = period
            facts.note("starting_price_period", f"period read from '{raw}'")

    # --- pricing model -----------------------------------------------------
    for model, phrases in _PRICING_MODEL_PHRASES:
        hit = next((p for p in phrases if p in combined), None)
        if not hit:
            continue
        # "freemium" is the honest label when a paid plan coexists with a
        # documented free plan; it is derived from two facts, not guessed.
        if (
            model in (PricingModel.SUBSCRIPTION, PricingModel.USAGE_BASED,
                      PricingModel.CREDITS, PricingModel.ONE_TIME)
            and facts.has_free_plan
        ):
            facts.pricing_model = PricingModel.FREEMIUM.value
            facts.note(
                "pricing_model",
                f"paid plans ('{hit}') alongside a documented free plan",
            )
            return
        facts.pricing_model = model.value
        facts.note("pricing_model", f"official page states '{hit}'")
        return

    if facts.starting_price_amount and facts.has_free_plan:
        facts.pricing_model = PricingModel.FREEMIUM.value
        facts.note(
            "pricing_model",
            f"free plan plus a paid tier at {facts.starting_price_raw}",
        )
        return
    if facts.starting_price_amount:
        facts.pricing_model = PricingModel.PAID.value
        facts.note("pricing_model", f"published price {facts.starting_price_raw}")
        return
    if facts.has_free_plan and "free forever" in combined:
        facts.pricing_model = PricingModel.FREE.value
        facts.note("pricing_model", "official page states 'free forever'")


def _has_pricing_section(view: _PageView) -> bool:
    """True when the page carries its own pricing section/table."""
    for _tag, value in view.headings:
        lowered = value.casefold()
        if any(cue in lowered for cue in ("pricing", "plans", "choose your plan",
                                          "simple pricing")):
            return True
    return False


def _parse_amount(raw: str | None) -> float | None:
    if not raw:
        return None
    cleaned = raw.replace(",", ".")
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None


def _period_for(raw: str | None) -> str:
    if not raw:
        return ""
    lowered = raw.casefold()
    for needle, period in _PERIOD_WORDS:
        if needle in lowered:
            return period
    return ""


def _extract_version(
    facts: OfficialFacts, views: Sequence[_PageView], combined: str
) -> None:
    """Version — only when the page prints one explicitly.

    JSON-LD ``softwareVersion`` is authoritative. Free-text "v2.1" is accepted
    only from a heading, where it is the page's own claim about the product,
    not a number lifted out of a changelog entry about something else.
    """
    for view in views:
        for block in view.jsonld:
            value = clean_text(
                block.get("softwareVersion") or block.get("version"), max_length=40
            )
            if value:
                facts.version = value
                facts.note("version", f"JSON-LD softwareVersion on {view.url}")
                return

    for view in views:
        for tag, value in view.headings:
            if tag not in ("h1", "h2"):
                continue
            match = _VERSION_RE.search(value)
            if match:
                facts.version = match.group("ver")
                facts.note("version", f"stated in a heading on {view.url}: '{value[:60]}'")
                return


def _extract_launch(
    facts: OfficialFacts, views: Sequence[_PageView], combined: str
) -> None:
    """Launch/release date — only from an explicit, dated statement.

    A copyright year is *not* a launch date and is deliberately ignored.
    """
    for view in views:
        for block in view.jsonld:
            for key in ("datePublished", "releaseDate", "foundingDate", "dateCreated"):
                raw = clean_text(block.get(key))
                if not raw:
                    continue
                parsed, precision = _parse_date(raw)
                if parsed:
                    facts.launch_date = parsed
                    facts.launch_date_precision = precision
                    facts.note("launch_date", f"JSON-LD {key} on {view.url}: '{raw}'")
                    return

    match = _LAUNCH_RE.search(combined)
    if match:
        raw = clean_text(match.group("value"))
        parsed, precision = _parse_date(raw)
        if parsed:
            facts.launch_date = parsed
            facts.launch_date_precision = precision
            facts.note(
                "launch_date",
                f"official page states '{clean_text(match.group(0))[:60]}'",
            )


def _parse_date(raw: str | None) -> tuple[date | None, str | None]:
    """Parse an explicit date string. Year-only yields year precision.

    A future date is refused: it is bad data, and bad data must not become a
    verified fact.
    """
    if not raw:
        return None, None
    text = raw.strip()
    today = datetime.now(timezone.utc).date()
    if re.fullmatch(r"\d{4}", text):
        year = int(text)
        if 1990 <= year <= today.year:
            return date(year, 1, 1), "year"
        return None, None
    try:
        from dateutil import parser as date_parser

        parsed = date_parser.parse(text, fuzzy=False, default=datetime(2000, 1, 1))
    except Exception:  # noqa: BLE001 - unparseable text is not a date
        return None, None
    value = parsed.date()
    if value > today or value.year < 1990:
        return None, None
    precision = "day" if re.search(r"\d{1,2}", text.split()[-1] or "") else "month"
    return value, precision


def _extract_user_count(facts: OfficialFacts, combined: str) -> None:
    """A *stated* user count (e.g. "trusted by 50,000+ creators").

    Recorded as the vendor's own claim — a string, never converted into a
    numeric adoption metric, because a marketing claim is not a measurement.
    """
    match = _USER_COUNT_RE.search(combined)
    if match:
        value = clean_text(match.group(0), max_length=80)
        if value:
            facts.stated_user_count = value
            facts.note("stated_user_count", f"official page claims '{value}'")


def _extract_logo(facts: OfficialFacts, view: _PageView) -> None:
    """Logo URL, from JSON-LD, ``og:image`` or a logo-classed image."""
    for block in view.jsonld:
        logo = block.get("logo")
        if isinstance(logo, Mapping):
            logo = logo.get("url")
        url = normalize_url(clean_text(logo)) if logo else None
        if url:
            facts.logo_url = url
            facts.note("logo_url", f"JSON-LD logo on {view.url}")
            return

    og_image = _meta(view.soup, ("og:image", "twitter:image"))
    if og_image:
        url = absolutize(og_image, view.url)
        if url:
            facts.logo_url = url
            facts.note("logo_url", f"og:image on {view.url}")
            return

    try:
        images = view.soup.find_all("img", limit=25)
    except Exception:  # noqa: BLE001
        images = []
    for node in images:
        haystack = " ".join(
            str(node.get(attr) or "") for attr in ("alt", "class", "id", "src")
        ).casefold()
        if "logo" in haystack:
            url = absolutize(node.get("src"), view.url)
            if url:
                facts.logo_url = url
                facts.note("logo_url", f"logo image on {view.url}")
                return


# ==================================================== verifier-facing adapter
class OfficialFactsExtractor:
    """Adapter that plugs :func:`extract_official_facts` into verification.

    :class:`~src.verification.verifier.OfficialSiteVerifier` accepts injected
    extractors with the signature ``(tool, FetchResult) -> dict`` and applies
    only the non-empty values it gets back. This class is that callable, and it
    is the only place where official facts become :class:`~src.models.tool.Tool`
    fields.

    Two deliberate properties:

    * **It reuses the verification fetch.** The ``FetchResult`` handed to the
      extractor is the very response verification read, so no duplicate request
      is made for the homepage and no field can come from a page that was not
      verified.
    * **One optional extra request.** Pricing usually lives on ``/pricing``,
      not the homepage. When ``client`` is supplied and the homepage links to a
      same-site pricing page, that single page is fetched (cached, robots-aware
      — it goes through the same :class:`~src.core.http_client.HttpClient`).
      Without a client the extractor still works; pricing simply stays blank
      rather than being guessed.

    Extracted facts are also kept in :attr:`facts_by_tool` so the pipeline can
    persist the full evidence trail alongside the record.
    """

    #: Fields this extractor may set (documentation + a guard against drift).
    provides: tuple[str, ...] = (
        "company", "short_description", "detailed_overview", "key_features",
        "use_cases", "ai_capabilities", "inputs", "outputs", "platforms",
        "integrations", "has_api", "api_docs_url", "open_source_status",
        "repository_url", "signup_requirement", "pricing", "adoption",
        "limitations", "version", "launch_date", "launch_date_precision",
        "logo_url", "official_evidence",
    )

    def __init__(
        self,
        client: HttpClient | None = None,
        *,
        fetch_pricing_page: bool = True,
        today: date | None = None,
    ) -> None:
        self.client = client
        self.fetch_pricing_page = fetch_pricing_page
        self.today = today
        #: ``tool.id -> OfficialFacts`` for the records processed so far.
        self.facts_by_tool: dict[str, OfficialFacts] = {}

    # ----------------------------------------------------------- the callable
    def __call__(self, tool: Any, fetched: FetchResult | None) -> dict[str, Any]:
        if fetched is None or not getattr(fetched, "ok", False):
            return {}
        markup = fetched.text or ""
        base_url = fetched.final_url or getattr(tool, "website", None) or ""
        if not base_url:
            return {}

        extra_pages = self._pricing_pages(markup, base_url)
        facts = extract_official_facts(
            markup,
            base_url,
            product_name=getattr(tool, "name", None),
            extra_pages=extra_pages,
        )
        tool_id = getattr(tool, "id", None)
        if tool_id:
            self.facts_by_tool[str(tool_id)] = facts
        return self.to_field_values(tool, facts)

    # ----------------------------------------------------------------- mapping
    def to_field_values(self, tool: Any, facts: OfficialFacts) -> dict[str, Any]:
        """Map :class:`OfficialFacts` onto ``Tool`` field values.

        Only fields the page actually evidenced are returned; the verifier
        drops empties, so an unverified fact simply never appears — it is not
        overwritten with a placeholder.
        """
        values: dict[str, Any] = {}

        def put(field_name: str, value: Any) -> None:
            if value in (None, "", [], {}):
                return
            values[field_name] = value

        put("company", facts.company)
        put("short_description", facts.product_description)
        put("detailed_overview", facts.detailed_overview)
        put("key_features", facts.key_features)
        put("use_cases", facts.use_cases)
        put("ai_capabilities", facts.ai_capabilities)
        put("inputs", facts.inputs)
        put("outputs", facts.outputs)
        put("platforms", facts.platforms)
        put("integrations", facts.integrations)
        put("has_api", facts.has_api)
        put("api_docs_url", facts.api_docs_url)
        put("open_source_status", facts.open_source_status)
        put("repository_url", facts.repository_url)
        put("signup_requirement", facts.signup_requirement)
        put("limitations", facts.limitations)
        put("version", facts.version)
        put("launch_date", facts.launch_date)
        put("launch_date_precision", facts.launch_date_precision)
        put("logo_url", facts.logo_url)

        pricing = self._pricing_value(tool, facts)
        if pricing is not None:
            values["pricing"] = pricing

        adoption = self._adoption_value(tool, facts)
        if adoption is not None:
            values["adoption"] = adoption

        if facts.evidence:
            # The evidence trail travels *on the record*, so a reviewer can
            # audit any value without re-fetching the page.
            existing = dict(getattr(tool, "official_evidence", None) or {})
            existing.update(facts.evidence)
            values["official_evidence"] = existing
        return values

    def _pricing_value(self, tool: Any, facts: OfficialFacts) -> Any:
        """A ``PricingInfo`` carrying only officially evidenced pricing facts."""
        from src.models.tool import PricingInfo

        current = getattr(tool, "pricing", None)
        data: dict[str, Any] = {}
        if current is not None:
            try:
                data = {
                    key: value
                    for key, value in current.model_dump().items()
                    if value not in (None, "", [], {})
                }
            except Exception:  # noqa: BLE001 - a hostile record is data
                data = {}

        changed = False
        for field_name, value in (
            ("model", facts.pricing_model),
            ("starting_price_amount", facts.starting_price_amount),
            ("starting_price_currency", facts.starting_price_currency),
            ("starting_price_period", facts.starting_price_period or None),
            ("starting_price_raw", facts.starting_price_raw),
            ("has_free_plan", facts.has_free_plan),
            ("has_free_trial", facts.has_free_trial),
            ("free_trial_days", facts.free_trial_days),
            ("pricing_url", facts.pricing_url),
        ):
            if value in (None, "", [], {}):
                continue
            data[field_name] = value
            changed = True

        if not changed:
            return None
        # Only dated when something was actually read off the official pages.
        data["pricing_verified_at"] = (self.today or _today()).isoformat()
        try:
            return PricingInfo(**data)
        except Exception as exc:  # noqa: BLE001 - schema rejection is expected
            logger.debug("pricing value rejected by schema: %s", exc)
            return None

    def _adoption_value(self, tool: Any, facts: OfficialFacts) -> Any:
        """Adoption signals — only the vendor's *stated* user count, as text.

        A marketing claim is recorded as a claim. It is never promoted into
        ``monthly_visits`` or any other measured metric, so it cannot buy
        adoption points it did not earn.
        """
        if not facts.stated_user_count:
            return None
        from src.models.base import SourceRef
        from src.models.tool import AdoptionSignals

        current = getattr(tool, "adoption", None)
        data: dict[str, Any] = {}
        if current is not None:
            try:
                data = {
                    key: value
                    for key, value in current.model_dump().items()
                    if value not in (None, "", [], {})
                }
            except Exception:  # noqa: BLE001
                data = {}
        if data.get("stated_user_count"):
            return None
        data["stated_user_count"] = facts.stated_user_count
        data["observed_at"] = (self.today or _today()).isoformat()
        data["signal_sources"] = [
            *(data.get("signal_sources") or []),
            SourceRef(
                name="Official product website",
                url=facts.official_url,
                kind="official",
            ).model_dump(mode="json"),
        ]
        try:
            return AdoptionSignals(**data)
        except Exception as exc:  # noqa: BLE001
            logger.debug("adoption value rejected by schema: %s", exc)
            return None

    # ---------------------------------------------------------- extra fetches
    def _pricing_pages(self, markup: str, base_url: str) -> list[tuple[str, str]]:
        """Fetch at most one same-site pricing page. Never leaves the domain."""
        if not self.fetch_pricing_page or self.client is None:
            return []
        target = _find_pricing_link(markup, base_url)
        if not target:
            return []
        try:
            result = self.client.try_fetch(target, use_cache=True)
        except Exception as exc:  # noqa: BLE001 - a failed extra fetch is not fatal
            logger.debug("pricing page fetch failed: %s", exc)
            return []
        if result is None or not result.ok or not result.text:
            return []
        return [(result.final_url or target, result.text)]


def _find_pricing_link(markup: str, base_url: str) -> str | None:
    """The most likely same-site pricing URL on ``markup`` (``None`` if absent)."""
    view = _page_view(markup, base_url)
    if view is None:
        return None
    best: str | None = None
    for target, label in view.anchors:
        if not same_site(base_url, target):
            continue
        path = (urlsplit(target).path or "").casefold()
        haystack = f"{path} {label.casefold()}"
        if "pricing" in haystack or "/plans" in path or "/price" in path:
            # Prefer a dedicated path over an in-page anchor.
            if path.strip("/"):
                return target
            best = best or target
    return best


def _today() -> date:
    return datetime.now(timezone.utc).date()
