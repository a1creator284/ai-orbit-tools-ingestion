"""Controlled vocabularies.

Every enum here has an explicit "unknown"-equivalent absence strategy: fields
that use these enums are Optional, and unverifiable values stay ``None`` rather
than being guessed (Tools guideline section 10).
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """String-valued enum that serialises to its value."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)

    @classmethod
    def coerce(cls, value: object) -> "StrEnum | None":
        """Best-effort parse; returns ``None`` instead of raising."""
        if value is None or isinstance(value, cls):
            return value  # type: ignore[return-value]
        text = str(value).strip().lower().replace(" ", "_").replace("-", "_")
        if not text:
            return None
        for member in cls:
            if member.value == text or member.name.lower() == text:
                return member
        return None


class EntityType(StrEnum):
    """Entity types from the technical specification section 3."""

    TOOL = "tool"
    TASK = "task"
    COMPANY = "company"
    NEWS = "news"
    VIDEO = "video"
    ROBOT = "robot"
    DEVICE = "device"
    MODEL = "model"
    REPOSITORY = "repository"
    MCP = "mcp"
    COLLECTION = "collection"
    PERSONAL = "personal"
    CREATIVE = "creative"


class ToolStatus(StrEnum):
    """Current operational status of a tool (guideline sections 4 and 6)."""

    ACTIVE = "active"
    BETA = "beta"
    ALPHA = "alpha"
    WAITLIST = "waitlist"
    DEPRECATED = "deprecated"
    DISCONTINUED = "discontinued"
    ACQUIRED = "acquired"
    INACCESSIBLE = "inaccessible"


class PricingModel(StrEnum):
    FREE = "free"
    FREEMIUM = "freemium"
    SUBSCRIPTION = "subscription"
    ONE_TIME = "one_time"
    USAGE_BASED = "usage_based"
    CREDITS = "credits"
    OPEN_SOURCE = "open_source"
    ENTERPRISE = "enterprise"
    CONTACT_SALES = "contact_sales"
    PAID = "paid"


class Platform(StrEnum):
    WEB = "web"
    BROWSER_EXTENSION = "browser_extension"
    CHROME_EXTENSION = "chrome_extension"
    FIREFOX_EXTENSION = "firefox_extension"
    EDGE_EXTENSION = "edge_extension"
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"
    IOS = "ios"
    ANDROID = "android"
    API = "api"
    CLI = "cli"
    SDK = "sdk"
    DESKTOP = "desktop"
    MOBILE = "mobile"
    SLACK = "slack"
    DISCORD = "discord"
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    FIGMA_PLUGIN = "figma_plugin"
    VSCODE_EXTENSION = "vscode_extension"
    JETBRAINS_PLUGIN = "jetbrains_plugin"
    WORDPRESS_PLUGIN = "wordpress_plugin"
    SHOPIFY_APP = "shopify_app"
    GOOGLE_WORKSPACE = "google_workspace"
    MICROSOFT_365 = "microsoft_365"
    SELF_HOSTED = "self_hosted"
    ON_PREMISE = "on_premise"


class IOFormat(StrEnum):
    """Specific input/output formats.

    Guideline section 9 bans vague values such as "AI content"; a controlled
    vocabulary makes that structurally impossible.
    """

    # text-ish
    TEXT = "text"
    PROMPT = "prompt"
    MARKDOWN = "markdown"
    HTML = "html"
    JSON = "json"
    CSV = "csv"
    XML = "xml"
    YAML = "yaml"
    SQL = "sql"
    CODE = "code"
    URL = "url"
    RSS = "rss"
    EMAIL = "email"
    # documents
    PDF = "pdf"
    DOCX = "docx"
    DOC = "doc"
    PPTX = "pptx"
    XLSX = "xlsx"
    TXT = "txt"
    EPUB = "epub"
    LATEX = "latex"
    # images
    IMAGE = "image"
    PNG = "png"
    JPG = "jpg"
    SVG = "svg"
    WEBP = "webp"
    GIF = "gif"
    PSD = "psd"
    FIGMA_FILE = "figma_file"
    # audio / video
    AUDIO = "audio"
    MP3 = "mp3"
    WAV = "wav"
    VIDEO = "video"
    MP4 = "mp4"
    SUBTITLES = "subtitles"
    TRANSCRIPT = "transcript"
    # 3d / other
    MODEL_3D = "model_3d"
    OBJ = "obj"
    GLB = "glb"
    # structured products
    SUMMARY = "summary"
    STRUCTURED_REPORT = "structured_report"
    EXTRACTED_DATA = "extracted_data"
    DATASET = "dataset"
    SPREADSHEET = "spreadsheet"
    SLIDE_DECK = "slide_deck"
    CHART = "chart"
    DIAGRAM = "diagram"
    WEBSITE = "website"
    LANDING_PAGE = "landing_page"
    UI_DESIGN = "ui_design"
    API_RESPONSE = "api_response"
    WORKFLOW = "workflow"
    AGENT_ACTION = "agent_action"
    VOICE_CLONE = "voice_clone"
    EDITED_IMAGE = "edited_image"
    EDITED_VIDEO = "edited_video"
    EDITED_AUDIO = "edited_audio"
    TRANSLATION = "translation"
    SEARCH_RESULTS = "search_results"
    CITATIONS = "citations"


class AICapability(StrEnum):
    TEXT_GENERATION = "text_generation"
    TEXT_SUMMARIZATION = "text_summarization"
    TEXT_CLASSIFICATION = "text_classification"
    TRANSLATION = "translation"
    QUESTION_ANSWERING = "question_answering"
    CONVERSATIONAL_AI = "conversational_ai"
    RAG = "retrieval_augmented_generation"
    CODE_GENERATION = "code_generation"
    CODE_REVIEW = "code_review"
    IMAGE_GENERATION = "image_generation"
    IMAGE_EDITING = "image_editing"
    IMAGE_RECOGNITION = "image_recognition"
    OCR = "ocr"
    VIDEO_GENERATION = "video_generation"
    VIDEO_EDITING = "video_editing"
    SPEECH_TO_TEXT = "speech_to_text"
    TEXT_TO_SPEECH = "text_to_speech"
    VOICE_CLONING = "voice_cloning"
    MUSIC_GENERATION = "music_generation"
    RECOMMENDATION = "recommendation"
    FORECASTING = "forecasting"
    DATA_ANALYSIS = "data_analysis"
    WEB_SEARCH = "web_search"
    WEB_BROWSING = "web_browsing"
    AGENTIC_WORKFLOW = "agentic_workflow"
    FUNCTION_CALLING = "function_calling"
    MULTIMODAL = "multimodal"
    FINE_TUNING = "fine_tuning"
    EMBEDDINGS = "embeddings"
    THREE_D_GENERATION = "three_d_generation"
    ANOMALY_DETECTION = "anomaly_detection"
    SENTIMENT_ANALYSIS = "sentiment_analysis"


class OpenSourceStatus(StrEnum):
    OPEN_SOURCE = "open_source"
    SOURCE_AVAILABLE = "source_available"
    PARTIALLY_OPEN = "partially_open"
    PROPRIETARY = "proprietary"


class SignupRequirement(StrEnum):
    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"
    WAITLIST = "waitlist"
    INVITE_ONLY = "invite_only"


class QualityBand(StrEnum):
    """Ranking bands from the Tools guideline section 5."""

    EXCEPTIONAL = "exceptional"   # 90-100 strongly prioritize
    EXCELLENT = "excellent"       # 80-89  include
    GOOD = "good"                 # 70-79  include selectively
    AVERAGE = "average"           # 60-69  usually skip
    REJECT = "reject"             # <60    reject


class VerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    PENDING = "pending"
    VERIFIED = "verified"
    PARTIALLY_VERIFIED = "partially_verified"
    FAILED = "failed"
    UNREACHABLE = "unreachable"


class RejectionReason(StrEnum):
    """Rejection taxonomy from the Tools guideline section 4."""

    DEAD_OR_SHUTDOWN = "dead_or_shutdown"
    WEBSITE_BROKEN = "website_broken"
    ABANDONED = "abandoned"
    NO_MEANINGFUL_FUNCTIONALITY = "no_meaningful_functionality"
    SPAM_OR_SEO_GENERATED = "spam_or_seo_generated"
    DUPLICATE = "duplicate"
    FAKE_OR_UNVERIFIABLE = "fake_or_unverifiable"
    VERY_LOW_QUALITY = "very_low_quality"
    OUTDATED_LOW_USAGE = "outdated_low_usage"
    MINOR_CLONE = "minor_clone"
    UNVERIFIABLE_INFORMATION = "unverifiable_information"
    BELOW_SCORE_THRESHOLD = "below_score_threshold"
    MISSING_REQUIRED_FIELDS = "missing_required_fields"
    NOT_AN_AI_TOOL = "not_an_ai_tool"
    CATEGORY_QUOTA_EXCEEDED = "category_quota_exceeded"


class RelationshipType(StrEnum):
    """Relationship vocabulary from the technical specification section 5."""

    DEVELOPS = "develops"            # Company -> Tool/Model
    SOLVES = "solves"                # Tool -> Task
    INTEGRATES_WITH = "integrates_with"  # MCP -> Tool, Tool -> Tool
    RUNS = "runs"                    # Device -> Model
    USES_MODEL = "uses_model"        # Tool -> Model
    ALTERNATIVE_TO = "alternative_to"
    PART_OF = "part_of"
    HAS_REPOSITORY = "has_repository"
    BELONGS_TO_COLLECTION = "belongs_to_collection"
