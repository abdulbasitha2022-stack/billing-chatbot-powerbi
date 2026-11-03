/*
 * visual.ts - the whole "Billing AI Assistant" Power BI custom visual.
 *
 * WHAT THIS FILE DOES
 *   A Power BI custom visual is a small web page (HTML + JavaScript) that Power BI runs inside
 *   a report. This one draws a chat window. The user types a question ("monthly revenue for
 *   2025"), the visual sends it to the Flask backend (nl2sql_poc/chatbot_api.py) and shows the
 *   assistant's answer. The backend does the heavy work: it plans with an LLM, writes SQL,
 *   creates a MySQL view, adds a new report page to the Power BI project and then reloads
 *   Power BI Desktop so the new page appears.
 *
 * HOW IT TALKS TO THE BACKEND
 *   Plain HTTP calls with fetch() to API_BASE (http://localhost:5056), all going through the
 *   api() helper further down. JSON in, JSON out. Endpoints used here:
 *     GET  /health                         is the backend up? is "demo role" selection on?
 *     POST /chat                           send a user message (or a button action), get new messages back
 *     GET  /session/<id>/progress          short "what am I doing right now" text while /chat runs
 *     GET  /session/<id>/transcript?after=N  every message with id > N (used to restore / catch up)
 *     GET  /session/latest[?demo_role=X]   the most recent active session (used to restore)
 *     POST /session/<id>/end               end the session for good
 *     POST /session/<id>/reset             old "New chat" endpoint (the server now ends the session)
 *     POST /feedback                       "Satisfied" / "Unsatisfied" for one generated report
 *     POST /reports/save                   "Save report" on a generated page
 *     GET  /reports/saved[/<request_id>]   the user's saved reports / is this one saved?
 *     POST /reports/saved/<id>/open|delete open a saved report (restores a removed page) / forget it
 *
 * WHY THE VISUAL MUST "RESTORE" ITSELF
 *   When the backend reloads Power BI Desktop (or the user switches report pages), Power BI
 *   throws this visual away and builds a brand-new one. All JavaScript variables are lost.
 *   The conversation itself is kept on the server, so the visual only has to remember the
 *   session id (in Power BI's local storage) and ask the server for the transcript again.
 *
 * THREE MODES (the same visual code is used in three places)
 *   1. Chat mode: the normal chat window on the "AI Assistant" page (with the "Saved reports" list).
 *   2. Feedback mode: on every generated report page the backend places another copy of this
 *      visual and sets its "feedback" properties (mode/requestId/sessionId, declared under
 *      "objects" in capabilities.json). That copy hides the chat and shows only a small
 *      "Was this report useful?" panel. See update() and renderReportFeedback().
 *   3. Save mode: a third copy next to the "Back to AI Assistant" button that shows only a
 *      "Save report" button (mode "save"). See renderSaveButton().
 *
 * THE SANDBOX
 *   Power BI runs custom visuals in a sandboxed <iframe>. That means: no alert()/confirm()
 *   dialogs, no page navigation, network access only to the URLs listed under the "WebAccess"
 *   privilege in capabilities.json, and browser storage only through Power BI's own
 *   storageV2Service (the "LocalStorage" privilege). That storage can be missing, disabled
 *   by the user/admin, or throw errors - so every storage call here is wrapped in try/catch
 *   and the visual keeps working (just without remembering things) when it fails.
 */
"use strict";

// powerbi-visuals-api gives us the TypeScript types for talking to Power BI (no runtime logic).
import powerbi from "powerbi-visuals-api";
// The stylesheet (colours, bubbles, buttons). The pbiviz build tool bundles it with this file.
import "./../style/visual.less";

// Short local names for three types we use a lot:
//   VisualConstructorOptions - what Power BI hands to the constructor (the host element + host services)
//   VisualUpdateOptions      - what Power BI hands to update() (size, data, formatting "objects")
//   IVisual                  - the interface every custom visual class must implement
import VisualConstructorOptions = powerbi.extensibility.visual.VisualConstructorOptions;
import VisualUpdateOptions = powerbi.extensibility.visual.VisualUpdateOptions;
import IVisual = powerbi.extensibility.visual.IVisual;

// Must match the WebAccess privilege in capabilities.json.
// The Flask backend's address. Requests to any other host are blocked by the sandbox.
const API_BASE = "http://localhost:5056";
// Keys used in Power BI's local storage. The ".v2"/".v1" suffix lets the format change later
// without reading old, incompatible values.
const STORAGE_KEY = "billingChatbot.session.v2";            // the current chat session id
const DEMO_ROLE_STORAGE_KEY = "billingChatbot.demoRole.v1"; // last chosen demo role (also "<key>.<sessionId>" per session)
// "Demo roles" let one person pretend to be different users while demoing (the backend uses the
// role for access rules). The dropdown only appears if /health says demo_role_selection is on.
// "as const" makes TypeScript treat this as the exact three strings, not just string[].
const DEMO_ROLES = ["CEO", "Developer", "Analyst"] as const;
// Timeouts and timer intervals, all in milliseconds (the "_" in 300_000 is just a digit separator).
const CHAT_TIMEOUT_MS = 300_000;   // planning + SQL + Power BI page generation can take a while
const QUICK_TIMEOUT_MS = 20_000;     // default timeout for every other request
const POLL_INTERVAL_MS = 2_500;      // how often to fetch the transcript while the session is "busy"
const PROGRESS_INTERVAL_MS = 1_200;  // how often to fetch the progress text while /chat is running
const POLL_MAX_MS = 4 * 60_000;      // stop transcript polling after 4 minutes no matter what
const DEMO_ROLE_RETRY_MS = 5_000;    // wait between /health retries when the backend is not up yet

// logDebug: the visual's only logging helper. It writes to the browser console, which you can
// only see in the developer tools (in Power BI Desktop: enable developer mode / use the visual's
// debug tools, then open the Console tab). The user never sees these lines in the report.
// Console diagnostics: open Power BI Desktop's developer tools and filter on "[billingChatbot]".
// Never pass message text, assistant replies, data rows or headers here; ids and statuses only.
function logDebug(level: "debug" | "info" | "warn" | "error", message: string, detail?: unknown): void {
    try {
        const line = `[billingChatbot] ${new Date().toISOString()} ${message}`;
        // console[level] picks console.debug / console.info / console.warn / console.error.
        if (detail === undefined) console[level](line);
        else console[level](line, detail);
    } catch {
        /* diagnostics must never break the visual */
    }
}

// The welcome text shown at the top of an empty chat, and the clickable example questions
// ("chips") shown under it. Clicking a chip sends that text as if the user had typed it.
const GREETING =
    "Hi! I'm your Billing AI Assistant. Ask me for a report, or ask questions about the one you're " +
    "viewing. You can also say things like “make it a pie chart” or “same but for 2025”.";
const EXAMPLES = [
    "Monthly revenue for the last 12 months",
    "Active subscriptions by plan",
    "Refunds by reason this year"
];

// ---- Data shapes (TypeScript interfaces only describe objects; they produce no JavaScript) ----

// A suggestion button the backend attaches to an assistant message (e.g. "Bar chart", "Yes").
// Clicking it calls send() with `message` (or the label) and the extra fields below, which tell
// the backend what kind of button was pressed instead of making it parse free text.
interface Suggestion {
    label: string;
    message?: string;
    action?: string;
    visual_type?: string;
    choice?: string;            // feedback_followup: "return" | "finish"
}

// One message of the conversation, exactly as the server returns it in "messages" arrays.
// `id` is a number that grows by one per message within the session; the visual uses it to avoid
// drawing the same message twice and to ask "give me everything after id N".
// `kind` changes the bubble style ("report", "error", "status", ...); `meta` carries extras such as
// suggestion buttons and, for a finished report, the request_id used for feedback.
interface ChatEntry {
    id: number;
    role: "user" | "assistant";
    text: string;
    kind?: string;
    meta?: { suggestions?: Suggestion[]; request_id?: string; view_name?: string; page_name?: string; chart_type?: string };
}

// One row of GET /reports/saved (saved_at / last_opened_at are UTC ISO strings without a zone).
interface SavedReport {
    request_id: string;
    title: string;
    visual_type?: string | null;
    saved_at?: string | null;
    last_opened_at?: string | null;
}

// Friendly names for the chart types in the "Saved reports" list (same labels as the backend).
const VISUAL_LABELS: Record<string, string> = {
    clusteredColumnChart: "Column chart", barChart: "Bar chart", lineChart: "Line chart",
    pieChart: "Pie chart", donutChart: "Donut chart", tableEx: "Table"
};

// What api() returns: ok = HTTP status 200-299, status = the HTTP status code, data = the parsed
// JSON body ({} if the body was empty or not JSON). `any` because each endpoint returns a different shape.
interface ApiResult {
    ok: boolean;
    status: number;
    data: any;
}

// Optional extras for send(); they are copied into the /chat request body (see Suggestion).
interface SendOptions {
    action?: string;
    visual_type?: string;
    choice?: string;
}

// The visual class. pbiviz.json names this class, and Power BI creates one instance per copy of
// the visual on a page. The IVisual lifecycle:
//   constructor(options) - called once when the visual is created: build the HTML here.
//   update(options)      - called right after, and again whenever size, data or formatting change.
//   destroy()            - called when Power BI removes the visual (page change, reload): stop timers.
export class Visual implements IVisual {
    // Power BI's local-storage service (null if the host did not provide one). Survives visual
    // re-creation, unlike normal fields. May still fail at runtime (see the note at the top).
    private storage: powerbi.extensibility.IVisualLocalStorageV2Service | null;
    // ---- HTML elements built in the constructor and reused later ----
    private chatArea: HTMLDivElement;           // the scrolling list of message bubbles
    private input: HTMLTextAreaElement;          // the text box the user types into
    private sendButton: HTMLButtonElement;
    private newChatButton: HTMLButtonElement;
    private endSessionButton: HTMLButtonElement;
    private demoRoleControl: HTMLLabelElement;  // "Demo role" label wrapping the dropdown (hidden unless demo mode)
    private demoRoleSelect: HTMLSelectElement;
    private statusText: HTMLDivElement;         // one line under the input for status / error messages
    private greetingEl: HTMLDivElement;         // the welcome bubble + example chips
    // "Saved reports": header button + a panel shown in place of the chat area while open.
    private savedReportsButton: HTMLButtonElement;
    private savedPanel: HTMLDivElement;
    private savedList: HTMLDivElement;
    private savedStatus: HTMLDivElement;
    private savedPanelOpen = false;

    // ---- Conversation state ----
    // The server-side conversation this visual belongs to; null until the first message is sent
    // (or until a previous session is restored).
    private sessionId: string | null = null;
    // Every message received from the server, in order (used to find the last message).
    private entries: ChatEntry[] = [];
    // message id -> its bubble element. "Already rendered?" check, so no message is drawn twice.
    private rendered = new Map<number, HTMLElement>();
    // User bubbles drawn immediately on send, before the server has confirmed them with an id.
    // applyEntries() matches them by text so they are not drawn a second time.
    private pendingUser: { el: HTMLElement; text: string }[] = [];
    // Highest message id seen so far; sent as ?after= so the server returns only newer messages.
    private lastId = 0;
    // True while a /chat request is in flight. Blocks a second send and disables the input.
    private isSending = false;
    // Set by destroy(). Async code checks it after each await so a dead visual stops working.
    private destroyed = false;
    // ---- Transcript polling (while the server says the session is "busy", e.g. during reload) ----
    // Timer ids from window.setInterval / setTimeout are numbers; null means "no timer running".
    private pollTimer: number | null = null;
    private pollStartedAt = 0;          // when polling began (Date.now()), to enforce POLL_MAX_MS
    private pollInFlight = false;       // true while one poll request is running; avoids overlapping requests
    // ---- Progress polling (while our own /chat request is running) ----
    private progressTimer: number | null = null;
    private progressInFlight = false;
    // ---- Two-click confirmation: a time (ms) until which a second click counts as "confirm" ----
    private newChatArmedUntil = 0;
    private endSessionArmedUntil = 0;
    // Not used anywhere in this file at the moment.
    private ratedRequests = new Set<string>();
    // True once the session has ended: input and buttons are disabled until "New chat".
    private sessionEnded = false;
    // The element Power BI gave us to draw in, and our chat wrapper inside it (hidden in feedback mode).
    private rootElement: HTMLElement;
    private chatContainer: HTMLDivElement;
    // Guards so the feedback panel / the session restore only happen once, although update()
    // is called many times.
    private feedbackRendered = false;
    private restoreStarted = false;
    // ---- Demo role state ----
    private demoRole = "CEO";                    // role sent to the backend as demo_role
    private demoRoleSelectionEnabled = false;    // true only when /health says demo mode is on
    private demoRoleFromStorage = false;    // false: demoRole is only the "CEO" default
    // A promise that finishes after the FIRST /health check. Code that needs to know the demo
    // mode (send, restore) does "await this.demoRoleReady" first. Later retries do not delay it.
    private demoRoleReady: Promise<void>;
    // True when a session was restored but we have no record of its role: then send() does NOT
    // send demo_role and the server keeps whatever role the session already has.
    private demoRolePinnedToSession = false;
    private demoRoleRetryTimer: number | null = null;   // pending /health retry (setTimeout id)
    private healthAttempts = 0;                          // /health attempt counter, only for log messages

    // constructor: Power BI calls this once when it creates the visual. It builds all the HTML
    // with document.createElement (no HTML templates), wires up click/keyboard handlers, and
    // starts the first /health check. It does NOT restore the session yet - that waits for the
    // first update(), because only update() tells us whether we are in chat or feedback mode.
    // Layout built here:  container > [header (title, role dropdown, End session, New chat),
    //                                  chatArea, composer (textarea + Send), statusText]
    constructor(options: VisualConstructorOptions) {
        // options.host gives access to Power BI services; "?." and "??" make this null-safe.
        this.storage = options?.host?.storageV2Service ?? null;
        // options.element is the empty <div> Power BI reserved for this visual.
        const root = options.element;
        this.rootElement = root;

        const container = document.createElement("div");
        container.className = "chatbot-container";
        this.chatContainer = container;

        const header = document.createElement("div");
        header.className = "chatbot-header";
        const title = document.createElement("div");
        title.className = "chatbot-title";
        title.textContent = "Billing AI Assistant";
        // Hover the title to see which build is loaded (must match "version" in pbiviz.json).
        title.title = "Billing AI Assistant (build 1.0.1.0)";
        this.newChatButton = document.createElement("button");
        this.newChatButton.type = "button";
        this.newChatButton.className = "new-chat-button";
        this.newChatButton.textContent = "New chat";
        // "() => void promise" calls an async method without waiting for it; "void" tells the linter
        // we are ignoring the returned promise on purpose.
        this.newChatButton.addEventListener("click", () => void this.onNewChatClick());
        this.endSessionButton = document.createElement("button");
        this.endSessionButton.type = "button";
        this.endSessionButton.className = "end-session-button";
        this.endSessionButton.textContent = "End session";
        this.endSessionButton.addEventListener("click", () => void this.onEndSessionClick());
        this.savedReportsButton = document.createElement("button");
        this.savedReportsButton.type = "button";
        this.savedReportsButton.className = "saved-reports-button";
        this.savedReportsButton.textContent = "Saved reports";
        this.savedReportsButton.addEventListener("click", () => void this.toggleSavedReports());
        // Demo role dropdown: created hidden; initializeDemoRoleSelector() shows it only if the
        // backend has demo mode turned on.
        const roleLabel = document.createElement("label");
        roleLabel.className = "demo-role-control";
        roleLabel.textContent = "Demo role";
        roleLabel.hidden = true;
        this.demoRoleSelect = document.createElement("select");
        this.demoRoleSelect.className = "demo-role-select";
        this.demoRoleSelect.setAttribute("aria-label", "Demo role");
        this.demoRoleSelect.hidden = true;
        DEMO_ROLES.forEach(role => {
            const option = document.createElement("option");
            option.value = role;
            option.textContent = role;
            this.demoRoleSelect.appendChild(option);
        });
        // When the user picks a role, remember it (it is sent with the next /chat request).
        this.demoRoleSelect.addEventListener("change", () => {
            this.demoRole = this.demoRoleSelect.value;
            logDebug("info", "Demo role selected", { role: this.demoRole });
            void this.saveDemoRole();
        });
        roleLabel.appendChild(this.demoRoleSelect);
        this.demoRoleControl = roleLabel;
        header.appendChild(title);
        const headerActions = document.createElement("div");
        headerActions.className = "header-actions";
        headerActions.appendChild(roleLabel);
        headerActions.appendChild(this.savedReportsButton);
        headerActions.appendChild(this.endSessionButton);
        headerActions.appendChild(this.newChatButton);
        header.appendChild(headerActions);

        // The message list. role="log" + aria-live let screen readers announce new messages.
        this.chatArea = document.createElement("div");
        this.chatArea.className = "chat-area";
        this.chatArea.setAttribute("role", "log");
        this.chatArea.setAttribute("aria-live", "polite");

        const composer = document.createElement("div");
        composer.className = "composer";
        this.input = document.createElement("textarea");
        this.input.className = "chat-input";
        this.input.rows = 1;
        this.input.placeholder = "Ask for a report, or ask about this one…";
        this.input.setAttribute("aria-label", "Message");
        // Enter sends; Shift+Enter makes a new line. isComposing is true while an IME (e.g. for
        // Chinese/Japanese input) is still building a character - Enter then must not send.
        this.input.addEventListener("keydown", event => {
            if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
                event.preventDefault();
                void this.send(this.input.value);
            }
        });
        // Grow the text box as the user types more lines.
        this.input.addEventListener("input", () => this.autoGrow());
        this.sendButton = document.createElement("button");
        this.sendButton.type = "button";
        this.sendButton.className = "send-button";
        this.sendButton.textContent = "Send";
        this.sendButton.addEventListener("click", () => void this.send(this.input.value));
        composer.appendChild(this.input);
        composer.appendChild(this.sendButton);

        this.statusText = document.createElement("div");
        this.statusText.className = "status-text";
        this.statusText.setAttribute("role", "status");

        // "Saved reports" panel: hidden until the header button is clicked (see toggleSavedReports).
        this.savedPanel = document.createElement("div");
        this.savedPanel.className = "saved-reports-panel";
        this.savedPanel.hidden = true;
        const savedHeading = document.createElement("div");
        savedHeading.className = "saved-reports-heading";
        const savedTitle = document.createElement("span");
        savedTitle.textContent = "Saved reports";
        const savedClose = document.createElement("button");
        savedClose.type = "button";
        savedClose.className = "saved-reports-close";
        savedClose.textContent = "Back to chat";
        savedClose.addEventListener("click", () => void this.toggleSavedReports(false));
        savedHeading.append(savedTitle, savedClose);
        this.savedList = document.createElement("div");
        this.savedList.className = "saved-reports-list";
        this.savedStatus = document.createElement("div");
        this.savedStatus.className = "saved-reports-status";
        this.savedStatus.setAttribute("role", "status");
        this.savedPanel.append(savedHeading, this.savedStatus, this.savedList);

        container.appendChild(header);
        container.appendChild(this.savedPanel);
        container.appendChild(this.chatArea);
        container.appendChild(composer);
        container.appendChild(this.statusText);
        root.appendChild(container);

        this.greetingEl = this.buildGreeting();
        this.chatArea.appendChild(this.greetingEl);
        // Start the /health check now (not awaited - a constructor cannot be async). The promise
        // is stored so later code can wait for it with "await this.demoRoleReady".
        this.demoRoleReady = this.initializeDemoRoleSelector();
        logDebug("info", `Visual constructed (API ${API_BASE}, storage ${this.storage ? "available" : "unavailable"})`);
    }

    // ------------------------------------------------------------------ lifecycle
    // update: Power BI calls this after the constructor and again on every resize, data change or
    // formatting change - so it can run many times. VisualUpdateOptions carries the current size
    // (viewport) and "dataViews". This visual has no data fields; it only reads the "objects"
    // part of dataViews, i.e. the saved values of the "feedback" properties from capabilities.json.
    // Decision made here:
    //   feedback.mode === "feedback" -> show the report-page feedback panel (once) and stop.
    //   feedback.mode === "save"     -> show only the "Save report" button (once) and stop.
    //   otherwise                    -> chat mode: restore the previous conversation (once).
    public update(options: VisualUpdateOptions): void {
        // Generated report pages host this same custom visual in feedback-only mode.
        // The request/session identifiers are embedded in the PBIR visual objects.
        // NOTE: Power BI passes static objects via dataViews[0].metadata even when
        // there is no dataViewMappings defined, so the chatbot renders immediately
        // without requiring any field to be dragged in.
        const feedbackObjs = options.dataViews?.[0]?.metadata?.objects as any;
        const feedback = feedbackObjs?.feedback;
        const mode = this.readObjectText(feedback?.mode);
        if (mode === "feedback" || mode === "save") {
            const requestId = this.readObjectText(feedback?.requestId);
            const sessionId = this.readObjectText(feedback?.sessionId);
            // Only once, and only if we know which report (requestId) the panel is for.
            if (requestId && !this.feedbackRendered) {
                this.feedbackRendered = true;
                logDebug("info", `Rendering report ${mode} mode`, { requestId, sessionId: sessionId || null });
                this.chatContainer.style.display = "none";
                if (mode === "save") this.renderSaveButton(requestId, sessionId || "");
                else this.renderReportFeedback(requestId, sessionId || "");
            }
            return;
        }
        // Chat mode: restore the conversation on the first update only (fire-and-forget async).
        if (!this.restoreStarted) {
            this.restoreStarted = true;
            logDebug("debug", "First update in chat mode; restoring conversation");
            void this.restoreConversation();
        }
    }

    // readObjectText: turns one "objects" property value into a plain string. Depending on how
    // the value was saved in the report (PBIR files), Power BI may hand it over as a plain string,
    // as { value: "..." }, or as an expression { expr: { Literal: { Value: "'abc'" } } } where the
    // text still has quote marks around it. The regex strips one leading/trailing ' or ".
    // Returns "" when nothing usable is found. Called by update().
    private readObjectText(value: any): string {
        if (typeof value === "string") return value.replace(/^['"]|['"]$/g, "");
        if (value && typeof value === "object") {
            if (typeof value.value === "string") return value.value.replace(/^['"]|['"]$/g, "");
            if (typeof value.expr?.Literal?.Value === "string") return value.expr.Literal.Value.replace(/^['"]|['"]$/g, "");
        }
        return "";
    }

    // renderReportFeedback: FEEDBACK MODE ONLY (called once by update()). Builds the small panel
    // shown on a generated report page:
    //   - a "Your report is ready / click Refresh" notice (new pages may show blank visuals until
    //     Power BI refreshes the data),
    //   - "Was this report useful?" with Satisfied / Unsatisfied buttons (POST /feedback),
    //   - after "Unsatisfied": "Return to conversation" / "Finish session" follow-up buttons.
    // requestId identifies the generated report; sessionId is the chat it came from ("" if unknown).
    // The little state variables below (currentSatisfaction, isSubmitting, followupContainer) live
    // in this function's closure: the button handlers created here share them.
    private renderReportFeedback(requestId: string, sessionId: string): void {
        const panel = document.createElement("div");
        panel.className = "report-feedback-panel";
        const refreshNotice = document.createElement("div");
        refreshNotice.className = "report-refresh-notice";

        const refreshHeading = document.createElement("div");
        refreshHeading.className = "report-refresh-heading";
        refreshHeading.textContent = "↻ Your report is ready";

        const refreshCopy = document.createElement("div");
        refreshCopy.className = "report-refresh-copy";
        refreshCopy.textContent =
            "If your chart or table is blank, Click Refresh in Power BI Desktop to load the report data.";

        const refreshHint = document.createElement("span");
        refreshHint.className = "report-refresh-hint";
        refreshHint.textContent = "Power BI Desktop  ·  Home → Refresh";

        refreshNotice.append(
            refreshHeading,
            refreshCopy,
            refreshHint
        );
        const title = document.createElement("div");
        title.className = "report-feedback-title";
        title.textContent = "Was this report useful?";
        const actions = document.createElement("div");
        actions.className = "report-feedback-actions";
        const yes = document.createElement("button");
        yes.type = "button"; yes.className = "feedback-button"; yes.textContent = "Satisfied";
        const no = document.createElement("button");
        no.type = "button"; no.className = "feedback-button"; no.textContent = "Unsatisfied";
        const status = document.createElement("div");
        status.className = "report-feedback-status";

        let currentSatisfaction: boolean | null = null;   // last answer saved on the server (null = none yet)
        let isSubmitting = false;                          // a /feedback request is in flight
        let followupContainer: HTMLDivElement | null = null; // the "Return / Finish" box, if shown

        // Removes the follow-up box (used when the answer changes to "Satisfied").
        const removeFollowup = () => {
            if (followupContainer) {
                followupContainer.remove();
                followupContainer = null;
            }
        };

        // Shows "Would you like to return to the conversation?" with two buttons, after "Unsatisfied".
        const showFollowup = () => {
            removeFollowup();
            followupContainer = document.createElement("div");
            followupContainer.className = "report-feedback-followup";
            const question = document.createElement("p");
            question.textContent = "Would you like to return to the conversation with the assistant to improve this report?";
            const followupActions = document.createElement("div");
            followupActions.className = "report-feedback-actions";

            const returnBtn = document.createElement("button");
            returnBtn.type = "button"; returnBtn.className = "feedback-button"; returnBtn.textContent = "Return to conversation";
            // "Return to conversation": tell the backend (POST /chat with action "feedback_followup",
            // choice "return") so the chat page has a message inviting the user to improve the
            // report. finishBtn is declared further down, but this handler only runs on a click,
            // by which time it exists.
            returnBtn.addEventListener("click", async () => {
                returnBtn.disabled = true;
                finishBtn.disabled = true;
                status.textContent = "Notifying assistant…";
                try {
                    if (sessionId) {
                        // Use the role stored for THIS session (if any) so the backend sees the same user.
                        await this.demoRoleReady;
                        const sessionRole = await this.readStoredDemoRole(sessionId, true);
                        if (sessionRole) {
                            this.demoRole = sessionRole;
                            await this.saveDemoRole();
                        }
                        const result = await this.api("/chat", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({
                                session_id: sessionId,
                                action: "feedback_followup",
                                choice: "return",
                                message: "Return to conversation",
                                demo_role: sessionRole || undefined
                            })
                        });
                        logDebug("info", "Feedback follow-up 'return' sent", { sessionId, requestId, status: result.status });
                    } else {
                        logDebug("warn", "Feedback follow-up 'return' skipped: no session id", { requestId });
                    }
                } catch (error) {
                    // api() throws on network errors / timeouts (not on HTTP errors).
                    /* ignore — navigation instruction is still shown */
                    logDebug("warn", "Feedback follow-up 'return' failed", error);
                }
                // Power BI custom visuals cannot trigger page navigation directly.
                // Show a clear instruction so the user knows what to do.
                status.textContent = "✅ Session preserved. Click the 'Back to AI Assistant' button at the bottom left " +
                    "(Ctrl+click while editing in Power BI Desktop), or the 'AI Assistant' page tab, to continue.";
            });

            const finishBtn = document.createElement("button");
            finishBtn.type = "button"; finishBtn.className = "feedback-button"; finishBtn.textContent = "Finish session";
            // "Finish session": ends the chat session on the server (see finishFeedbackSession).
            finishBtn.addEventListener("click", () => void this.finishFeedbackSession(sessionId, status, finishBtn, yes, no, returnBtn));

            followupActions.append(returnBtn, finishBtn);
            followupContainer.append(question, followupActions);
            panel.appendChild(followupContainer);
        };

        // Sends the Satisfied/Unsatisfied answer.
        //   Request:  POST /feedback { session_id?, request_id, satisfaction: "satisfied"|"unsatisfied", satisfied: bool }
        //   Response: 2xx on success; otherwise { error } or { message } explaining why.
        // The user may change their mind (click the other button later); clicking the same answer
        // twice, or clicking while a request is running, does nothing.
        const submitFeedback = async (satisfied: boolean): Promise<void> => {
            if (isSubmitting || currentSatisfaction === satisfied) return;
            isSubmitting = true;
            yes.disabled = true;
            no.disabled = true;
            status.textContent = "Recording feedback…";

            try {
                const result = await this.api("/feedback", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        session_id: sessionId || undefined,
                        request_id: requestId,
                        satisfaction: satisfied ? "satisfied" : "unsatisfied",
                        satisfied
                    })
                });
                // Turn an HTTP error into an exception so the catch block below handles both kinds of failure.
                if (!result.ok) throw new Error(result.data?.error || result.data?.message || "Feedback could not be saved.");
                logDebug("info", "Report feedback sent", { requestId, sessionId: sessionId || null, satisfied });

                currentSatisfaction = satisfied;
                const label = satisfied ? "Satisfied" : "Unsatisfied";

                // Highlight the chosen button and disable it; keep the other one clickable so the
                // answer can still be changed.
                if (satisfied) {
                    yes.classList.add("selected");
                    yes.disabled = true;
                    no.classList.remove("selected");
                    no.disabled = false;
                    removeFollowup();
                    status.textContent = `Feedback recorded: ${label}. Thank you!`;
                } else {
                    no.classList.add("selected");
                    no.disabled = true;
                    yes.classList.remove("selected");
                    yes.disabled = false;
                    status.textContent = `Feedback recorded: ${label}.`;
                    showFollowup();
                }
            } catch (error) {
                logDebug("error", "Report feedback failed", { requestId, satisfied, error });
                // Saving failed: put the buttons back the way they were for the last SAVED answer.
                if (currentSatisfaction === true) {
                    yes.classList.add("selected");
                    yes.disabled = true;
                    no.classList.remove("selected");
                    no.disabled = false;
                } else if (currentSatisfaction === false) {
                    no.classList.add("selected");
                    no.disabled = true;
                    yes.classList.remove("selected");
                    yes.disabled = false;
                } else {
                    yes.disabled = false;
                    no.disabled = false;
                }
                status.textContent = error instanceof Error ? error.message : "Feedback could not be saved.";
            } finally {
                isSubmitting = false;
            }
        };

        yes.addEventListener("click", () => void submitFeedback(true));
        no.addEventListener("click", () => void submitFeedback(false));
        actions.append(yes, no);
        panel.append(refreshNotice, title, actions, status);
        // Added directly to Power BI's element, next to the (now hidden) chat container.
        this.rootElement.appendChild(panel);
    }

    // finishFeedbackSession: the "Finish session" button on a report page (feedback mode).
    //   Request:  POST /session/<id>/end { reason: "finished_after_unsatisfied" }
    //   Response: 200 { ended: true, ... } when ended, 202 { pending: true } when a request is
    //             still running (it ends right after), 404 unknown session, 403 another user's.
    // 409 is also treated as fine (the comment in api() calls it "already ended"). The buttons are
    // passed in because they live inside renderReportFeedback(), not in class fields.
    private async finishFeedbackSession(sessionId: string, status: HTMLDivElement, finishBtn: HTMLButtonElement,
                                        yesBtn?: HTMLButtonElement, noBtn?: HTMLButtonElement, returnBtn?: HTMLButtonElement): Promise<void> {
        if (!sessionId) {
            logDebug("warn", "Finish session from report page skipped: no session id");
            status.textContent = "Session ID is unavailable; please end the session from the assistant page.";
            return;
        }
        finishBtn.disabled = true;
        if (returnBtn) returnBtn.disabled = true;
        if (yesBtn) yesBtn.disabled = true;
        if (noBtn) noBtn.disabled = true;
        status.textContent = "Finishing session…";

        try {
            const result = await this.api(`/session/${encodeURIComponent(sessionId)}/end`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ reason: "finished_after_unsatisfied" })
            });
            if (!result.ok && result.status !== 409) {
                throw new Error(result.data?.error || "Could not finish the session.");
            }
            logDebug("info", "Session finished from report page", { sessionId, status: result.status });
            status.textContent = "Session finished. Your conversation and report history are preserved.";
        } catch (error) {
            logDebug("error", "Finish session from report page failed", { sessionId, error });
            // Re-enable buttons so the user can try again (but keep an already-chosen answer disabled).
            finishBtn.disabled = false;
            if (returnBtn) returnBtn.disabled = false;
            if (yesBtn && yesBtn.classList.contains("selected")) yesBtn.disabled = true;
            if (noBtn && noBtn.classList.contains("selected")) noBtn.disabled = true;
            status.textContent = error instanceof Error ? error.message : "Could not finish the session.";
        }
    }

    // ------------------------------------------------------------------ saved reports
    // savedReportsRole: the demo role to send with the /reports/... calls (undefined when demo mode
    // is off). In demo mode the role is what identifies the owner of a saved report, so the
    // session's own stored role wins over "the last role used".
    private async savedReportsRole(sessionId?: string | null): Promise<string | undefined> {
        await this.demoRoleReady;
        if (!this.demoRoleSelectionEnabled) return undefined;
        if (sessionId) {
            const sessionRole = await this.readStoredDemoRole(sessionId);
            if (sessionRole) return sessionRole;
        }
        return this.demoRole;
    }

    // renderSaveButton: SAVE MODE ONLY (called once by update()). One button next to the native
    // "Back to AI Assistant" button on a generated page. The visual is only 60 px tall, so the
    // button's own label carries the state ("Saving…", "✓ Saved", "Retry save") and error details
    // go into its tooltip.
    //   On load:  GET  /reports/saved/<requestId>?demo_role=  -> { saved }  (show "✓ Saved" if so)
    //   On click: POST /reports/save { request_id, session_id, demo_role } -> 201 saved | 200 already_saved
    private renderSaveButton(requestId: string, sessionId: string): void {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "report-save-button";
        button.textContent = "Save report";
        button.title = "Keep this report so you can open it again later from 'Saved reports' on the assistant page";
        const markSaved = () => {
            button.textContent = "✓ Saved";
            button.title = "Saved. Open it any time from 'Saved reports' on the assistant page.";
            button.classList.add("saved");
            button.disabled = true;
        };

        button.addEventListener("click", async () => {
            if (button.disabled) return;
            button.disabled = true;
            button.textContent = "Saving…";
            try {
                const demoRole = await this.savedReportsRole(sessionId);
                // Saving first asks Power BI Desktop to save its pending edits, which can be slow.
                const result = await this.api("/reports/save", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ request_id: requestId, session_id: sessionId || undefined, demo_role: demoRole })
                }, CHAT_TIMEOUT_MS);
                if (!result.ok) throw new Error(result.data?.error || result.data?.message || "The report could not be saved.");
                logDebug("info", "Report saved", { requestId, status: result.data?.status ?? null });
                markSaved();
            } catch (error) {
                logDebug("error", "Saving the report failed", { requestId, error });
                button.textContent = "Retry save";
                button.title = error instanceof Error ? error.message : "The report could not be saved.";
                button.disabled = false;
            }
        });

        const wrapper = document.createElement("div");
        wrapper.className = "report-save-panel";
        wrapper.appendChild(button);
        this.rootElement.appendChild(wrapper);

        // Already saved earlier (or this is a restored page)? Then show it straight away.
        void (async () => {
            try {
                const demoRole = await this.savedReportsRole(sessionId);
                const query = demoRole ? `?demo_role=${encodeURIComponent(demoRole)}` : "";
                const result = await this.api(`/reports/saved/${encodeURIComponent(requestId)}${query}`);
                if (!this.destroyed && result.ok && result.data?.saved) markSaved();
            } catch (error) {
                logDebug("debug", "Could not check whether the report is saved", { requestId, error });
            }
        })();
    }

    // toggleSavedReports: the header's "Saved reports" button. While the panel is open it takes
    // the chat area's place (the composer stays usable); opening it always reloads the list.
    private async toggleSavedReports(open = !this.savedPanelOpen): Promise<void> {
        this.savedPanelOpen = open;
        this.savedPanel.hidden = !open;
        this.chatArea.hidden = open;
        this.savedReportsButton.classList.toggle("selected", open);
        if (open) await this.loadSavedReports();
        else this.scrollToBottom();
    }

    // loadSavedReports: GET /reports/saved?demo_role= -> { reports: SavedReport[] } (newest first).
    private async loadSavedReports(): Promise<void> {
        this.savedList.textContent = "";
        this.savedStatus.textContent = "Loading saved reports…";
        try {
            const demoRole = await this.savedReportsRole(this.sessionId);
            const query = demoRole ? `?demo_role=${encodeURIComponent(demoRole)}` : "";
            const result = await this.api(`/reports/saved${query}`);
            if (this.destroyed) return;
            if (!result.ok) throw new Error(result.data?.error || "Saved reports are unavailable right now.");
            const reports = (result.data?.reports ?? []) as SavedReport[];
            logDebug("info", "Saved reports loaded", { count: reports.length });
            this.savedStatus.textContent = reports.length ? "" :
                "No saved reports yet. Use the 'Save report' button on a generated report page.";
            reports.forEach(report => this.savedList.appendChild(this.buildSavedReportRow(report)));
        } catch (error) {
            logDebug("error", "Loading saved reports failed", error);
            this.savedStatus.textContent = error instanceof Error ? error.message : "Saved reports are unavailable right now.";
        }
    }

    // One list row: title, "Line chart · Saved 3 Nov 2026, 14:02", and Open / Delete buttons.
    // Delete uses the same two-click confirmation as "New chat" (dialogs are blocked in visuals).
    private buildSavedReportRow(report: SavedReport): HTMLDivElement {
        const row = document.createElement("div");
        row.className = "saved-report-row";
        const info = document.createElement("div");
        info.className = "saved-report-info";
        const title = document.createElement("div");
        title.className = "saved-report-title";
        title.textContent = report.title;
        const details = document.createElement("div");
        details.className = "saved-report-details";
        const chart = report.visual_type ? VISUAL_LABELS[report.visual_type] || "Chart" : "";
        details.textContent = [chart, report.saved_at ? `Saved ${this.formatUtc(report.saved_at)}` : ""]
            .filter(Boolean).join(" · ");
        info.append(title, details);

        const actions = document.createElement("div");
        actions.className = "saved-report-actions";
        const open = document.createElement("button");
        open.type = "button"; open.className = "saved-report-open"; open.textContent = "Open";
        const remove = document.createElement("button");
        remove.type = "button"; remove.className = "saved-report-delete"; remove.textContent = "Delete";
        let deleteArmedUntil = 0;
        open.addEventListener("click", () => void this.openSavedReport(report, open, remove));
        remove.addEventListener("click", () => {
            const now = Date.now();
            if (now > deleteArmedUntil) {
                deleteArmedUntil = now + 3500;
                remove.textContent = "Confirm delete?";
                remove.classList.add("confirming");
                window.setTimeout(() => {
                    if (Date.now() >= deleteArmedUntil) {
                        remove.textContent = "Delete";
                        remove.classList.remove("confirming");
                    }
                }, 3600);
                return;
            }
            void this.deleteSavedReport(report, row, open, remove);
        });
        actions.append(open, remove);
        row.append(info, actions);
        return row;
    }

    // openSavedReport: POST /reports/saved/<id>/open { session_id?, demo_role? }
    //   200 { status: "already_present" | "restored", message, page_name, busy?, messages? }
    // When the page had been removed the backend writes it back and reloads Power BI Desktop;
    // with our session id it also adds the message to this chat and marks the session busy, so
    // the usual transcript polling shows "Power BI has reloaded" afterwards.
    private async openSavedReport(report: SavedReport, open: HTMLButtonElement, remove: HTMLButtonElement): Promise<void> {
        open.disabled = true;
        remove.disabled = true;
        open.textContent = "Opening…";
        this.savedStatus.textContent = `Opening “${report.title}”…`;
        try {
            const demoRole = await this.savedReportsRole(this.sessionId);
            const sessionId = this.sessionEnded ? undefined : this.sessionId || undefined;
            const result = await this.api(`/reports/saved/${encodeURIComponent(report.request_id)}/open`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ session_id: sessionId, demo_role: demoRole })
            }, CHAT_TIMEOUT_MS);
            if (this.destroyed) return;
            if (!result.ok) throw new Error(result.data?.error || "The saved report could not be opened.");
            logDebug("info", "Saved report opened", { requestId: report.request_id, status: result.data?.status ?? null });
            await this.toggleSavedReports(false);
            const messages = (result.data?.messages ?? []) as ChatEntry[];
            if (messages.length && result.data?.session_id === this.sessionId) this.applyEntries(messages);
            else this.appendLocalNote(String(result.data?.message || "The saved report is ready."));
            if (result.data?.busy) this.startPolling();
        } catch (error) {
            logDebug("error", "Opening the saved report failed", { requestId: report.request_id, error });
            this.savedStatus.textContent = error instanceof Error ? error.message : "The saved report could not be opened.";
        } finally {
            open.disabled = false;
            remove.disabled = false;
            open.textContent = "Open";
        }
    }

    // deleteSavedReport: POST /reports/saved/<id>/delete { demo_role? } -> 200 { status: "deleted" }.
    // Only the saved copy is removed; a page that is still in the report stays.
    private async deleteSavedReport(report: SavedReport, row: HTMLDivElement, open: HTMLButtonElement,
                                    remove: HTMLButtonElement): Promise<void> {
        open.disabled = true;
        remove.disabled = true;
        try {
            const demoRole = await this.savedReportsRole(this.sessionId);
            const result = await this.api(`/reports/saved/${encodeURIComponent(report.request_id)}/delete`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ demo_role: demoRole })
            });
            if (!result.ok) throw new Error(result.data?.error || "The saved report could not be deleted.");
            logDebug("info", "Saved report deleted", { requestId: report.request_id });
            row.remove();
            if (!this.savedList.childElementCount) {
                this.savedStatus.textContent = "No saved reports yet. Use the 'Save report' button on a generated report page.";
            }
        } catch (error) {
            logDebug("error", "Deleting the saved report failed", { requestId: report.request_id, error });
            this.savedStatus.textContent = error instanceof Error ? error.message : "The saved report could not be deleted.";
            open.disabled = false;
            remove.disabled = false;
            remove.textContent = "Delete";
            remove.classList.remove("confirming");
        }
    }

    // formatUtc: "2026-11-03T08:32:00" (UTC from the backend) -> local "3 Nov 2026, 14:02".
    private formatUtc(value: string): string {
        const date = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : value + "Z");
        if (isNaN(date.getTime())) return value;
        return date.toLocaleString(undefined, { day: "numeric", month: "short", year: "numeric",
                                                hour: "2-digit", minute: "2-digit" });
    }

    // destroy: Power BI calls this right before it throws the visual away (page switch, reload).
    // Timers would otherwise keep firing and calling the backend from a dead visual, so stop them
    // all. Requests already in flight cannot be cancelled here; their code checks this.destroyed.
    public destroy(): void {
        logDebug("debug", "Visual destroyed", { sessionId: this.sessionId });
        this.destroyed = true;
        this.stopPolling();
        this.stopProgressPolling();
        if (this.demoRoleRetryTimer !== null) {
            window.clearTimeout(this.demoRoleRetryTimer);
            this.demoRoleRetryTimer = null;
        }
    }

    // ------------------------------------------------------------------ persistence
    // Power BI Desktop re-creates custom visuals when it reloads the project or when the user
    // navigates between pages. The conversation itself lives on the server; the visual only has
    // to remember its session id.
    //
    // About storageV2Service: it is Power BI's replacement for the browser's localStorage (which the
    // sandboxed iframe may not allow). It is async (get/set return promises), needs the
    // "LocalStorage" privilege, and may reject - e.g. when the key was never written, or when the
    // user/admin has turned custom-visual storage off. Hence a try/catch around every call.

    // readStoredSessionId: returns the saved session id, or null if there is none / storage fails.
    // Called by restoreConversation().
    private async readStoredSessionId(): Promise<string | null> {
        if (!this.storage) return null;
        try {
            const value = await this.storage.get(STORAGE_KEY);
            return typeof value === "string" && value ? value : null;
        } catch (error) {
            logDebug("debug", "No stored session id (storage get failed)", error);
            return null;   // nothing stored yet
        }
    }

    // initializeDemoRoleSelector: the startup /health check (started by the constructor).
    //   Request:  GET /health
    //   Response: { ok: true, mode, provider, model, dialect, demo_role_selection: bool }
    // If the backend is unreachable it schedules itself again every DEMO_ROLE_RETRY_MS until it
    // succeeds or the visual is destroyed. On success, and only if demo_role_selection is true,
    // it shows the role dropdown preselected with the saved role (or "CEO"). The dropdown is
    // disabled while a session exists, because a session's role must not change mid-conversation.
    private async initializeDemoRoleSelector(): Promise<void> {
        let health: ApiResult | null = null;
        const attempt = ++this.healthAttempts;
        try {
            health = await this.api("/health", undefined, QUICK_TIMEOUT_MS);
        } catch (error) {
            logDebug(attempt === 1 ? "warn" : "debug", `Health check attempt ${attempt} failed`, error);
            health = null;
        }
        // Stop if the visual is gone, or if an earlier attempt already turned demo mode on.
        if (this.destroyed || this.demoRoleSelectionEnabled) return;
        if (!health?.ok) {
            // Backend not reachable yet (e.g. started after Power BI loaded the visual): keep asking
            // in the background. Callers awaiting demoRoleReady only wait for this first attempt.
            logDebug(attempt === 1 ? "warn" : "debug",
                `Health check attempt ${attempt} not OK (status ${health ? health.status : "n/a"}); ` +
                `retrying in ${DEMO_ROLE_RETRY_MS} ms`);
            this.demoRoleRetryTimer = window.setTimeout(() => {
                this.demoRoleRetryTimer = null;
                void this.initializeDemoRoleSelector();
            }, DEMO_ROLE_RETRY_MS);
            return;
        }
        logDebug("info", `Health check attempt ${attempt} OK`, { demoRoleSelection: health.data?.demo_role_selection === true });
        try {
            if (health.data?.demo_role_selection !== true) return;
            this.demoRoleSelectionEnabled = true;
            const savedRole = await this.readStoredDemoRole();
            this.demoRoleFromStorage = !!savedRole;
            this.demoRole = savedRole || "CEO";
            this.demoRoleSelect.value = this.demoRole;
            this.demoRoleControl.hidden = false;
            this.demoRoleSelect.hidden = false;
            this.demoRoleSelect.disabled = !!this.sessionId;
            logDebug("info", "Demo role selector enabled", { role: this.demoRole, fromStorage: this.demoRoleFromStorage });
        } catch (error) {
            // Role selection stays hidden unless the backend explicitly enables demo mode.
            logDebug("warn", "Demo role selector setup failed", error);
        }
    }

    // saveDemoRole: stores the current demo role twice - as "the last role used" and, when a
    // session exists, under a per-session key ("<key>.<sessionId>"). The per-session copy lets a
    // re-created visual (or the report-page copy) know which role a given session belongs to.
    // Does nothing unless demo mode is on. Called on dropdown change, before each send, etc.
    private async saveDemoRole(): Promise<void> {
        if (!this.demoRoleSelectionEnabled || !this.storage) return;
        try {
            await this.storage.set(DEMO_ROLE_STORAGE_KEY, this.demoRole);
            if (this.sessionId) {
                await this.storage.set(`${DEMO_ROLE_STORAGE_KEY}.${this.sessionId}`, this.demoRole);
            }
        } catch (error) {
            /* role selection still applies to this visual instance */
            logDebug("warn", "Could not store the demo role", error);
        }
    }

    // readStoredDemoRole: reads a saved role back.
    //   sessionId given     -> first try that session's own role.
    //   sessionOnly = true  -> stop there (do not fall back to "the last role used").
    // Only values that are one of DEMO_ROLES are accepted; anything else counts as "nothing saved".
    // Returns the role, or null.
    private async readStoredDemoRole(sessionId?: string, sessionOnly = false): Promise<string | null> {
        if (!this.storage) return null;
        try {
            if (sessionId) {
                const sessionRole = await this.storage.get(`${DEMO_ROLE_STORAGE_KEY}.${sessionId}`);
                const validSessionRole = DEMO_ROLES.find(role => role === sessionRole);
                if (validSessionRole) return validSessionRole;
            }
            if (sessionOnly) return null;
            const saved = await this.storage.get(DEMO_ROLE_STORAGE_KEY);
            return DEMO_ROLES.find(role => role === saved) || null;
        } catch (error) {
            logDebug("debug", "No stored demo role (storage get failed)", { sessionId: sessionId || null, error });
            return null;
        }
    }

    // saveSessionId: remembers the current session id so a re-created visual can restore it.
    private async saveSessionId(): Promise<void> {
        if (!this.storage || !this.sessionId) return;
        try {
            await this.storage.set(STORAGE_KEY, this.sessionId);
        } catch (error) {
            logDebug("warn", "Could not store the chat session id", { sessionId: this.sessionId, error });
        }
    }

    // clearStoredSessionId: forgets the saved session id (session ended, gone, or "New chat").
    // remove() returns a promise that is not awaited here, so the try/catch only catches errors
    // thrown synchronously.
    private clearStoredSessionId(): void {
        logDebug("debug", "Clearing stored session id");
        try {
            this.storage?.remove(STORAGE_KEY);
        } catch (error) {
            /* ignore */
            logDebug("debug", "Could not clear the stored session id", error);
        }
    }

    // restoreConversation: CHAT MODE, run once from the first update(). Finds the session to
    // continue and redraws its messages. Steps:
    //   1. Wait for the first /health check (we need to know whether demo mode is on).
    //   2. Pick a session id, in this order:
    //        a) the id saved in local storage;
    //        b) demo mode with no saved role: the newest session over ALL roles
    //           (findLatestSessionAcrossRoles);
    //        c) otherwise GET /session/latest (filtered by the current role in demo mode).
    //           Needed because local storage may be per visual instance and so empty.
    //   3. In demo mode, recover that session's role (or "pin" to it if unknown).
    //   4. GET /session/<id>/transcript?after=0 -> { session_id, busy, ended, end_requested,
    //      last_id, messages[] } and draw all messages.
    //   5. If the session is still busy (e.g. Power BI is reloading for it), start polling.
    // "fromFallback" = the id came from the server (b/c), not storage; then a vanished session is
    // not worth telling the user about.
    private async restoreConversation(): Promise<void> {
        await this.demoRoleReady;
        let id = await this.readStoredSessionId();
        let fromFallback = false;
        if (id) logDebug("info", "Restoring session from local storage", { sessionId: id });
        if (!id && this.demoRoleSelectionEnabled && !this.demoRoleFromStorage) {
            // No saved role either: the "CEO" default is a guess, so look for the newest session
            // of every demo role instead of only CEO's.
            const found = await this.findLatestSessionAcrossRoles();
            // The visual may have been destroyed while we were waiting; then do nothing more.
            if (this.destroyed) return;
            // A string result is an explanation message, not a session (see that method).
            if (typeof found === "string") {
                logDebug("warn", "Session restore across demo roles not possible", { reason: found });
                this.setStatus(found);          // nothing restored; say why instead of guessing
                return;
            }
            if (found) {
                id = found.sessionId;
                fromFallback = true;
                this.demoRole = found.role;
                this.demoRoleSelect.value = found.role;
                logDebug("info", "Restoring newest session across demo roles", { sessionId: id, role: found.role });
            } else {
                logDebug("info", "No previous session found for any demo role");
            }
        } else if (!id) {
            // Local storage can be scoped to a visual instance. Restore the most recent session
            // from the backend when returning from a report page; demo mode filters by role.
            try {
                const roleQuery = this.demoRoleSelectionEnabled
                    ? `?demo_role=${encodeURIComponent(this.demoRole)}` : "";
                // Response: 200 { session_id, last_active } or 404 { error } when there is none.
                const latest = await this.api(`/session/latest${roleQuery}`, undefined, QUICK_TIMEOUT_MS);
                if (latest.ok && latest.data?.session_id) {
                    id = latest.data.session_id as string;
                    fromFallback = true;
                    logDebug("info", "Restoring session from /session/latest", { sessionId: id });
                } else {
                    logDebug("info", "No session restored from /session/latest", { status: latest.status });
                }
            } catch (error) {
                /* backend not reachable yet */
                logDebug("warn", "Could not query /session/latest during restore", error);
            }
        }
        if (!id || this.destroyed) return;

        if (this.demoRoleSelectionEnabled) {
            const sessionRole = await this.readStoredDemoRole(id, true);
            if (sessionRole) {
                this.demoRole = sessionRole;
                this.demoRoleSelect.value = this.demoRole;
            } else {
                // Role of this session unknown here: stop sending demo_role so we cannot
                // accidentally send the wrong one; the server keeps the session's own role.
                this.demoRolePinnedToSession = true;
            }
            logDebug("debug", "Restore demo role", { sessionId: id, role: sessionRole, pinnedToSession: !sessionRole });
        }

        try {
            const result = await this.api(`/session/${encodeURIComponent(id)}/transcript?after=0`, undefined, QUICK_TIMEOUT_MS);
            if (this.destroyed) return;
            // 404 = the server no longer knows this session (e.g. the backend was restarted).
            if (result.status === 404) {
                logDebug("info", "Restored session no longer exists on the server", { sessionId: id, fromFallback });
                this.clearStoredSessionId();
                if (!fromFallback) {
                    this.appendLocalNote("I've started a fresh conversation; the previous one is no longer available.");
                }
                return;
            }
            if (!result.ok) {
                logDebug("warn", "Session restore transcript request not OK", { sessionId: id, status: result.status });
                return;
            }
            this.sessionId = id;
            this.demoRoleSelect.disabled = true;
            const messages = (result.data.messages ?? []) as ChatEntry[];
            logDebug("info", "Session restored", {
                sessionId: id, fromFallback, messageCount: messages.length,
                ended: !!result.data.ended, busy: !!result.data.busy
            });
            this.applyEntries(messages);
            // An ended session is shown read-only: history visible, input disabled.
            if (result.data.ended) {
                this.clearStoredSessionId();
                this.disableSession();
                return;
            }
            void this.saveSessionId();
            if (result.data.busy) this.startPolling();
        } catch (error) {
            // Only network errors / timeouts reach here (api() does not throw for HTTP errors).
            logDebug("error", "Session restore failed", { sessionId: id, error });
            this.setStatus("Can't reach the assistant service. Is the Flask backend running?");
        }
    }

    // The newest active session over all demo roles: { sessionId, role }, null when no role has
    // one, or a status message when it cannot be decided reliably (nothing is restored then).
    private async findLatestSessionAcrossRoles(): Promise<{ sessionId: string; role: string } | null | string> {
        // Called only by restoreConversation(). Asks GET /session/latest?demo_role=<role> once per
        // role (one after another), collects the answers, then picks the one with the largest
        // last_active (epoch seconds of its last message). Any failure aborts with a message.
        const found: { sessionId: string; role: string; lastActive: number | null }[] = [];
        for (const role of DEMO_ROLES) {
            let latest: ApiResult;
            try {
                latest = await this.api(`/session/latest?demo_role=${encodeURIComponent(role)}`,
                                        undefined, QUICK_TIMEOUT_MS);
            } catch (error) {
                logDebug("warn", "Could not query /session/latest", { role, error });
                return "Can't reach the assistant service to restore your previous conversation.";
            }
            if (latest.status === 404) continue;                 // no session for this role
            const sessionId = latest.data?.session_id;
            if (!latest.ok || typeof sessionId !== "string" || !sessionId) {
                logDebug("warn", "Unexpected /session/latest response", { role, status: latest.status });
                return "Couldn't restore your previous conversation. Start a new chat to continue.";
            }
            const lastActive = latest.data?.last_active;
            found.push({ sessionId, role,
                         lastActive: typeof lastActive === "number" && isFinite(lastActive) ? lastActive : null });
        }
        if (found.length <= 1) return found[0] ?? null;          // nothing to compare
        const times = found.map(f => f.lastActive);
        if (times.some(t => t === null) || new Set(times).size !== times.length) {
            // Missing or equal timestamps: do not guess which conversation is the newest.
            return "Several recent conversations were found, but the newest one can't be determined. " +
                   "Send a message to start a new chat.";
        }
        // Sort newest first (b - a = descending) and return the first.
        found.sort((a, b) => (b.lastActive as number) - (a.lastActive as number));
        return found[0];
    }

    // ------------------------------------------------------------------ HTTP
    // api: the ONE place that talks to the backend. Every request in this file goes through it.
    //   path      - e.g. "/chat" (API_BASE is put in front)
    //   init      - normal fetch() options: method, headers, body (undefined = simple GET)
    //   timeoutMs - give up after this long (fetch has no timeout of its own)
    // Returns { ok, status, data } for ANY HTTP answer, including 404/500 - callers check `ok`.
    // Throws only when no answer arrived at all (backend down, CORS/network error, or timeout),
    // which is why callers wrap it in try/catch.
    private async api(path: string, init?: RequestInit, timeoutMs = QUICK_TIMEOUT_MS): Promise<ApiResult> {
        // An AbortController can cancel a running fetch. The timer calls abort() after timeoutMs;
        // fetch then rejects with an "AbortError".
        const controller = new AbortController();
        const timer = window.setTimeout(() => controller.abort(), timeoutMs);
        const method = init?.method || "GET";
        try {
            let response: Response;
            try {
                response = await fetch(API_BASE + path, { ...init, signal: controller.signal });
            } catch (error) {
                const aborted = controller.signal.aborted;
                logDebug("warn", `${method} ${path} ${aborted ? `timed out after ${timeoutMs} ms` : "network error"}`, error);
                // Log, then re-throw: the caller decides what to show the user.
                throw error;
            }
            // Parse the JSON body; keep {} if it is empty or not JSON (e.g. an HTML error page).
            let data: any = {};
            try {
                data = await response.json();
            } catch (error) {
                /* empty or non-JSON body */
                logDebug("debug", `${method} ${path} returned an empty or non-JSON body (HTTP ${response.status})`, error);
            }
            if (!response.ok) {
                // 404/409 are expected answers for some endpoints (no session, already ended).
                logDebug(response.status === 404 || response.status === 409 ? "info" : "warn",
                    `${method} ${path} -> HTTP ${response.status}`);
            }
            return { ok: response.ok, status: response.status, data };
        } finally {
            // Always cancel the timeout timer, whether we returned or threw.
            window.clearTimeout(timer);
        }
    }

    // ------------------------------------------------------------------ sending
    // send: sends one user message to the assistant. Called by Enter/Send, greeting chips and
    // suggestion chips (the chips pass `options` such as action/visual_type/choice).
    // Flow:
    //   1. Draw the user's bubble at once (kept in pendingUser until the server confirms it).
    //   2. Create a session id if this is the first message, and store it.
    //   3. Show a "typing" bubble and start polling /progress to update its label.
    //   4. POST /chat and wait (up to CHAT_TIMEOUT_MS - report generation is slow).
    //        Request:  { session_id, message, visual_type ("auto" by default), action?, choice?, demo_role? }
    //        Response: { session_id, reply, intent, messages: ChatEntry[] (only the NEW ones),
    //                    busy, session_ended, request_id?, ... }  (error: { status:"error", message })
    //   5. Draw the new messages; if busy, start transcript polling.
    // Errors are caught and shown as an error bubble. "finally" always re-enables the input.
    private async send(rawText: string, options: SendOptions = {}): Promise<void> {
        await this.demoRoleReady;
        const text = (rawText || "").trim();
        if (!text || this.isSending) return;
        if (this.sessionEnded) {
            logDebug("info", "Send blocked: session has ended", { sessionId: this.sessionId });
            this.setStatus("This session has ended. Click 'New chat' to start a new session.");
            return;
        }

        this.hideGreetingChips();
        const bubble = this.appendBubble("user", text);
        this.pendingUser.push({ el: bubble, text });
        this.input.value = "";
        this.autoGrow();
        this.setSending(true);
        // Create the session id here (not on the server) so live progress can be polled even
        // while the very first message is still being processed.
        if (!this.sessionId) {
            this.sessionId = this.newSessionId();
            logDebug("info", "New session created", { sessionId: this.sessionId, role: this.demoRole });
            this.demoRoleSelect.disabled = true;
            void this.saveSessionId();
        }
        await this.saveDemoRole();
        const sentAt = Date.now();
        logDebug("info", "Chat request sent", {
            sessionId: this.sessionId, action: options.action ?? null, visualType: options.visual_type ?? "auto",
            choice: options.choice ?? null, messageLength: text.length
        });
        const typing = this.showTyping();
        // Picking a chart type is what starts the long report build, so say so up front.
        if (options.action === "choose_chart" || this.lastMessageAsksForChart()) {
            this.setTypingLabel(typing, "Report generation in progress…");
        }
        this.startProgressPolling(typing);
        this.setStatus("");

        try {
            const result = await this.api("/chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    session_id: this.sessionId,
                    message: text,
                    visual_type: options.visual_type ?? "auto",
                    action: options.action,
                    choice: options.choice,
                    // undefined fields are left out by JSON.stringify.
                    demo_role: this.demoRolePinnedToSession ? undefined : this.demoRole
                })
            }, CHAT_TIMEOUT_MS);

            typing.remove();
            // Safety net: if the server ever answers with a different session id, adopt and store
            // it. (Today the server reuses the id we send, creating the session if it is new.)
            if (result.data?.session_id && result.data.session_id !== this.sessionId) {
                logDebug("info", "Server replaced the session id", { from: this.sessionId, to: result.data.session_id });
                this.sessionId = result.data.session_id as string;
                void this.saveSessionId();
            }

            const messages = (result.data?.messages ?? []) as ChatEntry[];
            const last = messages[messages.length - 1];
            logDebug(result.ok ? "info" : "warn", "Chat response received", {
                sessionId: this.sessionId, status: result.status, ms: Date.now() - sentAt,
                intent: result.data?.intent ?? null, busy: !!result.data?.busy, messageCount: messages.length,
                lastKind: last?.kind ?? null, requestId: last?.meta?.request_id ?? null
            });
            if (messages.length) {
                this.applyEntries(messages);
            } else {
                // No messages (e.g. an HTTP error with only { message } or { error }): show what we got.
                const fallback = result.data?.message || result.data?.error ||
                    `The assistant returned an unexpected response (HTTP ${result.status}).`;
                this.appendBubble("assistant", String(fallback), "error");
            }
            // busy = the server is still working for this session (e.g. reloading Power BI): poll.
            if (result.data?.busy) this.startPolling();
        } catch (error) {
            // Network error or timeout (a reload of Power BI can also cut the request off).
            typing.remove();
            logDebug("error", "Chat request failed", { sessionId: this.sessionId, ms: Date.now() - sentAt, error });
            this.appendBubble("assistant",
                "I couldn't reach the assistant service. Please check that the Flask backend is running and try again.",
                "error");
            // The server may have finished the request anyway (e.g. Power BI reloaded mid-request).
            window.setTimeout(() => void this.syncFromServer(), 1500);
        } finally {
            this.stopProgressPolling();
            this.setSending(false);
            this.input.focus();
        }
    }

    // newSessionId: makes a random id with the browser's secure random generator. Each of the 12
    // random bytes is cut to its low 4 bits (& 0x0f) and written as one hex digit, so the result
    // is 12 hex characters, e.g. "3fa09c1e7b2d".
    private newSessionId(): string {
        const randomValues = new Uint8Array(12);
        window.crypto.getRandomValues(randomValues);
        return Array.from(randomValues, value => (value & 0x0f).toString(16)).join("");
    }

    // lastMessageAsksForChart: true if the newest message is from the assistant and offers a
    // "choose_chart" suggestion - i.e. whatever the user sends now will start building a report.
    private lastMessageAsksForChart(): boolean {
        const last = this.entries[this.entries.length - 1];
        return !!last && last.role === "assistant" &&
            !!last.meta?.suggestions?.some(s => s.action === "choose_chart");
    }

    // ------------------------------------------------------------------ live progress while /chat runs
    // While /chat is running (it can take minutes) we ask every PROGRESS_INTERVAL_MS what the
    // server is doing and show it in the typing bubble ("Writing SQL…", etc.). This works only
    // because the session id is created on the client before /chat is sent (see send()).
    // startProgressPolling: starts the repeating timer (stopping any old one first).
    private startProgressPolling(typing: HTMLDivElement): void {
        this.stopProgressPolling();
        logDebug("debug", "Progress polling started", { sessionId: this.sessionId });
        this.progressTimer = window.setInterval(() => void this.pollProgress(typing), PROGRESS_INTERVAL_MS);
    }

    // stopProgressPolling: stops that timer. Safe to call when none is running.
    private stopProgressPolling(): void {
        if (this.progressTimer !== null) {
            window.clearInterval(this.progressTimer);
            this.progressTimer = null;
            logDebug("debug", "Progress polling stopped", { sessionId: this.sessionId });
        }
    }

    // pollProgress: one progress check, run by the timer.
    //   Request:  GET /session/<id>/progress   (5 s timeout)
    //   Response: { progress: "text", busy, ended }   (404 if the server does not know the session yet)
    // progressInFlight skips a tick if the previous request has not returned yet.
    private async pollProgress(typing: HTMLDivElement): Promise<void> {
        if (!this.sessionId || this.progressInFlight) return;
        this.progressInFlight = true;
        try {
            const result = await this.api(`/session/${encodeURIComponent(this.sessionId)}/progress`, undefined, 5_000);
            const text = result.data?.progress;
            if (result.ok && typeof text === "string" && text) this.setTypingLabel(typing, text);
        } catch (error) {
            /* progress is best-effort */
            logDebug("debug", "Progress poll failed", { sessionId: this.sessionId, error });
        } finally {
            this.progressInFlight = false;
        }
    }

    // setTypingLabel: changes the text next to the animated dots in the typing bubble.
    private setTypingLabel(typing: HTMLDivElement, text: string): void {
        const label = typing.querySelector(".typing-label");
        if (label) label.textContent = text;
    }

    // syncFromServer: called 1.5 s after a /chat request failed. The server may have finished
    // the work anyway, so fetch any messages we missed (GET /transcript?after=lastId) and
    // start polling if the session is still busy.
    private async syncFromServer(): Promise<void> {
        if (!this.sessionId || this.destroyed) return;
        try {
            const result = await this.api(
                `/session/${encodeURIComponent(this.sessionId)}/transcript?after=${this.lastId}`);
            if (result.ok) {
                const messages = (result.data.messages ?? []) as ChatEntry[];
                logDebug("info", "Synced from server after failed chat", {
                    sessionId: this.sessionId, newMessages: messages.length, busy: !!result.data.busy
                });
                this.applyEntries(messages);
                if (result.data.busy) this.startPolling();
            }
        } catch (error) {
            /* still offline */
            logDebug("warn", "Sync from server failed (still offline?)", { sessionId: this.sessionId, error });
        }
    }

    // ------------------------------------------------------------------ polling (background Power BI reload)
    // Transcript polling: after /chat returns, the server can still be "busy" for this session
    // (typically reloading Power BI Desktop to show the new page) and may add more messages
    // later. So every POLL_INTERVAL_MS we fetch new messages, until busy is false or POLL_MAX_MS
    // has passed. Note the reload usually destroys this visual; the new instance then restores
    // the session and, if still busy, starts polling again.
    // startPolling: starts the timer (no-op if already polling or destroyed).
    private startPolling(): void {
        if (this.pollTimer !== null || this.destroyed) return;
        this.pollStartedAt = Date.now();
        logDebug("info", "Transcript polling started (session busy)", { sessionId: this.sessionId, afterId: this.lastId });
        this.setStatus("Power BI is updating the report…");
        this.pollTimer = window.setInterval(() => void this.pollOnce(), POLL_INTERVAL_MS);
    }

    // stopPolling: stops the timer and clears the status line (unless a send is in progress).
    private stopPolling(): void {
        if (this.pollTimer !== null) {
            window.clearInterval(this.pollTimer);
            this.pollTimer = null;
            logDebug("info", "Transcript polling stopped", {
                sessionId: this.sessionId, afterMs: Date.now() - this.pollStartedAt
            });
        }
        if (!this.isSending) this.setStatus("");
    }

    // pollOnce: one tick of transcript polling.
    //   Request:  GET /session/<id>/transcript?after=<lastId>
    //   Response: { session_id, busy, ended, end_requested, last_id, messages: ChatEntry[] }
    // Errors are ignored (Power BI may be mid-reload); the deadline check below still runs.
    private async pollOnce(): Promise<void> {
        if (this.pollInFlight || !this.sessionId) return;
        this.pollInFlight = true;
        try {
            const result = await this.api(
                `/session/${encodeURIComponent(this.sessionId)}/transcript?after=${this.lastId}`);
            if (result.ok) {
                const messages = (result.data.messages ?? []) as ChatEntry[];
                if (messages.length) {
                    logDebug("debug", "Transcript poll received new messages", { sessionId: this.sessionId, count: messages.length });
                }
                this.applyEntries(messages);
                if (!result.data.busy) {
                    logDebug("debug", "Session no longer busy", { sessionId: this.sessionId });
                    this.stopPolling();
                }
            }
        } catch (error) {
            /* keep trying until the deadline */
            logDebug("debug", "Transcript poll failed", { sessionId: this.sessionId, error });
        } finally {
            this.pollInFlight = false;
        }
        // Give up after POLL_MAX_MS so a stuck "busy" flag cannot make us poll forever.
        if (Date.now() - this.pollStartedAt > POLL_MAX_MS) {
            if (this.pollTimer !== null) {
                logDebug("warn", `Transcript polling gave up after ${POLL_MAX_MS} ms`, { sessionId: this.sessionId });
            }
            this.stopPolling();
        }
    }

    // ------------------------------------------------------------------ new chat
    // onNewChatClick: the "New chat" button. First click only "arms" it for 3.5 s and changes
    // the label to "Click again to confirm"; a second click within that window really starts a
    // new chat: it ends the old session on the server (POST /session/<id>/reset), forgets the
    // stored id and clears the chat area back to the greeting. The new session id is only
    // created when the next message is sent.
    private async onNewChatClick(): Promise<void> {
        const now = Date.now();
        if (now > this.newChatArmedUntil) {
            // Two-step confirm (dialogs are blocked inside Power BI visuals).
            this.newChatArmedUntil = now + 3500;
            this.newChatButton.textContent = "Click again to confirm";
            // Put the label back after the window closes (unless the button was re-armed meanwhile).
            window.setTimeout(() => {
                if (Date.now() >= this.newChatArmedUntil) this.newChatButton.textContent = "New chat";
            }, 3600);
            return;
        }
        this.newChatArmedUntil = 0;
        this.newChatButton.textContent = "New chat";
        logDebug("info", "New chat confirmed", { previousSessionId: this.sessionId, previousEnded: this.sessionEnded });
        this.stopPolling();
        const id = this.sessionId;
        this.sessionId = null;
        this.demoRolePinnedToSession = false;
        this.demoRoleSelect.disabled = false;
        this.clearStoredSessionId();
        // Tell the server only if there was a live session. Response: { ok: true, ended, ... }.
        if (id && !this.sessionEnded) {
            try {
                const result = await this.api(`/session/${encodeURIComponent(id)}/reset`, { method: "POST" });
                logDebug("debug", "Previous session reset", { sessionId: id, status: result.status });
            } catch (error) {
                /* the old session simply expires on the server */
                logDebug("warn", "Could not reset the previous session", { sessionId: id, error });
            }
        }
        // Reset all local conversation state and UI back to "fresh visual".
        this.sessionEnded = false;
        this.entries = [];
        this.rendered.clear();
        this.pendingUser = [];
        this.lastId = 0;
        this.chatArea.textContent = "";
        this.greetingEl = this.buildGreeting();
        this.chatArea.appendChild(this.greetingEl);
        this.setStatus("");
        this.input.disabled = false;
        this.input.placeholder = "Ask for a report, or ask about this one…";
        this.sendButton.disabled = false;
        this.endSessionButton.disabled = false;
        this.endSessionButton.textContent = "End session";
        this.endSessionButton.classList.remove("confirming");
        this.input.focus();
    }

    // onEndSessionClick: the "End session" button, with the same two-click confirmation as
    // "New chat" (first click shows "Confirm End?"); the confirmed click calls endSession().
    private async onEndSessionClick(): Promise<void> {
        if (this.sessionEnded) return;
        const now = Date.now();
        if (now > this.endSessionArmedUntil) {
            this.endSessionArmedUntil = now + 3500;
            this.endSessionButton.textContent = "Confirm End?";
            this.endSessionButton.classList.add("confirming");
            window.setTimeout(() => {
                if (Date.now() >= this.endSessionArmedUntil && !this.sessionEnded) {
                    this.endSessionButton.textContent = "End session";
                    this.endSessionButton.classList.remove("confirming");
                }
            }, 3600);
            return;
        }
        this.endSessionArmedUntil = 0;
        this.endSessionButton.textContent = "End session";
        this.endSessionButton.classList.remove("confirming");
        await this.endSession();
    }

    // endSession: ends the current session for good (chat mode). Used by "End session" and by the
    // "Finish session" button under an unsatisfied report bubble.
    //   Request:  POST /session/<id>/end { reason: "user" }
    //   Response: 200 { ended: true, session_id, messages? }, or 202 { pending: true, message }
    //             when a request is still running on the server (it ends right after).
    // Refuses while our own /chat request is in flight. On success it stops all polling, forgets
    // the stored id, adds an "ended" note and disables the input (history stays visible).
    private async endSession(): Promise<void> {
        if (this.isSending) {
            logDebug("info", "End session deferred: a request is in flight", { sessionId: this.sessionId });
            this.setStatus("Please wait for the current request to finish before ending the session.");
            return;
        }
        const id = this.sessionId;
        if (id) {
            try {
                const result = await this.api(`/session/${encodeURIComponent(id)}/end`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ reason: "user" })
                });
                if (!result.ok && result.status !== 409) {
                    logDebug("warn", "End session rejected", { sessionId: id, status: result.status });
                    this.setStatus(result.data?.error || "Could not end the session.");
                    return;
                }
                logDebug("info", "Session ended", { sessionId: id, status: result.status });
            } catch (error) {
                logDebug("error", "End session request failed", { sessionId: id, error });
                this.setStatus("Could not reach the assistant to end the session.");
                return;
            }
        } else {
            logDebug("info", "Session ended locally (no session id yet)");
        }
        this.stopPolling();
        this.stopProgressPolling();
        this.clearStoredSessionId();

        const note = document.createElement("div");
        note.className = "bubble assistant status";
        note.textContent = "Session ended. Your conversation history and generated reports are preserved.";
        this.chatArea.appendChild(note);
        this.scrollToBottom();
        this.setStatus("");
        this.disableSession();
    }

    // disableSession: puts the UI in "session ended" state - input, Send, End session and every
    // button inside the chat (chips, feedback buttons) are disabled. Only "New chat" still works.
    private disableSession(): void {
        this.sessionEnded = true;
        this.input.disabled = true;
        this.input.value = "";
        this.input.placeholder = "Session ended. Further interaction in this session is disabled.";
        this.sendButton.disabled = true;
        this.endSessionButton.disabled = true;
        this.endSessionButton.textContent = "Session ended";
        this.endSessionButton.classList.remove("confirming");
        this.chatArea.querySelectorAll("button").forEach(b => (b.disabled = true));
    }

    // ------------------------------------------------------------------ rendering
    // buildGreeting: builds (does not insert) the welcome bubble plus one chip per EXAMPLES entry.
    // Used by the constructor and by "New chat".
    private buildGreeting(): HTMLDivElement {
        const wrapper = document.createElement("div");
        wrapper.className = "greeting";
        const bubble = document.createElement("div");
        bubble.className = "bubble assistant";
        this.renderRich(bubble, GREETING);
        wrapper.appendChild(bubble);
        const chips = document.createElement("div");
        chips.className = "chip-row greeting-chips";
        EXAMPLES.forEach(example => {
            chips.appendChild(this.makeChip(example, () => void this.send(example)));
        });
        wrapper.appendChild(chips);
        return wrapper;
    }

    // hideGreetingChips: removes the example chips once the conversation has started (the
    // greeting text itself stays).
    private hideGreetingChips(): void {
        this.greetingEl.querySelectorAll(".greeting-chips").forEach(el => el.remove());
    }

    // appendLocalNote: a grey "status" bubble created by the visual itself (not from the server).
    private appendLocalNote(text: string): void {
        this.appendBubble("assistant", text, "status");
    }

    // applyEntries: the single place where server messages become bubbles. Called with the
    // "messages" array from /chat, /transcript (restore, polling, sync).
    //   - Skips messages already drawn (same id), so overlapping responses are harmless.
    //   - A user message whose text matches a bubble we drew optimistically in send() reuses
    //     that bubble instead of drawing a duplicate.
    //   - Afterwards the suggestion chips are moved to the newest message.
    private applyEntries(incoming: ChatEntry[]): void {
        let changed = false;
        for (const entry of incoming) {
            if (this.rendered.has(entry.id)) continue;
            this.lastId = Math.max(this.lastId, entry.id);
            changed = true;
            this.hideGreetingChips();
            this.entries.push(entry);

            if (entry.role === "user") {
                const index = this.pendingUser.findIndex(p => p.text === entry.text);
                if (index >= 0) {
                    // splice removes the pending item from the list and returns it; we keep its element.
                    this.rendered.set(entry.id, this.pendingUser.splice(index, 1)[0].el);
                    continue;
                }
            }
            this.rendered.set(entry.id, this.appendBubble(entry.role, entry.text, entry.kind, entry.meta));
        }
        if (changed) this.refreshSuggestions();
    }

    // appendBubble: adds one message bubble at the bottom of the chat and returns it. The CSS
    // classes are "bubble user|assistant" plus the kind (e.g. "error", "report") for styling.
    // A finished report (kind "report" with a request_id) also gets Satisfied/Unsatisfied buttons.
    private appendBubble(role: "user" | "assistant", text: string, kind?: string, meta?: ChatEntry["meta"]): HTMLDivElement {
        const bubble = document.createElement("div");
        bubble.className = `bubble ${role}` + (kind && kind !== "text" ? ` ${kind}` : "");
        this.renderRich(bubble, text);
        this.chatArea.appendChild(bubble);
        if (role === "assistant" && kind === "report" && meta?.request_id) {
            this.appendFeedbackControls(bubble, meta.request_id);
        }
        this.scrollToBottom();
        return bubble;
    }

    // appendFeedbackControls: the "Was this report useful? Satisfied / Unsatisfied" row inside a
    // report bubble in the CHAT (the report-page version lives in renderReportFeedback).
    private appendFeedbackControls(bubble: HTMLDivElement, requestId: string): void {
        const row = document.createElement("div");
        row.className = "feedback-row";
        row.dataset.requestId = requestId;
        const label = document.createElement("span");
        label.className = "feedback-label";
        label.textContent = "Was this report useful?";
        row.appendChild(label);
        const yes = document.createElement("button");
        yes.type = "button";
        yes.className = "feedback-button";
        yes.textContent = "Satisfied";
        yes.addEventListener("click", () => void this.submitFeedback(requestId, true, row));
        const no = document.createElement("button");
        no.type = "button";
        no.className = "feedback-button";
        no.textContent = "Unsatisfied";
        no.addEventListener("click", () => void this.submitFeedback(requestId, false, row));
        row.append(yes, no);
        bubble.appendChild(row);
    }

    // submitFeedback: chat-mode version of sending feedback (same POST /feedback body as the
    // report-page panel). Buttons are disabled during the request; on success the chosen one is
    // highlighted, and "Unsatisfied" adds the Return/Finish follow-up. On failure buttons are
    // re-enabled and the error is shown in the status line.
    private async submitFeedback(requestId: string, satisfied: boolean, row: HTMLDivElement): Promise<void> {
        const buttons = Array.from(row.querySelectorAll("button")) as HTMLButtonElement[];
        buttons.forEach(button => button.disabled = true);
        try {
            const result = await this.api("/feedback", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    session_id: this.sessionId || undefined,
                    request_id: requestId,
                    satisfaction: satisfied ? "satisfied" : "unsatisfied",
                    satisfied
                })
            });
            if (!result.ok) throw new Error(result.data?.error || result.data?.message || "Feedback could not be saved.");
            logDebug("info", "Feedback sent", { sessionId: this.sessionId, requestId, satisfied });

            const label = satisfied ? "Satisfied" : "Unsatisfied";
            const bubble = row.parentElement as HTMLDivElement;
            const existingFollowup = bubble.querySelector(".feedback-followup");
            if (existingFollowup) existingFollowup.remove();

            // The row holds exactly two buttons: Satisfied first, Unsatisfied last.
            const yesBtn = row.querySelector("button:first-of-type") as HTMLButtonElement;
            const noBtn = row.querySelector("button:last-of-type") as HTMLButtonElement;
            if (yesBtn && noBtn) {
                if (satisfied) {
                    yesBtn.classList.add("selected");
                    yesBtn.disabled = true;
                    noBtn.classList.remove("selected");
                    noBtn.disabled = false;
                } else {
                    noBtn.classList.add("selected");
                    noBtn.disabled = true;
                    yesBtn.classList.remove("selected");
                    yesBtn.disabled = false;
                }
            }

            const labelSpan = row.querySelector(".feedback-label");
            if (labelSpan) {
                labelSpan.textContent = `Feedback recorded: ${label}.`;
            }

            if (!satisfied) {
                this.appendUnsatisfiedActions(bubble, row);
            }
        } catch (error) {
            logDebug("error", "Feedback failed", { sessionId: this.sessionId, requestId, satisfied, error });
            buttons.forEach(button => button.disabled = false);
            this.setStatus(error instanceof Error ? error.message : "Feedback could not be saved.");
        }
    }

    // appendUnsatisfiedActions: after "Unsatisfied" in the chat, asks "Would you like to return to
    // the conversation?" with two buttons:
    //   Return to conversation - POST /chat { action: "feedback_followup", choice: "return" } so the
    //                            assistant replies with how to improve the report.
    //   Finish session         - endSession().
    private appendUnsatisfiedActions(bubble: HTMLDivElement, feedbackRow?: HTMLDivElement): void {
        const existing = bubble.querySelector(".feedback-followup");
        if (existing) existing.remove();

        const prompt = document.createElement("div");
        prompt.className = "feedback-followup";
        const text = document.createElement("p");
        text.textContent = "Would you like to return to the conversation with the assistant to improve this report?";
        const actions = document.createElement("div");
        actions.className = "feedback-row";
        const back = document.createElement("button");
        back.type = "button"; back.className = "feedback-button"; back.textContent = "Return to conversation";
        back.addEventListener("click", async () => {
            // Setting textContent replaces the whole prompt (question and both buttons) with this line.
            prompt.textContent = "You can continue in this conversation to improve the report.";
            if (this.sessionId) {
                await this.demoRoleReady;
                this.demoRole = await this.readStoredDemoRole(this.sessionId) || this.demoRole;
                await this.saveDemoRole();
                const result = await this.api("/chat", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        session_id: this.sessionId,
                        action: "feedback_followup",
                        choice: "return",
                        message: "Return to conversation",
                        demo_role: this.demoRole
                    })
                });
                logDebug("info", "Feedback follow-up 'return' sent", { sessionId: this.sessionId, status: result.status });
            }
            this.input.focus();
        });
        const finish = document.createElement("button");
        finish.type = "button"; finish.className = "feedback-button"; finish.textContent = "Finish session";
        finish.addEventListener("click", () => {
            if (feedbackRow) {
                feedbackRow.querySelectorAll("button").forEach(b => (b.disabled = true));
            }
            void this.endSession();
        });
        actions.append(back, finish);
        prompt.append(text, actions);
        bubble.appendChild(prompt);
        this.scrollToBottom();
    }

    // refreshSuggestions: suggestion chips are only shown under the NEWEST message. This removes
    // all existing chip rows and, if the last message is from the assistant and has
    // meta.suggestions, adds a fresh row under it. Clicking a chip disables the row and sends it.
    private refreshSuggestions(): void {
        this.chatArea.querySelectorAll(".bubble-chips").forEach(el => el.remove());
        const last = this.entries[this.entries.length - 1];
        const suggestions = last?.role === "assistant" ? last.meta?.suggestions : undefined;
        const bubble = last ? this.rendered.get(last.id) : undefined;
        if (!suggestions?.length || !bubble) return;

        const row = document.createElement("div");
        row.className = "chip-row bubble-chips";
        suggestions.forEach(suggestion => {
            row.appendChild(this.makeChip(suggestion.label, () => {
                row.querySelectorAll("button").forEach(b => ((b as HTMLButtonElement).disabled = true));
                void this.send(suggestion.message || suggestion.label,
                    { action: suggestion.action, visual_type: suggestion.visual_type, choice: suggestion.choice });
            }));
        });
        bubble.appendChild(row);
        this.scrollToBottom();
    }

    // makeChip: one rounded clickable button. Clicks are ignored while a message is being sent.
    private makeChip(label: string, onClick: () => void): HTMLButtonElement {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "chip";
        chip.textContent = label;
        chip.addEventListener("click", () => {
            if (!this.isSending) onClick();
        });
        return chip;
    }

    // showTyping: adds the "assistant is typing" bubble (three animated dots + a label that
    // progress polling fills in). send() removes it when /chat returns.
    private showTyping(): HTMLDivElement {
        const el = document.createElement("div");
        el.className = "bubble assistant typing";
        el.setAttribute("aria-label", "The assistant is typing");
        for (let i = 0; i < 3; i++) {
            const dot = document.createElement("span");
            dot.className = "dot";
            el.appendChild(dot);
        }
        const label = document.createElement("span");
        label.className = "typing-label";
        el.appendChild(label);
        this.chatArea.appendChild(el);
        this.scrollToBottom();
        return el;
    }

    // Tiny, safe formatter (no innerHTML): paragraphs, "- " bullets, **bold**, `code`,
    // ``` fenced code blocks, and SQL that the model forgot to fence.
    // Why no innerHTML: message text comes from the server/LLM. Inserting it as HTML could run
    // injected markup; creating elements and setting textContent can never do that.
    // It walks the text line by line (index i) and decides what each line starts.
    private renderRich(container: HTMLElement, text: string): void {
        const lines = text.split(/\r?\n/);
        let list: HTMLUListElement | null = null;
        let i = 0;
        while (i < lines.length) {
            const line = lines[i];

            // 1) A ``` fence (optionally ```sql): collect lines until the closing fence.
            const fence = /^\s*```\s*(\w*)\s*$/.exec(line);
            if (fence) {
                const body: string[] = [];
                i++;
                while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) body.push(lines[i++]);
                i++;   // closing fence (or end of text)
                list = null;
                this.appendCodeBlock(container, body.join("\n"), fence[1].toLowerCase() === "sql" || this.looksLikeSql(body));
                continue;
            }

            // 2) Unfenced SQL: a line starting with SELECT/WITH begins a block that runs to the next
            //    empty line; treat it as code if it is 3+ lines and contains FROM.
            if (/^\s*(select|with)\b/i.test(line)) {
                const block: string[] = [];
                let j = i;
                while (j < lines.length && lines[j].trim() !== "") block.push(lines[j++]);
                if (block.length >= 3 && this.looksLikeSql(block)) {
                    list = null;
                    this.appendCodeBlock(container, block.join("\n"), true);
                    i = j;
                    continue;
                }
            }

            // 3) Bullet lines ("- ", "* ", "• "): consecutive ones share one <ul>.
            const bullet = /^\s*[-*•]\s+(.*)$/.exec(line);
            if (bullet) {
                if (!list) {
                    list = document.createElement("ul");
                    container.appendChild(list);
                }
                const item = document.createElement("li");
                this.renderInline(item, bullet[1]);
                list.appendChild(item);
                i++;
                continue;
            }
            // 4) Anything else ends the current list; non-empty lines become a <p>.
            list = null;
            if (line.trim()) {
                const paragraph = document.createElement("p");
                this.renderInline(paragraph, line);
                container.appendChild(paragraph);
            }
            i++;
        }
    }

    // looksLikeSql: rough check - starts with SELECT/WITH and contains FROM somewhere.
    private looksLikeSql(lines: string[]): boolean {
        const text = lines.join("\n");
        return /^\s*(select|with)\b/i.test(text) && /\bfrom\b/i.test(text);
    }

    // appendCodeBlock: a <pre><code> block; SQL gets colour highlighting, other code is plain text.
    private appendCodeBlock(container: HTMLElement, code: string, sql: boolean): void {
        const pre = document.createElement("pre");
        pre.className = "code-block";
        const codeEl = document.createElement("code");
        if (sql) {
            this.highlightSql(codeEl, code);
        } else {
            codeEl.textContent = code;
        }
        pre.appendChild(codeEl);
        container.appendChild(pre);
    }

    // Colours SQL keywords, strings and numbers. Pure DOM (no innerHTML).
    private highlightSql(parent: HTMLElement, code: string): void {
        const keywords = new Set(("select from where group by order having limit offset join left right inner outer " +
            "full cross on as and or not in is null like between case when then else end distinct union all with " +
            "asc desc interval exists over partition").split(" "));
        // split() with a capturing group keeps the matched pieces in the result, so we get the
        // tokens ('strings', numbers, words) AND everything between them, in order.
        // Tokens get a <span class="str|num|kw">; everything else is added as plain text.
        for (const part of code.split(/('(?:[^']|'')*'|\b\d+(?:\.\d+)?\b|\b[A-Za-z_]+\b)/)) {
            if (!part) continue;
            let className = "";
            if (part.startsWith("'")) className = "str";
            else if (/^\d/.test(part)) className = "num";
            else if (keywords.has(part.toLowerCase())) className = "kw";
            if (className) {
                const span = document.createElement("span");
                span.className = className;
                span.textContent = part;
                parent.appendChild(span);
            } else {
                parent.appendChild(document.createTextNode(part));
            }
        }
    }

    // renderInline: formatting inside one line - **bold** becomes <strong>, `code` becomes <code>,
    // the rest is plain text. Same split-with-capture trick as highlightSql.
    private renderInline(parent: HTMLElement, text: string): void {
        for (const part of text.split(/(\*\*[^*]+\*\*|`[^`]+`)/)) {
            if (!part) continue;
            if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
                const strong = document.createElement("strong");
                strong.textContent = part.slice(2, -2);
                parent.appendChild(strong);
            } else if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
                const code = document.createElement("code");
                code.textContent = part.slice(1, -1);
                parent.appendChild(code);
            } else {
                parent.appendChild(document.createTextNode(part));
            }
        }
    }

    // ------------------------------------------------------------------ small UI helpers
    // setSending: flips the "busy sending" flag and disables/enables the input and Send button.
    private setSending(sending: boolean): void {
        this.isSending = sending;
        this.sendButton.disabled = sending;
        this.input.disabled = sending;
    }

    // setStatus: text of the small status line under the input ("" hides the message).
    private setStatus(text: string): void {
        this.statusText.textContent = text;
    }

    // autoGrow: resizes the text box to fit its content, up to 96px tall (then it scrolls).
    // Setting height to "auto" first lets scrollHeight shrink again when text is deleted.
    private autoGrow(): void {
        this.input.style.height = "auto";
        this.input.style.height = `${Math.min(this.input.scrollHeight, 96)}px`;
    }

    // scrollToBottom: keeps the newest message in view.
    private scrollToBottom(): void {
        this.chatArea.scrollTop = this.chatArea.scrollHeight;
    }
}
