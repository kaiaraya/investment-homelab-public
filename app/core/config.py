from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    PROJECT_NAME: str = "Investment Homelab Gateway"
    API_V1_STR: str = "/api/v1"
    DATABASE_URL: str = "sqlite+aiosqlite:///./investment_homelab.db"
    CLICK2WIN_DATABASE_URL: str = "sqlite+aiosqlite:///./investment_homelab_click2win.db"
    
    # Secret Key for LINE and External Webhook protection
    HOMELAB_SECRET_KEY: str = "change-me-set-in-dotenv"
    
    # LINE Messaging API Credentials
    LINE_CHANNEL_SECRET: str = "mock_line_channel_secret"
    LINE_CHANNEL_ACCESS_TOKEN: str = "mock_line_channel_token"
    LINE_ADMIN_USER_ID: Optional[str] = None
    LINE_SIMULATION_MODE: bool = True

    # --- LINE Group OTP Relay (2026-10-08: replaces KDE Connect as primary OTP channel) ---
    # groupId of the "a14s homelab" LINE group. When set, push_message() targets the
    # group (push `to` accepts a groupId) instead of falling back to broadcast.
    # Auto-captured by the webhook on the first group message (webhook_line.py).
    LINE_GROUP_ID: Optional[str] = None
    # Public webhook URL that the cloudflared tunnel exposes (diagnostics / readiness).
    LINE_PUBLIC_WEBHOOK_URL: str = "https://line.a14s.dev/api/v1/webhook/line"
    # Master switch for the LINE-based OTP readiness gate (Invariant 9 — LINE variant).
    LINE_OTP_RELAY_ENABLED: bool = True

    # --- LINE LIFF / MiniApp (Mobile-First WebApp) ---
    LIFF_ID: Optional[str] = None
    MINIAPP_PUBLIC_BASE_URL: str = "https://miniapp.line.me"
    LINE_LOGIN_CHANNEL_ID: Optional[str] = None
    LINE_ALLOWED_USER_IDS: str = ""
    MINIAPP_DEV_BYPASS_LOCAL: bool = False

    # --- LINE slash-command CLI /negotiate guardrails (TASK-line-cli) ---
    # G5: a /nego amend may move an order at most this many SET ticks from its
    # current price (design spec Q6 / approved 2026-10-13).
    LINE_NEGO_MAX_TICKS: int = 3
    #: CDP-5 — let the /nego G3 money guard accept a CDP-AUTHENTICATED tab (the daemon's
    #: Chrome) as "session alive". Enabled after CDP-5 rollout; fails closed on UNKNOWN/DAEMON_DOWN.
    LINE_NEGO_CDP_GATE: bool = True
    # G2: allowed negotiation windows (Bangkok time), comma-separated "HH:MM-HH:MM".
    LINE_NEGO_WINDOWS: str = "09:55-12:30,14:00-16:30"
    
    # Pre-trade Risk & Guard Configuration
    MAX_ORDER_VALUE_THB: float = 1_000_000.0
    ORDER_IDEMPOTENCY_WINDOW_SEC: int = 10
    
    # Optional Gemini / LLM API key
    GEMINI_API_KEY: Optional[str] = None

    # DeepSeek LLM (OpenAI-compatible) — primary AI interpreter for Signal/OTP agents
    DEEPSEEK_API_KEY: Optional[str] = None
    DEEPSEEK_MODEL: str = "deepseek-chat"
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    
    # Krungsri Securities & Browser Automation Configuration
    KRUNGSRI_USERNAME: str = ""
    KRUNGSRI_PASSWORD: str = ""
    KRUNGSRI_LOGIN_URL: str = "https://www.krungsrisecurities.com/th/main/default.aspx"
    PLAYWRIGHT_HEADLESS: bool = False
    PLAYWRIGHT_CHROME_EXECUTABLE: str = "/usr/bin/google-chrome"
    # Witness/debug: run the Quick Trade driver with a visible window so the
    # human can watch the amend flow. Keep True in normal operations.
    QUICKTRADE_HEADLESS: bool = True
    OTP_TIMEOUT_SECONDS: int = 180
    # KDE-RCA-005: max age (seconds) of a mobile notification still accepted as an OTP
    # candidate when we CANNOT positively match the broker Ref Code. Android Doze /
    # KDE Connect link flapping can delay a push well past the phone-clock `date`, so a
    # tight 5-minute guard silently discards freshly-arrived OTPs. A definitive Ref Code
    # match always bypasses this guard entirely.
    OTP_STALENESS_GRACE_SECONDS: int = 600
    SESSION_STATE_PATH: str = "data/session_state.json"
    SESSION_HEARTBEAT_INTERVAL: int = 1800  # Sentinel check interval: 30 minutes (1800s)
    ENABLE_LEGACY_TRADEMAN: bool = False  # Pure CDP mode retires TradeMan (Streaming + SBL on :9333)
    TRADEMAN_URL: str = "https://trademan.in.th"
    TRADEMAN_PORTFOLIO_URL: str = "https://trademan.in.th/Portfolio"
    TRADEMAN_ACCOUNT_NO_CASH: str = "<REDACTED_KSS_ACCT>"
    TRADEMAN_ACCOUNT_NO_MARGIN: str = "<REDACTED_KSS_ACCT>"
    KRUNGSRI_PIN: str = ""  # set in .env
    TRADING_PIN: str = ""  # set in .env

    # Target Trading Platform Switch ("CLICK2WIN" for Settrade sandbox or "TRADEMAN" for Krungsri live)
    TRADING_PLATFORM: str = "CLICK2WIN"

    # Settrade Click2Win Credentials & Configuration (Web Scraping Legacy)
    CLICK2WIN_URL: str = "https://click2win.settrade.com"
    CLICK2WIN_USERNAME: str = ""
    CLICK2WIN_PASSWORD: str = ""
    CLICK2WIN_PIN: str = ""
    CLICK2WIN_ACCOUNT_NO_EQUITY: str = "CW32_E_7338419"
    CLICK2WIN_ACCOUNT_NO_DERIVATIVES: str = "CW32_D_7338419"
    CLICK2WIN_SESSION_PATH: str = "data/click2win_session.json"

    # Settrade Streaming (Krungsri SSO) Configuration (TASK-loop1-8)
    # Primary broker: Krungsri SSO -> 'โปรแกรม Streaming' -> StreamingPage.jsp
    STREAMING_SESSION_PATH: str = "data/streaming_session.json"
    STREAMING_URL: str = "https://wma1.settrade.com/realtime/streaming7/StreamingPage.jsp"
    STREAMING_ACCOUNT_NO: str = "<REDACTED_KSS_ACCT>"  # margin (primary — Short/Cover), per user
    STREAMING_ACCOUNT_NO_MARGIN: str = "<REDACTED_KSS_ACCT>"
    STREAMING_ACCOUNT_NO_CASH: str = "<REDACTED_KSS_ACCT>"

    # Settrade Open API Configuration (Direct Machine-to-Machine Sandbox / Live API)
    SETTRADE_APP_ID: str = ""
    SETTRADE_APP_SECRET: str = ""
    SETTRADE_BROKER_ID: str = "SANDBOX"
    SETTRADE_APP_CODE: str = "SANDBOX"
    SETTRADE_ACCOUNT_NO_EQUITY: str = ""
    SETTRADE_ACCOUNT_NO_DERIVATIVES: str = ""
    SETTRADE_PIN: str = "000000"

    # Liberator Securities Configuration
    LIBERATOR_USERNAME: str = ""
    LIBERATOR_PASSWORD: str = ""
    LIBERATOR_PIN: str = ""
    LIBERATOR_ACCOUNT_NO: str = ""  # set in .env — Liberator CASH account (<REDACTED_LIBERATOR_USERNAME>)
    LIBERATOR_LOGIN_URL: str = "https://webtrade.liberator.co.th/login"
    LIBERATOR_SESSION_PATH: str = "data/liberator_session.json"

    # Phase 3 & Policy Engine Configuration (4 Centers & Forward-Compatible Safety)
    DRY_RUN_MODE: bool = True  # Safety mode: prepares and fills orders but does not submit final PIN
    # PIN safety (2026-10-29 lockout on account <REDACTED_KSS_ACCT>): every submit carries the trading
    # PIN and the broker locks the account after a few wrong entries. The guard counts
    # broker PIN rejections per broker and fail-closes the next submit; it never counts a
    # pre-submit refusal (no PIN typed) and clears itself only on a broker-accepted order.
    PIN_GUARD_ENABLED: bool = True
    PIN_GUARD_MAX_FAILURES: int = 1
    PIN_GUARD_LOCK_MINUTES: int = 240
    PIN_GUARD_STATE_PATH: str = "data/pin_guard.json"
    TRADING_MODE: str = "TEST"  # "TEST" (Virtual TradeMan Simulator) or "LIVE" (TradeMan Real Orders)
    # KSS price amendment path: "quicktrade" (in-place) or "trademan_cancel_replace"
    # (TradeMan has no native Change — KSS-RCA-016; requires guardrails C1–C7).
    KSS_AMEND_MODE: str = "quicktrade"
    LIVE_ORDER_VALUE_LIMIT_THB: float = 50_000.0  # Guardrail limit per order in LIVE mode
    CARD_TTL_SECONDS: int = 180  # Card expiration window (3 minutes)
    UNANSWERED_ACTION_POLICY: str = "HOLD"  # Options: "HOLD", "URGENT_PING", "AUTO_EXECUTE_CUTLOSS", "AI_AGENT_DECIDE"
    UNANSWERED_TIMEOUT_SECONDS: int = 180  # Timeout for interactive LINE card response
    PRICE_NEGOTIATION_POLICY: str = "PROPOSE_LINE"  # Options: "PROPOSE_LINE", "AUTO_ADJUST", "CANCEL_ORDER", "AI_AGENT_DECIDE"
    ABNORMAL_PRICE_CHANGE_THRESHOLD: float = 3.0  # Alert if %Change vs prior day close >= +3% or <= -3%
    PORTFOLIO_OVERVIEW_TIME: str = "10:15"
    ALERT_CENTER_START: str = "10:30"
    ALERT_CENTER_END: str = "17:30"
    PRICE_NEGOTIATION_ROUNDS: list[str] = ["11:00", "15:30"]
    EOD_REPORT_TIME: str = "16:35"

    # LINE Ecosystem & Quota Policy + Daily EOD Net Equity Recording
    LINE_CHAT_COMMANDS_ENABLED: bool = False
    LINE_PUSH_MODE: str = "eod_only"  # "eod_only" | "all" | "off"
    EOD_SUMMARY_ENABLED: bool = True
    LINE_EOD_PUSH_ENABLED: bool = False
    LINE_EOD_PUSH_TIME: str = "17:05"
    LINE_EOD_STATE_PATH: str = "data/eod_summary_state.json"
    NET_EQUITY_HISTORY_JSON_PATH: str = "data/net_equity_history.json"
    NET_EQUITY_HISTORY_CSV_PATH: str = "data/net_equity_history.csv"
    NET_EQUITY_MIRROR_CSV_PATH: str = "/srv/antigravity/shadow/InvestorPerformance/data/net_equity_history.csv"
    DAILY_PIPELINE_STATUS_PATH: str = "/srv/antigravity/shadow/logs/run_daily_latest.json"
    DAILY_PRICES_CACHE_CSV_PATH: str = "/srv/antigravity/shadow/DailyScanner/output/prices_2026_cache.csv"
    MINIAPP_INBOX_STATE_PATH: str = "data/miniapp_inbox.json"
    MINIAPP_INBOX_ARCHIVE_DIR: str = "logs"
    LINE_OTP_RELAY_CHANNEL: str = "miniapp"  # "miniapp" | "line" | "auto"

    # Scheduler and Notification Control
    ENABLE_INTRADAY_SCHEDULER: bool = False  # Master switch for the intraday market-regime scheduler (8 regimes; formerly Phase 3)
    ENABLE_NEGO_SCHEDULER: bool = True  # Lightweight Pure-CDP Auto-Nego Scheduler (MiniApp Schedule tab)
    NEGO_SCHEDULER_STATE_PATH: str = "data/nego_scheduler_state.json"
    NEGO_SCHEDULER_CHECK_INTERVAL_SEC: int = 20
    ENABLE_RUNBOOK_EXIT_SCHEDULER: bool = True  # Pure-CDP Runbook Daily Auto-Exit Bridge (10:00-10:30)
    RUNBOOK_AUTO_EXIT_DEFAULT_ENABLED: bool = True  # Auto-schedule & execute at 10:00-10:30 when Trade has not acted today
    RUNBOOK_LEDGER_PATH: str = "/srv/antigravity/shadow/InvestorPerformance/data/positions_ledger.json"
    RUNBOOK_EXIT_STATE_PATH: str = "data/runbook_exit_state.json"
    RUNBOOK_EXIT_CHECK_INTERVAL_SEC: int = 20
    SET_HOLIDAYS_PATH: str = "/home/araya-sawasdichai/trading-runbook/data/reference/holidays/holidays_index.json"
    # Model 19 Suggest Entry & Entry Ledger (on-demand human-in-the-loop via MiniApp Trade)
    DAILY_SCANNER_OUTPUT_DIR: str = "/srv/antigravity/shadow/DailyScanner/output"
    ENTRY_LEDGER_PATH: str = "data/entry_ledger.json"
    SUGGEST_ENTRY_STATE_PATH: str = "data/suggest_entry_state.json"
    SUGGEST_ENTRY_DEFAULT_BUDGET_THB: float = 150000.0
    # Slim-gateway switches (2026-10-08): heavy background daemons default OFF so the
    # gateway boots lean (webhook + API + DB + session sentinel only). Flip to True only
    # when the corresponding Pillar is actually under test.
    ENABLE_ACTION_EXIT_DISPATCHER: bool = False  # Tactical Action-Exit daemon + 09:45 pre-flight
    ENABLE_AGENTIC_ORCHESTRATOR: bool = False  # PT-MRO MasterOrchestrator background loop
    ENABLE_CLICK2WIN_POLLING: bool = False  # Master switch for Click2Win Favourite Polling Sentinel Worker (Pillar 2)
    ENABLE_PROGRESS_NOTIFICATIONS: bool = False  # Control 1-line progress updates (⏳/✅) to LINE
    KDECONNECT_DEVICE_ID: str = "9507a3fd53f848da9135ebe6253db18a"
    ENABLE_HITL_LIVE_GATE: bool = True  # Require 1-click confirmation at /console + KDE Connect mobile alert for real money
    ENABLE_HITL_SANDBOX_GATE: bool = True  # Allow testing HITL Gate on Click2Win sandbox without real money risk
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
