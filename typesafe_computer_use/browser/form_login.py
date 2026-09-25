"""Automatic sign-in on the one host named in CLICKER_FORM_LOGIN.

Credentials come from the local environment. This module never returns a field value and only
submits them from a page on that exact HTTPS host: a real <form> must also post to that host, and
a form without one, which a script sends, is submitted by its one sign-in button.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

# Finds the one login form on the page, or says why there is none to fill in. `host` is null when
# only the state is wanted; given, a real <form> must also post to it. It never reads a value.
FIND_LOGIN_JS = r"""
(host) => {
  const sameSecureHost = url => url.protocol === "https:" && url.hostname.toLowerCase() === host && (!url.port || url.port === "443");
  const shown = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.visibility !== "hidden" &&
      s.display !== "none" && Number.parseFloat(s.opacity || "1") > 0;
  };
  const visible = el => shown(el) && !el.disabled;
  const inputs = [...document.querySelectorAll("input")].filter(visible);
  const captcha = [...document.querySelectorAll(
    '.g-recaptcha,.h-captcha,[data-sitekey],iframe[src*="captcha" i],iframe[title*="captcha" i],[id*="captcha" i],[class*="captcha" i]'
  )].some(visible);
  const clue = el => {
    const labels = el.labels ? [...el.labels].map(label => label.textContent || "").join(" ") : "";
    return [el.name, el.id, el.placeholder, el.autocomplete, labels].join(" ").toLowerCase();
  };
  const otp = inputs.some(el =>
    /(^|\s)one-time-code(\s|$)/i.test(el.autocomplete || "") ||
    /one.?time|\botp\b|verification|authenticator|passcode|security.?code/.test(clue(el))
  );
  if (captcha) return {state: "manual_required:captcha"};
  if (otp) return {state: "manual_required:one_time_code"};

  const passwords = inputs.filter(el => el.type.toLowerCase() === "password");
  // A password field still on screen but switched off is a sign-in in flight, not one finished.
  if (!passwords.length && [...document.querySelectorAll('input[type="password" i]')].some(shown)) return {state: "busy"};
  if (!passwords.length) return {state: "not_needed"};
  if (passwords.length !== 1) return {state: "manual_required:password_fields_" + passwords.length};
  const password = passwords[0];
  const pick = inside => {
    const candidates = inputs.filter(el => inside(el) && ["text", "email", "tel"].includes(el.type.toLowerCase()) && !el.readOnly);
    const explicit = candidates.filter(el => (el.autocomplete || "").toLowerCase().split(/\s+/).includes("username"));
    const field = explicit.length === 1 ? explicit[0] : (!explicit.length && candidates.length === 1 ? candidates[0] : null);
    return {field, count: explicit.length || candidates.length};
  };

  const form = password.form || password.closest("form");
  if (form) {
    const {field, count} = pick(el => el.form === form);
    if (!field) return {state: "manual_required:username_fields_" + count};
    if (host !== null) {
      const action = new URL(form.getAttribute("action") || location.href, location.href);
      if (!sameSecureHost(action)) return {state: "manual_required:form_action_host"};
      if (typeof form.requestSubmit !== "function") return {state: "manual_required:no_submit"};
    }
    return {state: "login_form", username: field, password, form};
  }

  // No <form>: a script signs in when its button is pressed. The form is the smallest box around
  // the password field that holds one username field and, after the password, one sign-in button:
  // a Login tab above the fields is not it. The button counts while disabled: many turn on only
  // once both fields are filled.
  const SIGN_IN = /^(log\s*-?\s*in|sign\s*-?\s*in|登入|登錄|登录|submit|continue|next|繼續|下一步)$/i;
  const label = el => (el.tagName === "INPUT" ? el.value : el.innerText || el.getAttribute("aria-label") || "").trim();
  let sawUsername = false;
  for (let box = password.parentElement; box && box !== document.documentElement; box = box.parentElement) {
    const {field, count} = pick(el => box.contains(el));
    if (!field) {
      if (count > 1) return {state: "manual_required:username_fields_" + count};
      continue;
    }
    sawUsername = true;
    const buttons = [...box.querySelectorAll('button,[role="button"],input[type="submit"],input[type="button"]')].filter(el =>
      shown(el) && password.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING && SIGN_IN.test(label(el)));
    if (buttons.length === 1) return {state: "login_form", username: field, password, button: buttons[0]};
    if (buttons.length > 1) return {state: "manual_required:submit_buttons_" + buttons.length};
  }
  return {state: sawUsername ? "manual_required:no_sign_in_button" : "manual_required:no_username_field"};
}
""".strip()

LOGIN_PAGE_STATE_JS = f"(() => ({FIND_LOGIN_JS})(null).state)()"

LOGIN_SUBMIT_JS = r"""
async function(host, username, password) {
  if (!(location.protocol === "https:" && location.hostname.toLowerCase() === host && (!location.port || location.port === "443"))) {
    return "outside_target";
  }
  const found = (FIND_LOGIN)(host);
  if (found.state !== "login_form") return found.state;
  const setValue = (el, value) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", {bubbles: true}));
    el.dispatchEvent(new Event("change", {bubbles: true}));
  };
  const off = el => el.disabled || el.getAttribute("aria-disabled") === "true";
  try {
    setValue(found.username, username);
    setValue(found.password, password);
    if (found.form) {
      found.form.requestSubmit();
    } else {
      // The page's script turns the button on once it has seen the input: give it a moment.
      for (let waited = 0; off(found.button) && waited < 3000; waited += 100) {
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      if (off(found.button)) return "manual_required:sign_in_button_disabled";
      found.button.click();
    }
  } catch (_) {
    return "manual_required:submit_error";
  }
  return "submitted";
}
""".strip().replace("FIND_LOGIN", FIND_LOGIN_JS)


# Finds where a one-time code goes: one field that says it takes the code, or a row of one-letter
# boxes, one per character of a code this long. Such boxes usually have no name, so the element list
# the classifier reads never shows them. It never reads a value.
FIND_OTP_JS = r"""
(length) => {
  const shown = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.visibility !== "hidden" &&
      s.display !== "none" && Number.parseFloat(s.opacity || "1") > 0;
  };
  const inputs = [...document.querySelectorAll("input")].filter(el => shown(el) && !el.disabled && !el.readOnly &&
    ["text", "tel", "number", "password", ""].includes((el.getAttribute("type") || "").toLowerCase()));
  const clue = el => {
    const labels = el.labels ? [...el.labels].map(label => label.textContent || "").join(" ") : "";
    return [el.name, el.id, el.placeholder, el.autocomplete, el.getAttribute("aria-label"), labels].join(" ").toLowerCase();
  };
  const single = inputs.filter(el =>
    /(^|\s)one-time-code(\s|$)/i.test(el.autocomplete || "") ||
    /one.?time|\botp\b|verification|authenticator|passcode|security.?code|驗證碼|验证码/.test(clue(el)));
  const boxes = inputs.filter(el => el.maxLength === 1);
  let fields;
  if (boxes.length === length) fields = boxes;
  else if (single.length === 1 && !boxes.length) fields = single;
  else if (boxes.length > 1) return {state: "manual_required:otp_boxes_" + boxes.length};
  else if (single.length > 1) return {state: "manual_required:otp_fields_" + single.length};
  else return {state: "not_needed"};

  let box = fields[0].parentElement;
  while (box && !fields.every(el => box.contains(el))) box = box.parentElement;
  const last = fields[fields.length - 1];
  const VERIFY = /^(verify|verify otp|submit|confirm|continue|next|log\s*-?\s*in|sign\s*-?\s*in|驗證|验证|確認|确认|提交|繼續|下一步|登入)$/i;
  const label = el => (el.tagName === "INPUT" ? el.value : el.innerText || el.getAttribute("aria-label") || "").trim();
  let button = null;
  for (; box && box !== document.documentElement; box = box.parentElement) {
    const found = [...box.querySelectorAll('button,[role="button"],input[type="submit"],input[type="button"]')].filter(el =>
      shown(el) && last.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING && VERIFY.test(label(el)));
    if (found.length === 1) { button = found[0]; break; }
    if (found.length > 1) return {state: "manual_required:verify_buttons_" + found.length};
  }
  return {state: "otp_form", fields, button};
}
""".strip()


def _otp_state_js(length: int) -> str:
    return f"(({FIND_OTP_JS})({int(length)}).state)"


OTP_SUBMIT_JS = r"""
async function(host, code) {
  if (!(location.protocol === "https:" && location.hostname.toLowerCase() === host && (!location.port || location.port === "443"))) {
    return "outside_target";
  }
  const found = (FIND_OTP)(code.length);
  if (found.state !== "otp_form") return found.state;
  const setValue = (el, value) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    el.focus();
    setter.call(el, value);
    el.dispatchEvent(new Event("input", {bubbles: true}));
    el.dispatchEvent(new Event("change", {bubbles: true}));
  };
  const off = el => el.disabled || el.getAttribute("aria-disabled") === "true";
  try {
    if (found.fields.length === 1) setValue(found.fields[0], code);
    else found.fields.forEach((el, i) => setValue(el, code[i]));
    // With no button, a page that checks the code once the last box is filled has been answered.
    if (!found.button) return "submitted";
    for (let waited = 0; off(found.button) && waited < 3000; waited += 100) {
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    // A page that checked the code on its own may already have moved on.
    if (!found.button.isConnected) return "submitted";
    if (off(found.button)) return "manual_required:verify_button_disabled";
    found.button.click();
  } catch (_) {
    return "manual_required:submit_error";
  }
  return "submitted";
}
""".strip().replace("FIND_OTP", FIND_OTP_JS)


# Why a form needs a person, from the page script. Only these reach the log: a reason is a fixed
# word and a count, never anything read from the page.
MANUAL_REASON = re.compile(
    r"manual_required:(captcha|one_time_code|no_username_field|no_sign_in_button|sign_in_button_disabled|form_action_host|no_submit|submit_error|password_fields_\d{1,2}|username_fields_\d{1,2}|submit_buttons_\d{1,2}|verify_button_disabled|otp_boxes_\d{1,2}|otp_fields_\d{1,2}|verify_buttons_\d{1,2})"
)


def is_manual(state: str) -> bool:
    return state.partition(":")[0] == "manual_required"


def _checked(state: Any, allowed: set[str]) -> str:
    if isinstance(state, str) and (state in allowed or MANUAL_REASON.fullmatch(state)):
        return state
    return "manual_required"


def is_login_url(url: str, host: str) -> bool:
    """Whether a URL is HTTPS on exactly this host, with no URL credentials."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and (parts.hostname or "").lower() == host.lower()
        and port in (None, 443)
        and parts.username is None
        and parts.password is None
    )


def inspect_login_page(session: Any, host: str) -> str:
    """Return a sanitized state; never retrieve or expose any input value."""
    try:
        url = str(session.evaluate("location.href") or "")
        if not is_login_url(url, host):
            return "outside_target"
        state = session.evaluate(LOGIN_PAGE_STATE_JS)
    except Exception:
        return "manual_required"
    return _checked(state, {"login_form", "busy", "not_needed", "manual_required"})


def auto_login(session: Any, host: str, username: str, password: str) -> str:
    """Fill and submit a uniquely identified login form on this host, or return a safe status."""
    state = inspect_login_page(session, host)
    if state != "login_form":
        return state
    expression = f"({LOGIN_SUBMIT_JS})({json.dumps(host.lower())}, {json.dumps(username)}, {json.dumps(password)})"
    try:
        result = session.evaluate(expression, await_promise=True)
    except Exception:
        return "manual_required"
    return _checked(result, {"submitted", "busy", "not_needed", "outside_target", "manual_required"})


def wait_for_login_result(session: Any, host: str, *, timeout_ms: int = 8000, poll_ms: int = 200) -> str:
    """Wait for a submitted form to leave its login state, including SPA-backed submissions.

    A form whose fields are switched off while the request is out is still waited on."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        state = inspect_login_page(session, host)
        if state not in ("login_form", "busy"):
            return state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "login_failed"
        time.sleep(min(poll_ms / 1000, remaining))


def inspect_otp_page(session: Any, host: str, length: int) -> str:
    """Whether a one-time code of this length is asked for on this host: otp_form, not_needed, or why not."""
    try:
        if not is_login_url(str(session.evaluate("location.href") or ""), host):
            return "outside_target"
        state = session.evaluate(_otp_state_js(length))
    except Exception:
        # Asked on every step, often mid-navigation: a page that cannot answer has asked for nothing.
        return "not_needed"
    return _checked(state, {"otp_form", "not_needed", "manual_required"})


def auto_otp(session: Any, host: str, code: str) -> str:
    """Type the fixed code where the page asks for it, and press its verify button when it has one."""
    state = inspect_otp_page(session, host, len(code))
    if state != "otp_form":
        return state
    expression = f"({OTP_SUBMIT_JS})({json.dumps(host.lower())}, {json.dumps(code)})"
    try:
        result = session.evaluate(expression, await_promise=True)
    except Exception:
        return "manual_required"
    return _checked(result, {"submitted", "not_needed", "outside_target", "manual_required"})


def wait_for_otp_result(session: Any, host: str, length: int, *, timeout_ms: int = 8000, poll_ms: int = 200) -> str:
    """Wait for the code boxes to go; still there at the end, the code was not taken."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        state = inspect_otp_page(session, host, length)
        if state != "otp_form":
            return state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "otp_failed"
        time.sleep(min(poll_ms / 1000, remaining))
