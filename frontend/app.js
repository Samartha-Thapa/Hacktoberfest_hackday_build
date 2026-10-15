(function () {
  "use strict";

  const body = document.body;
  const onboarding = document.querySelector("[data-onboarding]");
  const dashboard = document.querySelector("[data-dashboard]");
  const primaryButton = document.querySelector("[data-onboarding-primary]");
  const secondaryButton = document.querySelector("[data-onboarding-secondary]");
  const setupPrimaryButton = document.querySelector("[data-setup-primary]");
  const setupSecondaryButton = document.querySelector("[data-setup-secondary]");
  const title = document.querySelector("[data-onboarding-title]");
  const copy = document.querySelector("[data-onboarding-copy]");
  const note = document.querySelector("[data-onboarding-note]");
  const setupNote = document.querySelector("[data-setup-note]");
  const buddyImage = document.querySelector("[data-buddy-image]");
  const buddyFace = document.querySelector("[data-buddy-face]");
  const onboardingScenes = document.querySelectorAll("[data-onboarding-scene]");
  const settingsLink = document.querySelector("[data-settings-link]");
  const inboxLink = document.querySelector("[data-inbox-link]");
  const dialog = document.querySelector("[data-tour-dialog]");
  const backdrop = document.querySelector("[data-tour-backdrop]");
  const spotlight = document.querySelector("[data-tour-spotlight]");
  const nextButton = document.querySelector("[data-tour-next]");
  const backButton = document.querySelector("[data-tour-back]");
  const skipButton = document.querySelector("[data-tour-skip]");
  const connectionButton = document.querySelector("[data-reopen-connection]");
  const connectionLabel = document.querySelector("[data-connection-label]");
  const connectionStateNote = document.querySelector("[data-connection-state-note]");
  const retryButton = document.querySelector('[data-action="retry"]');
  const refreshInboxButton = document.querySelector("[data-refresh-inbox]");
  const messageCount = document.querySelector("[data-message-count]");
  const messageList = document.querySelector("[data-message-list]");
  const detailSubject = document.querySelector("[data-detail-subject]");
  const detailFrom = document.querySelector("[data-detail-from]");
  const detailDate = document.querySelector("[data-detail-date]");
  const detailBody = document.querySelector("[data-detail-body]");
  const summaryInvitation = document.querySelector("[data-summary-invitation]");
  const summaryEnvironment = document.querySelector("[data-summary-environment]");
  const summaryMusicNote = document.querySelector("[data-summary-music-note]");
  const summarizeButton = document.querySelector("[data-summarize-email]");
  const summaryStatus = document.querySelector("[data-summary-status]");
  const summaryNote = document.querySelector("[data-summary-note]");
  const messageSummary = document.querySelector("[data-message-summary]");
  const summaryGist = document.querySelector("[data-summary-gist]");
  const summaryPoints = document.querySelector("[data-summary-points]");
  const summaryActions = document.querySelector("[data-summary-actions]");
  const detailStates = document.querySelectorAll("[data-detail-state]");
  const inboxStates = {
    disconnected: document.querySelector('[data-state="disconnected"]'),
    loading: document.querySelector('[data-state="loading"]'),
    empty: document.querySelector('[data-state="empty"]'),
    error: document.querySelector('[data-state="error"]'),
  };

  if (!onboarding || !dashboard || !primaryButton || !secondaryButton) {
    return;
  }

  const storageKey = "talkie-mail-onboarding-complete";
  const typingText = title.dataset.typingText || title.textContent.trim();
  const state = {
    connection: body.dataset.connectionState || "disconnected",
    screen: "welcome",
    oauthConnected: false,
    tourStep: 0,
  };
  let connectionConfigured = false;
  let csrfToken = null;
  let inboxRequestActive = false;
  let inboxLoaded = false;
  let detailRequestActive = false;
  let selectedMessageId = null;
  let summaryRequestActive = false;
  let summaryStatusTimer = null;
  let summaryMotionTimer = null;
  let summaryStartedAt = 0;
  let typingTimer = null;
  let authorizationMessage = null;
  let celebrationTimer = null;
  let typingPosition = 0;
  let lastFocusedElement = null;
  let walkthroughPending = false;

  const tourSteps = [
    {
      target: ".inbox-column",
      title: "Step 1: Your inbox",
      copy: "Your email list appears here, so you can see what needs your attention.",
    },
    {
      target: ".detail-panel",
      title: "Step 2: Select a message",
      copy: "Select an email from the list to read its details and choose what to do next.",
    },
    {
      target: "[data-summary-invitation]",
      title: "Step 3: Ask Buddy for a summary",
      copy: "Open an email and use Buddy to find the gist, key points, and action items.",
    },
  ];

  function saveCompletion() {
    try {
      window.localStorage.setItem(storageKey, "true");
    } catch (error) {
      // The current session still works when browser storage is unavailable.
    }
  }

  function showDashboard(openWalkthrough) {
    state.screen = "dashboard";
    window.clearTimeout(celebrationTimer);
    body.classList.remove("onboarding-active");
    onboarding.hidden = true;
    dashboard.hidden = false;
    inboxLink.classList.add("nav-link--active");
    settingsLink.classList.remove("nav-link--active");
    refreshConnectionStatus();
    walkthroughPending = Boolean(openWalkthrough && !storageHasCompleted());
  }

  function storageHasCompleted() {
    try {
      return window.localStorage.getItem(storageKey) === "true";
    } catch (error) {
      return false;
    }
  }

  function resolveTourTarget(selector) {
      const target = document.querySelector(selector);
      if (target && target.offsetWidth > 0 && target.offsetHeight > 0) {
        return target;
      }
      return document.querySelector(".detail-panel");
    }

  function positionTour() {
      if (!dialog || dialog.hidden) {
        return;
      }
      const target = resolveTourTarget(tourSteps[state.tourStep].target);
      if (!target) {
        return;
      }
      const rect = target.getBoundingClientRect();
      const top = Math.max(8, Math.min(window.innerHeight - 16, rect.top - 8));
      const left = Math.max(8, Math.min(window.innerWidth - 16, rect.left - 8));
      spotlight.style.top = top + "px";
      spotlight.style.left = left + "px";
      spotlight.style.width = Math.max(16, Math.min(window.innerWidth - left - 8, rect.width + 16)) + "px";
      spotlight.style.height = Math.max(16, Math.min(window.innerHeight - top - 8, rect.height + 16)) + "px";

      const centered = window.innerWidth < 700;
      dialog.classList.toggle("tour-dialog--centered", centered);
      if (centered) {
        dialog.style.removeProperty("top");
        dialog.style.removeProperty("left");
        return;
      }
      dialog.style.left = Math.max(16, window.innerWidth - dialog.offsetWidth - 16) + "px";
      dialog.style.top = Math.max(16, Math.min(rect.bottom + 18, window.innerHeight - dialog.offsetHeight - 16)) + "px";
    }

  function renderTourStep() {
      const step = tourSteps[state.tourStep];
      document.querySelector("[data-tour-step]").textContent =
        "Step " + (state.tourStep + 1) + " of " + tourSteps.length;
      document.querySelector("[data-tour-title]").textContent = step.title;
      document.querySelector("[data-tour-copy]").textContent = step.copy;
      backButton.disabled = state.tourStep === 0;
      nextButton.textContent = state.tourStep === tourSteps.length - 1 ? "Finish" : "Next";
      window.requestAnimationFrame(positionTour);
    }

  function closeTour(markComplete) {
      if (!dialog) {
        return;
      }
      dialog.hidden = true;
      backdrop.hidden = true;
      spotlight.hidden = true;
      body.classList.remove("tour-is-open");
      if (markComplete) {
        saveCompletion();
      }
      if (lastFocusedElement && typeof lastFocusedElement.focus === "function") {
        lastFocusedElement.focus();
      }
    }

  function openTour() {
      if (!dialog || state.screen !== "dashboard" || storageHasCompleted()) {
        return;
      }
      lastFocusedElement = document.activeElement;
      state.tourStep = 0;
      dialog.hidden = false;
      backdrop.hidden = false;
      spotlight.hidden = false;
      body.classList.add("tour-is-open");
      renderTourStep();
      nextButton.focus();
  }

  function showOnboardingScene(sceneName) {
    onboardingScenes.forEach(function (scene) {
      scene.hidden = scene.dataset.onboardingScene !== sceneName;
    });
    onboarding.classList.toggle("is-connection", sceneName === "connection");
    onboarding.classList.toggle("is-celebration", sceneName === "celebration");
  }

  function setBuddyExpression(expression) {
    if (!buddyFace) {
      return;
    }
    buddyFace.classList.toggle("is-happy", expression === "happy");
  }

  function showCelebration() {
    state.screen = "celebration";
    state.oauthConnected = false;
    body.classList.add("onboarding-active");
    onboarding.hidden = false;
    dashboard.hidden = true;
    showOnboardingScene("celebration");
    celebrationTimer = window.setTimeout(function () {
      showDashboard(true);
    }, 3000);
  }

  function setInboxState(stateName) {
    Object.entries(inboxStates).forEach(function ([name, element]) {
      if (element) {
        element.hidden = name !== stateName;
      }
    });
    if (messageList) {
      messageList.hidden = stateName !== "messages";
    }
  }

  function renderMessages(messages) {
    if (!messageList) {
      return;
    }
    messageList.replaceChildren();
    messages.forEach(function (message) {
      const item = document.createElement("article");
      item.className = "message-row";
      item.setAttribute("role", "button");
      item.tabIndex = 0;
      item.dataset.messageId = message.id;
      item.setAttribute("aria-label", "Open " + (message.subject || "message"));
      const heading = document.createElement("div");
      heading.className = "message-row__heading";
      const sender = document.createElement("strong");
      sender.textContent = message.from || "Unknown sender";
      const date = document.createElement("time");
      date.textContent = message.date || "";
      heading.append(sender, date);
      const subject = document.createElement("h3");
      subject.textContent = message.subject || "(No subject)";
      const snippet = document.createElement("p");
      snippet.textContent = message.snippet || "No preview available.";
      item.append(heading, subject, snippet);
      messageList.append(item);
    });
  }

  function setDetailState(stateName) {
    detailStates.forEach(function (element) {
      element.hidden = element.dataset.detailState !== stateName;
    });
  }

  function resetSummaryState() {
    summaryRequestActive = false;
    stopSummaryLoading();
    if (summaryInvitation) {
      summaryInvitation.hidden = false;
    }

    if (messageSummary) {
      messageSummary.hidden = true;
    }
    if (summarizeButton) {
      summarizeButton.disabled = true;
      summarizeButton.removeAttribute("aria-busy");
    }
    if (summaryStatus) {
      summaryStatus.textContent = "I can help you catch up.";
    }
    if (summaryNote) {
      summaryNote.textContent = "Ask Buddy when you are ready.";
    }
    if (summaryGist) summaryGist.textContent = "";
    if (summaryPoints) summaryPoints.replaceChildren();
    if (summaryActions) summaryActions.textContent = "";
  }

  function startSummaryLoading() {
    summaryStartedAt = Date.now();
    if (summaryInvitation) {
      summaryInvitation.classList.add("is-loading");
      summaryInvitation.setAttribute("aria-busy", "true");
    }
    const statusMessages = [
      "Buddy is reading your email…",
      "Taking it one message at a time.",
      "A little patience, a clearer picture.",
      "Almost there — Buddy is still thinking.",
    ];
    let messageIndex = 0;
    summaryStatus.textContent = statusMessages[messageIndex];
    summaryStatusTimer = window.setInterval(function () {
      const elapsed = Date.now() - summaryStartedAt;
      if (elapsed < 3200) {
        return;
      }
      messageIndex = Math.min(messageIndex + 1, statusMessages.length - 1);
      summaryStatus.textContent = statusMessages[messageIndex];
      if (messageIndex === statusMessages.length - 1) {
        window.clearInterval(summaryStatusTimer);
        summaryStatusTimer = null;
      }
    }, 800);
    summaryMotionTimer = window.setInterval(function () {
      if (summaryMusicNote) {
        summaryMusicNote.classList.remove("is-floating");
        void summaryMusicNote.offsetWidth;
        summaryMusicNote.classList.add("is-floating");
      }
      if (summaryEnvironment) {
        summaryEnvironment.classList.remove("is-drifting");
        void summaryEnvironment.offsetWidth;
        summaryEnvironment.classList.add("is-drifting");
      }
    }, 4200);
  }

  function stopSummaryLoading() {
    window.clearInterval(summaryStatusTimer);
    window.clearInterval(summaryMotionTimer);
    summaryStatusTimer = null;
    summaryMotionTimer = null;
    if (summaryInvitation) {
      summaryInvitation.classList.remove("is-loading");
      summaryInvitation.removeAttribute("aria-busy");
    }
    if (summaryMusicNote) {
      summaryMusicNote.classList.remove("is-floating");
    }
    if (summaryEnvironment) {
      summaryEnvironment.classList.remove("is-drifting");
    }
  }

  async function summarizeSelectedMessage() {
    if (!selectedMessageId || summaryRequestActive || !summarizeButton) {
      return;
    }
    summaryRequestActive = true;
    startSummaryLoading();
    summarizeButton.disabled = true;
    summarizeButton.setAttribute("aria-busy", "true");
    summaryNote.textContent = "";
    messageSummary.hidden = true;
    try {
      const response = await fetch(
        "/api/gmail/messages/" + encodeURIComponent(selectedMessageId) + "/summarize",
        { method: "POST", credentials: "same-origin" }
      );
      const result = await response.json().catch(function () { return {}; });
      if (!response.ok) {
        throw new Error(result.detail || "Summary request failed");
      }
      if (selectedMessageId !== result.message_id) {
        return;
      }
      summaryGist.textContent = result.gist;
      summaryPoints.replaceChildren();
      (result.key_points || []).forEach(function (point) {
        const item = document.createElement("li");
        item.textContent = point;
        summaryPoints.append(item);
      });
      summaryActions.textContent = result.action_items && result.action_items.length
        ? result.action_items.join(" ")
        : "No action items identified.";
      stopSummaryLoading();
      summaryStatus.textContent = "Here is the gist.";
      messageSummary.hidden = false;
      summaryNote.textContent = "";
    } catch (error) {
      stopSummaryLoading();
      summaryStatus.textContent = "I couldn't summarize that email.";
      summaryNote.textContent = "Please try again.";
      summarizeButton.disabled = false;
    } finally {
      summaryRequestActive = false;
      summarizeButton.removeAttribute("aria-busy");
    }
  }

  function safeHttpUrl(value) {
    try {
      const url = new URL(value, window.location.origin);
      return (url.protocol === "http:" || url.protocol === "https:") ? url.href : null;
    } catch (error) {
      return null;
    }
  }

  function renderBodyContent(content) {
    detailBody.replaceChildren();
    let list = null;
    content.forEach(function (block) {
      if (block.type !== "list_item" && list) {
        detailBody.append(list);
        list = null;
      }
      if (block.type === "list_item" && !list) {
        list = document.createElement("ul");
      }
      const element = document.createElement(
        block.type === "heading" ? "h" + Math.min(Math.max(block.level || 3, 1), 6) :
          block.type === "list_item" ? "li" : "p"
      );
      if (block.type === "list_item") {
        element.className = "message-detail__list-item";
      }
      (block.children || []).forEach(function (child) {
        if (child.type === "link") {
          const href = safeHttpUrl(child.href);
          if (!href) {
            const text = document.createTextNode(child.text || "");
            element.append(text);
            return;
          }
          const link = document.createElement("a");
          link.href = href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = child.text || href;
          element.append(link);
        } else {
          const text = document.createTextNode(child.text || "");
          element.append(text);
        }
      });
      if (list) {
        list.append(element);
      } else {
        detailBody.append(element);
      }
    });
    if (list) {
      detailBody.append(list);
    }
  }

  async function loadMessageDetail(messageId) {
    if (!messageId || detailRequestActive) {
      return;
    }
    detailRequestActive = true;
    selectedMessageId = messageId;
    resetSummaryState();
    setDetailState("loading");
    try {
      const response = await fetch("/api/gmail/messages/" + encodeURIComponent(messageId), {
        credentials: "same-origin",
      });
      if (!response.ok) {
        throw new Error("Message request failed");
      }
      const message = await response.json();
      detailSubject.textContent = message.subject || "(No subject)";
      detailFrom.textContent = message.from || "Unknown sender";
      detailDate.textContent = message.date || "Date unavailable";
      if (Array.isArray(message.body_content)) {
        renderBodyContent(message.body_content);
      } else {
        detailBody.textContent = message.body || "This message has no plain-text content.";
      }
      summarizeButton.disabled = false;
      setDetailState("message");
    } catch (error) {
      setDetailState("error");
    } finally {
      detailRequestActive = false;
    }

    if (summarizeButton) {
      summarizeButton.addEventListener("click", summarizeSelectedMessage);
    }
  }

  async function loadMessages(force) {
    if (state.connection !== "connected" || inboxRequestActive ||
      (inboxLoaded && !force)) {
      if (state.connection !== "connected") {
        setInboxState("disconnected");
      }
      return;
    }
    inboxRequestActive = true;
    setInboxState("loading");
    if (refreshInboxButton) {
      refreshInboxButton.disabled = true;
      refreshInboxButton.setAttribute("aria-busy", "true");
    }
    if (messageList) {
      messageList.addEventListener("click", function (event) {
        const row = event.target.closest("[data-message-id]");
        if (row) {
          loadMessageDetail(row.dataset.messageId);
        }
      });
      messageList.addEventListener("keydown", function (event) {
        if (event.key !== "Enter" && event.key !== " ") {
          return;
        }
        const row = event.target.closest("[data-message-id]");
        if (row) {
          event.preventDefault();
          loadMessageDetail(row.dataset.messageId);
        }
      });
    }
    try {
      const response = await fetch("/api/gmail/messages?max_results=20", {
        credentials: "same-origin",
      });
      if (response.status === 401) {
        state.connection = "disconnected";
        inboxLoaded = false;
        connectionLabel.textContent = "Not connected";
        if (refreshInboxButton) {
          refreshInboxButton.hidden = true;
        }
        setInboxState("disconnected");
        return;
      }
      if (!response.ok) {
        throw new Error("Inbox request failed");
      }
      const payload = await response.json();
      const messages = Array.isArray(payload.messages) ? payload.messages : [];
      renderMessages(messages);
      inboxLoaded = true;
      if (messageCount) {
        messageCount.textContent = messages.length + (messages.length === 1 ? " message" : " messages");
      }
      setInboxState(messages.length ? "messages" : "empty");
    } catch (error) {
      inboxLoaded = false;
      setInboxState("error");
    } finally {
      inboxRequestActive = false;
      if (refreshInboxButton) {
        refreshInboxButton.disabled = false;
        refreshInboxButton.removeAttribute("aria-busy");
      }
      if (walkthroughPending && state.screen === "dashboard") {
        walkthroughPending = false;
        window.setTimeout(openTour, 100);
      }
    }
  }

  function showWelcome() {
    state.screen = "welcome";
    body.classList.add("onboarding-active");
    onboarding.hidden = false;
    dashboard.hidden = true;
    showOnboardingScene("welcome");
    setBuddyExpression("neutral");
    title.textContent = "";
    title.setAttribute("aria-label", typingText);
    copy.textContent = "I’ll help you take things one message at a time and get things set up together.";
    note.hidden = true;
    primaryButton.textContent = "Continue";
    secondaryButton.textContent = "Skip intro";
    inboxLink.classList.remove("nav-link--active");
    settingsLink.classList.add("nav-link--active");
    window.clearInterval(typingTimer);
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      title.textContent = typingText;
      primaryButton.classList.add("is-revealed");
      setBuddyExpression("happy");
      return;
    }
    typingPosition = 0;
    const typeNextCharacter = function () {
      title.textContent = typingText.slice(0, typingPosition);
      typingPosition += 1;
      if (typingPosition > typingText.length) {
        window.clearInterval(typingTimer);
        primaryButton.classList.add("is-revealed");
        setBuddyExpression("happy");
        return;
      }
      const character = typingText[typingPosition - 1];
      const pause = ".!?,".includes(character) ? 130 : 24 + Math.floor(Math.random() * 22);
      typingTimer = window.setTimeout(typeNextCharacter, pause);
    };
    typeNextCharacter();
  }

  function showConnection() {
    state.screen = "connection";
    body.classList.add("onboarding-active");
    onboarding.hidden = false;
    dashboard.hidden = true;
    showOnboardingScene("connection");
    setBuddyExpression("neutral");
    setupPrimaryButton.disabled = true;
    setupPrimaryButton.removeAttribute("aria-busy");
    setupPrimaryButton.setAttribute("aria-disabled", "true");
    setupPrimaryButton.textContent = "Checking Gmail…";
    setupSecondaryButton.textContent = "Back to welcome";
    refreshConnectionStatus();
  }

  function updateConnectionControls(status) {
    connectionConfigured = status.configured;
    state.connection = status.connected ? "connected" : "disconnected";
    connectionLabel.textContent = status.connected ? "Connected" : "Not connected";
    if (state.screen !== "connection") {
      return;
    }
    if (status.connected) {
      setupPrimaryButton.disabled = true;
      setupPrimaryButton.textContent = "Gmail connected";
      setupNote.hidden = true;
      return;
    }
    setupPrimaryButton.textContent = status.configured ? "Connect Gmail" : "Connect Gmail unavailable";
    setupPrimaryButton.disabled = !status.configured;
    if (status.configured) {
      setupPrimaryButton.removeAttribute("aria-disabled");
      setupNote.hidden = !authorizationMessage;
      setupNote.textContent = authorizationMessage || "";
    } else {
      setupPrimaryButton.setAttribute("aria-disabled", "true");
      setupNote.hidden = true;
      setupNote.textContent = "Gmail connection requires backend configuration. No password or account data has been requested.";
    }
  }

  async function refreshConnectionStatus() {
    try {
      const response = await fetch("/api/gmail/status", { credentials: "same-origin" });
      if (!response.ok) {
        throw new Error("Connection status unavailable");
      }
      const status = await response.json();
      csrfToken = status.csrf_token || null;
      updateConnectionControls(status);
      if (
        status.connected &&
        state.oauthConnected &&
        state.screen !== "celebration"
      ) {
        showCelebration();
      } else if (status.connected && (state.screen === "welcome" || state.screen === "connection")) {
        showDashboard(!storageHasCompleted());
      }
      if (state.screen === "dashboard" && status.connected) {
        if (refreshInboxButton) {
          refreshInboxButton.hidden = false;
        }
        loadMessages(false);
      } else if (refreshInboxButton) {
        refreshInboxButton.hidden = true;
      }
      if (connectionStateNote) {
        connectionStateNote.textContent = status.connected
            ? "Gmail is connected. Your inbox metadata can now be loaded."
          : status.configured
            ? "Connect Gmail to authorize read-only access."
            : "Account connection is not available in this preview.";
      }
    } catch (error) {
      connectionConfigured = false;
      if (state.screen === "connection") {
        setupPrimaryButton.disabled = true;
        setupPrimaryButton.setAttribute("aria-disabled", "true");
        setupNote.hidden = false;
        setupNote.textContent = "We couldn't check Gmail configuration. Try again later.";
      }
    }
  }

  function startAuthorization() {
    setupPrimaryButton.disabled = true;
    setupPrimaryButton.setAttribute("aria-busy", "true");
    setupPrimaryButton.textContent = "Opening Google…";
    window.location.assign("/api/gmail/authorize");
  }

  async function disconnectGmail() {
    primaryButton.disabled = true;
    primaryButton.setAttribute("aria-busy", "true");
    try {
      const response = await fetch("/api/gmail/disconnect", {
        method: "POST",
        credentials: "same-origin",
        headers: csrfToken ? { "X-CSRF-Token": csrfToken } : {},
      });
      if (!response.ok) {
        throw new Error("Disconnect failed");
      }
      await refreshConnectionStatus();
    } catch (error) {
      note.hidden = false;
      note.textContent = "We couldn't disconnect Gmail. Please try again.";
    } finally {
      primaryButton.removeAttribute("aria-busy");
    }
  }

  function resetPrimary() {
    primaryButton.disabled = false;
    primaryButton.removeAttribute("aria-disabled");
  }

  primaryButton.addEventListener("click", function () {
    if (state.screen === "welcome") {
      showConnection();
    }
  });

  secondaryButton.addEventListener("click", function () {
    if (state.screen === "welcome") {
      showConnection();
    }
  });

  setupPrimaryButton.addEventListener("click", function () {
    if (state.screen === "connection" && connectionConfigured && state.connection !== "connected") {
      startAuthorization();
    }
  });

  setupSecondaryButton.addEventListener("click", function () {
    if (state.screen === "connection") {
      showWelcome();
    }
  });

  if (dialog) {
    nextButton.addEventListener("click", function () {
      if (state.tourStep === tourSteps.length - 1) {
        closeTour(true);
        return;
      }
      state.tourStep += 1;
      renderTourStep();
      nextButton.focus();
    });
    backButton.addEventListener("click", function () {
      if (state.tourStep > 0) {
        state.tourStep -= 1;
        renderTourStep();
        backButton.focus();
      }
    });
    skipButton.addEventListener("click", function () {
      closeTour(true);
    });
    window.addEventListener("resize", positionTour);
  }

  if (connectionButton) {
    connectionButton.addEventListener("click", showConnection);
  }

  settingsLink.addEventListener("click", function (event) {
    event.preventDefault();
    resetPrimary();
    showWelcome();
  });

  inboxLink.addEventListener("click", function (event) {
    event.preventDefault();
    if (state.connection === "connected") {
      showDashboard();
    } else {
      showConnection();
    }
  });

  if (retryButton) {
    retryButton.addEventListener("click", function () {
      loadMessages(true);
    });
  }
  if (refreshInboxButton) {
    refreshInboxButton.addEventListener("click", function () {
      loadMessages(true);
    });
  }

  showWelcome();
  const oauthResult = new URLSearchParams(window.location.search).get("oauth");
  if (oauthResult) {
    state.oauthConnected = oauthResult === "connected";
    authorizationMessage = oauthResult === "denied"
      ? "Google authorization was cancelled. You can try again when ready."
      : "Gmail authorization could not be completed. Please try again.";
    showConnection();
    if (oauthResult === "connected") {
      setupNote.textContent = "Gmail is connected. Preparing your inbox…";
    } else if (oauthResult === "denied") {
      setupNote.textContent = "Google authorization was cancelled. You can try again when ready.";
    } else {
      setupNote.textContent = "Gmail authorization could not be completed. Please try again.";
    }
    window.history.replaceState({}, "", window.location.pathname + "#settings");
  }
  refreshConnectionStatus();
})();

if (summarizeButton) {
  summarizeButton.addEventListener("click", summarizeSelectedMessage);
}
if (messageList) {
  messageList.addEventListener("click", function (event) {
    const row = event.target.closest("[data-message-id]");
    if (row) loadMessageDetail(row.dataset.messageId);
  });
  messageList.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" && event.key !== " ") return;
    const row = event.target.closest("[data-message-id]");
    if (row) {
      event.preventDefault();
      loadMessageDetail(row.dataset.messageId);
    }
  });
}