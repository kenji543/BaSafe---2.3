(() => {
  "use strict";

  const form = document.getElementById("login-form");
  const username = document.getElementById("login-username");
  const password = document.getElementById("login-password");
  const submit = document.getElementById("login-submit");
  const error = document.getElementById("login-error");
  const notice = document.getElementById("login-notice");
  const toggle = document.getElementById("toggle-password");

  const parameters = new URLSearchParams(window.location.search);
  if (parameters.get("expired") === "1") {
    notice.textContent = "Your session expired. Sign in again to continue.";
    notice.hidden = false;
  } else if (parameters.get("signed_out") === "1") {
    notice.textContent = "You have been signed out safely.";
    notice.hidden = false;
  }

  toggle.addEventListener("click", () => {
    const showing = password.type === "text";
    password.type = showing ? "password" : "text";
    toggle.textContent = showing ? "Show" : "Hide";
    toggle.setAttribute("aria-label", showing ? "Show password" : "Hide password");
    toggle.setAttribute("aria-pressed", String(!showing));
    password.focus();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    if (!username.value.trim() || !password.value) {
      error.textContent = "Enter both your username and password.";
      error.hidden = false;
      (!username.value.trim() ? username : password).focus();
      return;
    }
    submit.disabled = true;
    submit.querySelector("span").textContent = "Signing in…";
    try {
      const response = await fetch("/api/v1/admin/login", {
        method: "POST",
        cache: "no-store",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ username: username.value.trim(), password: password.value })
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error?.message || "Sign in was unsuccessful.");
      password.value = "";
      window.location.replace(payload.redirect || "/admin");
    } catch (caught) {
      error.textContent = caught.message || "Sign in was unsuccessful.";
      error.hidden = false;
      password.select();
    } finally {
      submit.disabled = false;
      submit.querySelector("span").textContent = "Sign in securely";
    }
  });

  username.focus();
})();
