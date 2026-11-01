"use strict";

import powerbi from "powerbi-visuals-api";
import "./../style/visual.less";

import VisualConstructorOptions = powerbi.extensibility.visual.VisualConstructorOptions;
import VisualUpdateOptions = powerbi.extensibility.visual.VisualUpdateOptions;
import IVisual = powerbi.extensibility.visual.IVisual;

// Must match the WebAccess privilege in capabilities.json.
const API_BASE = "http://localhost:5056";
const STORAGE_KEY = "billingChatbot.session.v2";
const DEMO_ROLE_STORAGE_KEY = "billingChatbot.demoRole.v1";
const DEMO_ROLES = ["CEO", "Developer", "Analyst"] as const;
const CHAT_TIMEOUT_MS = 300_000;   // planning + SQL + Power BI page generation can take a while
const QUICK_TIMEOUT_MS = 20_000;
const POLL_INTERVAL_MS = 2_500;
const PROGRESS_INTERVAL_MS = 1_200;
const POLL_MAX_MS = 4 * 60_000;
const DEMO_ROLE_RETRY_MS = 5_000;

const GREETING =
    "Hi! I'm your Billing AI Assistant. Ask me for a report, or ask questions about the one you're " +
    "viewing. You can also say things like “make it a pie chart” or “same but for 2025”.";
const EXAMPLES = [
    "Monthly revenue for the last 12 months",
    "Active subscriptions by plan",
    "Refunds by reason this year"
];

interface Suggestion {
    label: string;
    message?: string;
    action?: string;
    visual_type?: string;
    choice?: string;            // feedback_followup: "return" | "finish"
}

interface ChatEntry {
    id: number;
    role: "user" | "assistant";
    text: string;
    kind?: string;
    meta?: { suggestions?: Suggestion[]; request_id?: string; view_name?: string; page_name?: string; chart_type?: string };
}

interface ApiResult {
    ok: boolean;
    status: number;
    data: any;
}

interface SendOptions {
    action?: string;
    visual_type?: string;
    choice?: string;
}

export class Visual implements IVisual {
    private storage: powerbi.extensibility.IVisualLocalStorageV2Service | null;
    private chatArea: HTMLDivElement;
    private input: HTMLTextAreaElement;
    private sendButton: HTMLButtonElement;
    private newChatButton: HTMLButtonElement;
    private endSessionButton: HTMLButtonElement;
    private demoRoleControl: HTMLLabelElement;
    private demoRoleSelect: HTMLSelectElement;
    private statusText: HTMLDivElement;
    private greetingEl: HTMLDivElement;

    private sessionId: string | null = null;
    private entries: ChatEntry[] = [];
    private rendered = new Map<number, HTMLElement>();
    private pendingUser: { el: HTMLElement; text: string }[] = [];
    private lastId = 0;
    private isSending = false;
    private destroyed = false;
    private pollTimer: number | null = null;
    private pollStartedAt = 0;
    private pollInFlight = false;
    private progressTimer: number | null = null;
    private progressInFlight = false;
    private newChatArmedUntil = 0;
    private endSessionArmedUntil = 0;
    private ratedRequests = new Set<string>();
    private sessionEnded = false;
    private rootElement: HTMLElement;
    private chatContainer: HTMLDivElement;
    private feedbackRendered = false;
    private restoreStarted = false;
    private demoRole = "CEO";
    private demoRoleSelectionEnabled = false;
    private demoRoleFromStorage = false;    // false: demoRole is only the "CEO" default
    private demoRoleReady: Promise<void>;
    private demoRolePinnedToSession = false;
    private demoRoleRetryTimer: number | null = null;

    constructor(options: VisualConstructorOptions) {
        this.storage = options?.host?.storageV2Service ?? null;
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
        this.newChatButton.addEventListener("click", () => void this.onNewChatClick());
        this.endSessionButton = document.createElement("button");
        this.endSessionButton.type = "button";
        this.endSessionButton.className = "end-session-button";
        this.endSessionButton.textContent = "End session";
        this.endSessionButton.addEventListener("click", () => void this.onEndSessionClick());
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
        this.demoRoleSelect.addEventListener("change", () => {
            this.demoRole = this.demoRoleSelect.value;
            void this.saveDemoRole();
        });
        roleLabel.appendChild(this.demoRoleSelect);
        this.demoRoleControl = roleLabel;
        header.appendChild(title);
        const headerActions = document.createElement("div");
        headerActions.className = "header-actions";
        headerActions.appendChild(roleLabel);
        headerActions.appendChild(this.endSessionButton);
        headerActions.appendChild(this.newChatButton);
        header.appendChild(headerActions);

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
        this.input.addEventListener("keydown", event => {
            if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
                event.preventDefault();
                void this.send(this.input.value);
            }
        });
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

        container.appendChild(header);
        container.appendChild(this.chatArea);
        container.appendChild(composer);
        container.appendChild(this.statusText);
        root.appendChild(container);

        this.greetingEl = this.buildGreeting();
        this.chatArea.appendChild(this.greetingEl);
        this.demoRoleReady = this.initializeDemoRoleSelector();

    }

    // ------------------------------------------------------------------ lifecycle
    public update(options: VisualUpdateOptions): void {
        // Generated report pages host this same custom visual in feedback-only mode.
        // The request/session identifiers are embedded in the PBIR visual objects.
        // NOTE: Power BI passes static objects via dataViews[0].metadata even when
        // there is no dataViewMappings defined, so the chatbot renders immediately
        // without requiring any field to be dragged in.
        const feedbackObjs = options.dataViews?.[0]?.metadata?.objects as any;
        const feedback = feedbackObjs?.feedback;
        const mode = this.readObjectText(feedback?.mode);
        if (mode === "feedback") {
            const requestId = this.readObjectText(feedback?.requestId);
            const sessionId = this.readObjectText(feedback?.sessionId);
            if (requestId && !this.feedbackRendered) {
                this.feedbackRendered = true;
                this.chatContainer.style.display = "none";
                this.renderReportFeedback(requestId, sessionId || "");
            }
            return;
        }
        if (!this.restoreStarted) {
            this.restoreStarted = true;
            void this.restoreConversation();
        }
    }

    private readObjectText(value: any): string {
        if (typeof value === "string") return value.replace(/^['"]|['"]$/g, "");
        if (value && typeof value === "object") {
            if (typeof value.value === "string") return value.value.replace(/^['"]|['"]$/g, "");
            if (typeof value.expr?.Literal?.Value === "string") return value.expr.Literal.Value.replace(/^['"]|['"]$/g, "");
        }
        return "";
    }

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

        let currentSatisfaction: boolean | null = null;
        let isSubmitting = false;
        let followupContainer: HTMLDivElement | null = null;

        const removeFollowup = () => {
            if (followupContainer) {
                followupContainer.remove();
                followupContainer = null;
            }
        };

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
            returnBtn.addEventListener("click", async () => {
                returnBtn.disabled = true;
                finishBtn.disabled = true;
                status.textContent = "Notifying assistant…";
                try {
                    if (sessionId) {
                        await this.demoRoleReady;
                        const sessionRole = await this.readStoredDemoRole(sessionId, true);
                        if (sessionRole) {
                            this.demoRole = sessionRole;
                            await this.saveDemoRole();
                        }
                        await this.api("/chat", {
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
                    }
                } catch { /* ignore — navigation instruction is still shown */ }
                // Power BI custom visuals cannot trigger page navigation directly.
                // Show a clear instruction so the user knows what to do.
                status.textContent = "✅ Session preserved. Click the 'Back to AI Assistant' button at the bottom left " +
                    "(Ctrl+click while editing in Power BI Desktop), or the 'AI Assistant' page tab, to continue.";
            });

            const finishBtn = document.createElement("button");
            finishBtn.type = "button"; finishBtn.className = "feedback-button"; finishBtn.textContent = "Finish session";
            finishBtn.addEventListener("click", () => void this.finishFeedbackSession(sessionId, status, finishBtn, yes, no, returnBtn));

            followupActions.append(returnBtn, finishBtn);
            followupContainer.append(question, followupActions);
            panel.appendChild(followupContainer);
        };

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
                if (!result.ok) throw new Error(result.data?.error || result.data?.message || "Feedback could not be saved.");

                currentSatisfaction = satisfied;
                const label = satisfied ? "Satisfied" : "Unsatisfied";

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
        this.rootElement.appendChild(panel);
    }

    private async finishFeedbackSession(sessionId: string, status: HTMLDivElement, finishBtn: HTMLButtonElement,
                                        yesBtn?: HTMLButtonElement, noBtn?: HTMLButtonElement, returnBtn?: HTMLButtonElement): Promise<void> {
        if (!sessionId) {
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
            status.textContent = "Session finished. Your conversation and report history are preserved.";
        } catch (error) {
            finishBtn.disabled = false;
            if (returnBtn) returnBtn.disabled = false;
            if (yesBtn && yesBtn.classList.contains("selected")) yesBtn.disabled = true;
            if (noBtn && noBtn.classList.contains("selected")) noBtn.disabled = true;
            status.textContent = error instanceof Error ? error.message : "Could not finish the session.";
        }
    }

    public destroy(): void {
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
    private async readStoredSessionId(): Promise<string | null> {
        if (!this.storage) return null;
        try {
            const value = await this.storage.get(STORAGE_KEY);
            return typeof value === "string" && value ? value : null;
        } catch {
            return null;   // nothing stored yet
        }
    }

    private async initializeDemoRoleSelector(): Promise<void> {
        let health: ApiResult | null = null;
        try {
            health = await this.api("/health", undefined, QUICK_TIMEOUT_MS);
        } catch {
            health = null;
        }
        if (this.destroyed || this.demoRoleSelectionEnabled) return;
        if (!health?.ok) {
            // Backend not reachable yet (e.g. started after Power BI loaded the visual): keep asking
            // in the background. Callers awaiting demoRoleReady only wait for this first attempt.
            this.demoRoleRetryTimer = window.setTimeout(() => {
                this.demoRoleRetryTimer = null;
                void this.initializeDemoRoleSelector();
            }, DEMO_ROLE_RETRY_MS);
            return;
        }
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
        } catch {
            // Role selection stays hidden unless the backend explicitly enables demo mode.
        }
    }

    private async saveDemoRole(): Promise<void> {
        if (!this.demoRoleSelectionEnabled || !this.storage) return;
        try {
            await this.storage.set(DEMO_ROLE_STORAGE_KEY, this.demoRole);
            if (this.sessionId) {
                await this.storage.set(`${DEMO_ROLE_STORAGE_KEY}.${this.sessionId}`, this.demoRole);
            }
        } catch {
            /* role selection still applies to this visual instance */
        }
    }

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
        } catch {
            return null;
        }
    }

    private async saveSessionId(): Promise<void> {
        if (!this.storage || !this.sessionId) return;
        try {
            await this.storage.set(STORAGE_KEY, this.sessionId);
        } catch (error) {
            console.warn("Could not store the chat session id:", error);
        }
    }

    private clearStoredSessionId(): void {
        try {
            this.storage?.remove(STORAGE_KEY);
        } catch {
            /* ignore */
        }
    }

    private async restoreConversation(): Promise<void> {
        await this.demoRoleReady;
        let id = await this.readStoredSessionId();
        let fromFallback = false;
        if (!id && this.demoRoleSelectionEnabled && !this.demoRoleFromStorage) {
            // No saved role either: the "CEO" default is a guess, so look for the newest session
            // of every demo role instead of only CEO's.
            const found = await this.findLatestSessionAcrossRoles();
            if (this.destroyed) return;
            if (typeof found === "string") {
                this.setStatus(found);          // nothing restored; say why instead of guessing
                return;
            }
            if (found) {
                id = found.sessionId;
                fromFallback = true;
                this.demoRole = found.role;
                this.demoRoleSelect.value = found.role;
            }
        } else if (!id) {
            // Local storage can be scoped to a visual instance. Restore the most recent session
            // from the backend when returning from a report page; demo mode filters by role.
            try {
                const roleQuery = this.demoRoleSelectionEnabled
                    ? `?demo_role=${encodeURIComponent(this.demoRole)}` : "";
                const latest = await this.api(`/session/latest${roleQuery}`, undefined, QUICK_TIMEOUT_MS);
                if (latest.ok && latest.data?.session_id) {
                    id = latest.data.session_id as string;
                    fromFallback = true;
                }
            } catch {
                /* backend not reachable yet */
            }
        }
        if (!id || this.destroyed) return;

        if (this.demoRoleSelectionEnabled) {
            const sessionRole = await this.readStoredDemoRole(id, true);
            if (sessionRole) {
                this.demoRole = sessionRole;
                this.demoRoleSelect.value = this.demoRole;
            } else {
                this.demoRolePinnedToSession = true;
            }
        }

        try {
            const result = await this.api(`/session/${encodeURIComponent(id)}/transcript?after=0`, undefined, QUICK_TIMEOUT_MS);
            if (this.destroyed) return;
            if (result.status === 404) {
                this.clearStoredSessionId();
                if (!fromFallback) {
                    this.appendLocalNote("I've started a fresh conversation; the previous one is no longer available.");
                }
                return;
            }
            if (!result.ok) return;
            this.sessionId = id;
            this.demoRoleSelect.disabled = true;
            this.applyEntries((result.data.messages ?? []) as ChatEntry[]);
            if (result.data.ended) {
                this.clearStoredSessionId();
                this.disableSession();
                return;
            }
            void this.saveSessionId();
            if (result.data.busy) this.startPolling();
        } catch {
            this.setStatus("Can't reach the assistant service. Is the Flask backend running?");
        }
    }

    // The newest active session over all demo roles: { sessionId, role }, null when no role has
    // one, or a status message when it cannot be decided reliably (nothing is restored then).
    private async findLatestSessionAcrossRoles(): Promise<{ sessionId: string; role: string } | null | string> {
        const found: { sessionId: string; role: string; lastActive: number | null }[] = [];
        for (const role of DEMO_ROLES) {
            let latest: ApiResult;
            try {
                latest = await this.api(`/session/latest?demo_role=${encodeURIComponent(role)}`,
                                        undefined, QUICK_TIMEOUT_MS);
            } catch {
                return "Can't reach the assistant service to restore your previous conversation.";
            }
            if (latest.status === 404) continue;                 // no session for this role
            const sessionId = latest.data?.session_id;
            if (!latest.ok || typeof sessionId !== "string" || !sessionId) {
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
        found.sort((a, b) => (b.lastActive as number) - (a.lastActive as number));
        return found[0];
    }

    // ------------------------------------------------------------------ HTTP
    private async api(path: string, init?: RequestInit, timeoutMs = QUICK_TIMEOUT_MS): Promise<ApiResult> {
        const controller = new AbortController();
        const timer = window.setTimeout(() => controller.abort(), timeoutMs);
        try {
            const response = await fetch(API_BASE + path, { ...init, signal: controller.signal });
            let data: any = {};
            try {
                data = await response.json();
            } catch {
                /* empty or non-JSON body */
            }
            return { ok: response.ok, status: response.status, data };
        } finally {
            window.clearTimeout(timer);
        }
    }

    // ------------------------------------------------------------------ sending
    private async send(rawText: string, options: SendOptions = {}): Promise<void> {
        await this.demoRoleReady;
        const text = (rawText || "").trim();
        if (!text || this.isSending) return;
        if (this.sessionEnded) {
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
            this.demoRoleSelect.disabled = true;
            void this.saveSessionId();
        }
        await this.saveDemoRole();
        const typing = this.showTyping();
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
                    demo_role: this.demoRolePinnedToSession ? undefined : this.demoRole
                })
            }, CHAT_TIMEOUT_MS);

            typing.remove();
            if (result.data?.session_id && result.data.session_id !== this.sessionId) {
                this.sessionId = result.data.session_id as string;
                void this.saveSessionId();
            }

            const messages = (result.data?.messages ?? []) as ChatEntry[];
            if (messages.length) {
                this.applyEntries(messages);
            } else {
                const fallback = result.data?.message || result.data?.error ||
                    `The assistant returned an unexpected response (HTTP ${result.status}).`;
                this.appendBubble("assistant", String(fallback), "error");
            }
            if (result.data?.busy) this.startPolling();
        } catch (error) {
            typing.remove();
            console.error("Chat request failed:", error);
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

    private newSessionId(): string {
        const randomValues = new Uint8Array(12);
        window.crypto.getRandomValues(randomValues);
        return Array.from(randomValues, value => (value & 0x0f).toString(16)).join("");
    }

    private lastMessageAsksForChart(): boolean {
        const last = this.entries[this.entries.length - 1];
        return !!last && last.role === "assistant" &&
            !!last.meta?.suggestions?.some(s => s.action === "choose_chart");
    }

    // ------------------------------------------------------------------ live progress while /chat runs
    private startProgressPolling(typing: HTMLDivElement): void {
        this.stopProgressPolling();
        this.progressTimer = window.setInterval(() => void this.pollProgress(typing), PROGRESS_INTERVAL_MS);
    }

    private stopProgressPolling(): void {
        if (this.progressTimer !== null) {
            window.clearInterval(this.progressTimer);
            this.progressTimer = null;
        }
    }

    private async pollProgress(typing: HTMLDivElement): Promise<void> {
        if (!this.sessionId || this.progressInFlight) return;
        this.progressInFlight = true;
        try {
            const result = await this.api(`/session/${encodeURIComponent(this.sessionId)}/progress`, undefined, 5_000);
            const text = result.data?.progress;
            if (result.ok && typeof text === "string" && text) this.setTypingLabel(typing, text);
        } catch {
            /* progress is best-effort */
        } finally {
            this.progressInFlight = false;
        }
    }

    private setTypingLabel(typing: HTMLDivElement, text: string): void {
        const label = typing.querySelector(".typing-label");
        if (label) label.textContent = text;
    }

    private async syncFromServer(): Promise<void> {
        if (!this.sessionId || this.destroyed) return;
        try {
            const result = await this.api(
                `/session/${encodeURIComponent(this.sessionId)}/transcript?after=${this.lastId}`);
            if (result.ok) {
                this.applyEntries((result.data.messages ?? []) as ChatEntry[]);
                if (result.data.busy) this.startPolling();
            }
        } catch {
            /* still offline */
        }
    }

    // ------------------------------------------------------------------ polling (background Power BI reload)
    private startPolling(): void {
        if (this.pollTimer !== null || this.destroyed) return;
        this.pollStartedAt = Date.now();
        this.setStatus("Power BI is updating the report…");
        this.pollTimer = window.setInterval(() => void this.pollOnce(), POLL_INTERVAL_MS);
    }

    private stopPolling(): void {
        if (this.pollTimer !== null) {
            window.clearInterval(this.pollTimer);
            this.pollTimer = null;
        }
        if (!this.isSending) this.setStatus("");
    }

    private async pollOnce(): Promise<void> {
        if (this.pollInFlight || !this.sessionId) return;
        this.pollInFlight = true;
        try {
            const result = await this.api(
                `/session/${encodeURIComponent(this.sessionId)}/transcript?after=${this.lastId}`);
            if (result.ok) {
                this.applyEntries((result.data.messages ?? []) as ChatEntry[]);
                if (!result.data.busy) this.stopPolling();
            }
        } catch {
            /* keep trying until the deadline */
        } finally {
            this.pollInFlight = false;
        }
        if (Date.now() - this.pollStartedAt > POLL_MAX_MS) this.stopPolling();
    }

    // ------------------------------------------------------------------ new chat
    private async onNewChatClick(): Promise<void> {
        const now = Date.now();
        if (now > this.newChatArmedUntil) {
            // Two-step confirm (dialogs are blocked inside Power BI visuals).
            this.newChatArmedUntil = now + 3500;
            this.newChatButton.textContent = "Click again to confirm";
            window.setTimeout(() => {
                if (Date.now() >= this.newChatArmedUntil) this.newChatButton.textContent = "New chat";
            }, 3600);
            return;
        }
        this.newChatArmedUntil = 0;
        this.newChatButton.textContent = "New chat";
        this.stopPolling();
        const id = this.sessionId;
        this.sessionId = null;
        this.demoRolePinnedToSession = false;
        this.demoRoleSelect.disabled = false;
        this.clearStoredSessionId();
        if (id && !this.sessionEnded) {
            try {
                await this.api(`/session/${encodeURIComponent(id)}/reset`, { method: "POST" });
            } catch {
                /* the old session simply expires on the server */
            }
        }
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

    private async endSession(): Promise<void> {
        if (this.isSending) {
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
                    this.setStatus(result.data?.error || "Could not end the session.");
                    return;
                }
            } catch {
                this.setStatus("Could not reach the assistant to end the session.");
                return;
            }
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

    private hideGreetingChips(): void {
        this.greetingEl.querySelectorAll(".greeting-chips").forEach(el => el.remove());
    }

    private appendLocalNote(text: string): void {
        this.appendBubble("assistant", text, "status");
    }

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
                    this.rendered.set(entry.id, this.pendingUser.splice(index, 1)[0].el);
                    continue;
                }
            }
            this.rendered.set(entry.id, this.appendBubble(entry.role, entry.text, entry.kind, entry.meta));
        }
        if (changed) this.refreshSuggestions();
    }

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

            const label = satisfied ? "Satisfied" : "Unsatisfied";
            const bubble = row.parentElement as HTMLDivElement;
            const existingFollowup = bubble.querySelector(".feedback-followup");
            if (existingFollowup) existingFollowup.remove();

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
            buttons.forEach(button => button.disabled = false);
            this.setStatus(error instanceof Error ? error.message : "Feedback could not be saved.");
        }
    }

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
            prompt.textContent = "You can continue in this conversation to improve the report.";
            if (this.sessionId) {
                await this.demoRoleReady;
                this.demoRole = await this.readStoredDemoRole(this.sessionId) || this.demoRole;
                await this.saveDemoRole();
                await this.api("/chat", {
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
    private renderRich(container: HTMLElement, text: string): void {
        const lines = text.split(/\r?\n/);
        let list: HTMLUListElement | null = null;
        let i = 0;
        while (i < lines.length) {
            const line = lines[i];

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
            list = null;
            if (line.trim()) {
                const paragraph = document.createElement("p");
                this.renderInline(paragraph, line);
                container.appendChild(paragraph);
            }
            i++;
        }
    }

    private looksLikeSql(lines: string[]): boolean {
        const text = lines.join("\n");
        return /^\s*(select|with)\b/i.test(text) && /\bfrom\b/i.test(text);
    }

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
    private setSending(sending: boolean): void {
        this.isSending = sending;
        this.sendButton.disabled = sending;
        this.input.disabled = sending;
    }

    private setStatus(text: string): void {
        this.statusText.textContent = text;
    }

    private autoGrow(): void {
        this.input.style.height = "auto";
        this.input.style.height = `${Math.min(this.input.scrollHeight, 96)}px`;
    }

    private scrollToBottom(): void {
        this.chatArea.scrollTop = this.chatArea.scrollHeight;
    }
}
