/* Project Windstock — invite validation and Discord handoff */
(function () {
  var form = document.getElementById("inviteForm");
  var input = document.getElementById("inviteCode");
  var submit = document.getElementById("inviteSubmit");
  var status = document.getElementById("inviteStatus");
  if (!form || !input || !submit || !status) return;

  function setStatus(message, type) {
    status.hidden = !message;
    status.className = "invite-status" + (type ? " is-" + type : "");
    status.textContent = message;
  }

  function setBusy(busy) {
    submit.disabled = busy;
    submit.querySelector("[data-button-label]").textContent = busy ? "Checking…" : "Validate invite";
  }

  var params = new URLSearchParams(window.location.search);
  var result = params.get("joined");
  var error = params.get("error");
  if (result === "1") setStatus("You’re in. Discord has added you to the Windstock server.", "success");
  if (error) {
    var messages = {
      discord_denied: "Discord authorization was cancelled. Your invite was not used.",
      missing_callback: "The Discord callback was incomplete. Please try the invite again.",
      expired_invite: "That invite session expired. Start again with your code.",
      server_setup: "Discord access is not configured on the server yet.",
      discord_join_failed: "Discord could not complete the join. Your invite was released; please try again.",
    };
    setStatus(messages[error] || "The join flow could not be completed. Please try again.", "error");
  }

  input.addEventListener("input", function () {
    input.value = input.value.toUpperCase().replace(/\s+/g, "");
    if (status.classList.contains("is-error")) setStatus("", "");
  });

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    var code = input.value.trim();
    if (!code) {
      setStatus("Enter your invite code first.", "error");
      input.focus();
      return;
    }
    setBusy(true);
    setStatus("Checking your invite…", "loading");
    try {
      var response = await fetch("/api/invites/validate", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ code: code }),
      });
      var payload = await response.json().catch(function () { return {}; });
      if (!response.ok || !payload.ok || !payload.authorizationUrl) {
        throw new Error(payload.error || "That invite could not be validated.");
      }
      setStatus("Invite validated. Taking you to Discord…", "success");
      window.location.assign(payload.authorizationUrl);
    } catch (error) {
      setBusy(false);
      setStatus(error.message || "Could not reach the invite server. Please try again.", "error");
    }
  });
})();
