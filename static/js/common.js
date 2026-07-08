/*
 * Shared across index.html / admin.html: theme toggle, toast stack, and
 * a fetch() wrapper that redirects to /login on a 401 instead of every
 * page re-implementing that same check. Kept dependency-free (no build
 * step in this project) and attached to `window` so templates can call
 * it directly.
 */

(function () {
  const STORAGE_KEY = "ledger-ask-theme";

  function applyTheme(theme) {
    if (theme === "light" || theme === "dark") {
      document.documentElement.setAttribute("data-theme", theme);
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
  }

  function currentTheme() {
    return localStorage.getItem(STORAGE_KEY) || "auto";
  }

  function toggleTheme() {
    const order = ["auto", "light", "dark"];
    const next = order[(order.indexOf(currentTheme()) + 1) % order.length];
    localStorage.setItem(STORAGE_KEY, next);
    applyTheme(next);
    updateThemeToggleLabel();
  }

  function updateThemeToggleLabel() {
    const btn = document.getElementById("theme-toggle");
    if (!btn) return;
    const theme = currentTheme();
    btn.textContent = theme === "light" ? "☀️" : theme === "dark" ? "🌙" : "🌓";
    btn.title = `Theme: ${theme} (click to change)`;
  }

  applyTheme(currentTheme());

  // This script is loaded at the end of <body> (after the toggle button
  // already exists in the DOM), so by the time it runs, DOMContentLoaded
  // has typically already fired -- a listener registered for it here
  // would simply never call back, silently leaving the button dead.
  // Wire it up directly instead; only fall back to DOMContentLoaded for
  // the rare case this script is ever moved into <head>.
  function wireThemeToggle() {
    updateThemeToggleLabel();
    const btn = document.getElementById("theme-toggle");
    if (btn) btn.addEventListener("click", toggleTheme);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireThemeToggle);
  } else {
    wireThemeToggle();
  }

  let toastStack = null;
  function ensureToastStack() {
    if (!toastStack) {
      toastStack = document.createElement("div");
      toastStack.className = "toast-stack";
      document.body.appendChild(toastStack);
    }
    return toastStack;
  }

  function toast(message, type) {
    const stack = ensureToastStack();
    const el = document.createElement("div");
    el.className = `toast${type ? " toast-" + type : ""}`;
    el.textContent = message;
    stack.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transition = "opacity 0.2s ease";
      setTimeout(() => el.remove(), 200);
    }, 2600);
  }

  // fetch() that redirects to /login on 401 (server-side session expired
  // or never logged in) instead of every caller re-checking response.status
  // -- the one thing every page in this app that talks to /api/* needs.
  async function apiFetch(url, options) {
    const response = await fetch(url, options);
    if (response.status === 401) {
      window.location.href = "/login";
      throw new Error("Not authenticated");
    }
    return response;
  }

  function copyToClipboard(text, successMessage) {
    navigator.clipboard.writeText(text).then(
      () => toast(successMessage || "Copied to clipboard", "success"),
      () => toast("Could not copy to clipboard", "error")
    );
  }

  window.LedgerAsk = { toast, apiFetch, copyToClipboard, toggleTheme };
})();
